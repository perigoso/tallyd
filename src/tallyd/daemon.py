"""Call state machine and the event loop that drives it."""

from __future__ import annotations

import logging
import os
import select
import signal
import socket
import subprocess
import time

from .camera import CameraWatcher
from .config import Config
from .inotify import IN_Q_OVERFLOW, Inotify
from .mic import MicWatcher

log = logging.getLogger(__name__)

START = "start"
END = "end"

# The camera is event-driven; this periodic re-read exists only to heal state
# if an inotify event is ever missed. Rare enough to be cheap at this rate, and
# deliberately decoupled from poll_interval so tightening that does not make
# every tick walk /proc.
CAMERA_RESYNC_INTERVAL = 30.0


class CallState:
    """Edge-triggered call state with a dwell timer.

    `dwell` holds the in-call state for N seconds after the last signal stops,
    so muting the microphone or turning the camera off mid-meeting does not
    flap the actions. Teams, Zoom and Discord mute in software and keep the
    stream running, but browser WebRTC may genuinely drop the track, which
    without a dwell would read as "call ended".
    """

    def __init__(self, dwell: float) -> None:
        self.dwell = dwell
        self.in_call = False
        self.sources: tuple[str, ...] = ()
        self._last_active = 0.0
        self._signalled = False

    def update(self, sources: list[str], now: float) -> str | None:
        """Feed the current signal sources; returns START/END on an edge only."""
        self._signalled = bool(sources)
        if sources:
            self._last_active = now
            self.sources = tuple(sources)
            if not self.in_call:
                self.in_call = True
                return START
            return None
        if self.in_call and now - self._last_active >= self.dwell:
            self.in_call = False
            self.sources = ()
            return END
        return None

    def deadline(self) -> float | None:
        """When the dwell timer next needs attention, or None if it is idle.

        While capture is still live the timer is not counting down, so there is
        nothing to wake up for -- returning a deadline here would spin the loop
        at 100% CPU whenever dwell is 0.
        """
        if self.in_call and not self._signalled:
            return self._last_active + self.dwell
        return None


def sd_notify(message: str) -> None:
    """Best-effort sd_notify(3); a no-op outside systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):  # abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as exc:
        log.debug("sd_notify failed: %s", exc)


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = CallState(config.dwell)
        self._stop = False
        self._children: list[subprocess.Popen] = []

    # -- actions ---------------------------------------------------------
    def _run_action(self, edge: str, sources: tuple[str, ...]) -> None:
        command = self.config.on_call_start if edge == START else self.config.on_call_end
        if not command:
            return
        env = dict(os.environ,
                   TALLYD_STATE="active" if edge == START else "inactive",
                   TALLYD_SOURCES=" ".join(sources))
        try:
            # Spawned, never waited on: a hanging action must not stop
            # detection. start_new_session keeps it off our signal path.
            self._children.append(subprocess.Popen(
                command, shell=True, env=env, start_new_session=True))
        except OSError as exc:
            log.error("could not run %s action %r: %s", edge, command, exc)

    def _reap(self) -> None:
        still_running = []
        for child in self._children:
            code = child.poll()
            if code is None:
                still_running.append(child)
            elif code != 0:
                log.warning("action exited with status %s", code)
        self._children = still_running

    # -- loop ------------------------------------------------------------
    def _handle_edge(self, edge: str) -> None:
        if edge == START:
            log.info("call started (%s)", ", ".join(self.state.sources))
        else:
            log.info("call ended")
        self._run_action(edge, self.state.sources)

    def request_stop(self, *_args) -> None:
        self._stop = True

    def run(self) -> int:
        inotify = Inotify()
        # A socketpair woken by signal handlers lets poll() return immediately
        # on SIGTERM instead of sitting out the remaining poll interval.
        wake_r, wake_w = socket.socketpair()
        for sock in (wake_r, wake_w):
            sock.setblocking(False)
        previous_wakeup = signal.set_wakeup_fd(wake_w.fileno())
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self.request_stop)

        try:
            camera = CameraWatcher(inotify)
            mic = MicWatcher(
                retry_interval=self.config.mic_retry_interval,
                failures_before_backoff=self.config.mic_failures_before_backoff)
            poller = select.poll()
            poller.register(inotify.fileno(), select.POLLIN)
            poller.register(wake_r.fileno(), select.POLLIN)

            # Probe before announcing readiness, so the startup summary says
            # what actually works rather than what was configured.
            mic_sources = mic.active()
            log.info("watching %d camera device(s); microphone %s; dwell %.1fs",
                     len(camera.devices),
                     f"polled every {self.config.poll_interval:.1f}s"
                     if mic.available else "unavailable, running camera-only",
                     self.config.dwell)
            if not camera.devices and not mic.available:
                log.error("no camera devices and no microphone detection; "
                          "nothing can be detected until one of them appears")
            sd_notify("READY=1")

            camera_sources: list[str] = []
            camera_changed = True
            next_poll = time.monotonic() + self.config.poll_interval
            next_camera = 0.0
            while not self._stop:
                now = time.monotonic()
                if now >= next_poll:
                    mic_sources = mic.active()
                    next_poll = now + self.config.poll_interval
                    self._reap()
                if camera_changed or now >= next_camera:
                    camera_sources = camera.active()
                    camera_changed = False
                    next_camera = now + CAMERA_RESYNC_INTERVAL

                edge = self.state.update(mic_sources + camera_sources, now)
                if edge:
                    self._handle_edge(edge)

                poller.poll(self._timeout_ms(time.monotonic(), next_poll, next_camera))
                camera_changed = self._drain(inotify, camera, wake_r)
        finally:
            signal.set_wakeup_fd(previous_wakeup)
            inotify.close()
            wake_r.close()
            wake_w.close()

        if self.state.in_call:
            # Leave the machine in its normal state: if on_call_start locked
            # something down, exiting without the counterpart would strand it.
            log.info("shutting down while in a call; running the end action")
            self.state.in_call = False
            self._handle_edge(END)
        self._drain_children()
        return 0

    def _timeout_ms(self, now: float, next_poll: float, next_camera: float) -> int:
        deadline = min(next_poll, next_camera)
        dwell_deadline = self.state.deadline()
        if dwell_deadline is not None:
            deadline = min(deadline, dwell_deadline)
        return max(0, int((deadline - now) * 1000))

    def _drain(self, inotify: Inotify, camera: CameraWatcher,
               wake: socket.socket) -> bool:
        """Consume pending wake-ups; True if the camera needs re-reading."""
        try:
            wake.recv(4096)
        except (BlockingIOError, OSError):
            pass
        changed = False
        for event in inotify.read():
            if event.mask & IN_Q_OVERFLOW:
                log.warning("inotify queue overflowed; resynchronising from /proc")
                camera.rescan_devices()
                changed = True
            elif not event.name:
                # open/close on a device we watch: the interesting case.
                changed = True
            elif camera.is_device_event(event.name):
                # Hotplug seen on the /dev watch: a webcam came or went.
                camera.rescan_devices()
                changed = True
            # Anything else in /dev is not ours; do not walk /proc for it.
        return changed

    def _drain_children(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        for child in self._children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                log.warning("action still running at shutdown; leaving it detached")
