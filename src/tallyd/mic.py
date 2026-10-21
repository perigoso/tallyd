"""Microphone-in-use detection via the PipeWire graph.

PipeWire is an *optional* dependency. Without it the daemon keeps running on
the camera alone, says so in the log, and picks the microphone back up if
PipeWire later appears -- no restart needed.

Every audio path on a PipeWire desktop goes through the graph, including
Bluetooth headsets -- which bypass ALSA entirely, so /proc/asound reports
`closed` right through a live BT capture and cannot be used.

The signal is a node with media.class "Stream/Input/Audio" in state "running".
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

STREAM_CLASS = "Stream/Input/Audio"
_NODE_TYPE = "PipeWire:Interface:Node"
_DUMP_COMMAND = ("pw-dump",)
_DUMP_TIMEOUT = 5.0

# Once PipeWire looks absent, stop retrying at poll_interval. A missing binary
# or a stopped daemon does not fix itself in two seconds, and spawning a doomed
# subprocess every tick is the opposite of degrading gracefully. Both are
# overridable from [detection] in the config file.
_RETRY_INTERVAL = 30.0
_FAILURES_BEFORE_BACKOFF = 3


def _is_monitor(props: dict) -> bool:
    # pw-dump emits this as a JSON bool or the string "true" depending on origin.
    return props.get("stream.monitor") in (True, "true")


def _label(node: dict, props: dict) -> str:
    name = (props.get("application.process.binary")
            or props.get("application.name")
            or props.get("node.name")
            or f"node-{node.get('id')}")
    return f"mic:{name}"


def active_streams(dump: list) -> list[str]:
    """Capture streams in a parsed `pw-dump`, as ``mic:<app>`` labels.

    Dropping monitor streams is not optional: plasmashell keeps several
    "Stream/Input/Audio" nodes in state "running" for its volume peak meters,
    so without this filter the daemon reports a permanent call. They are sink
    monitors and all carry stream.monitor=true.

    The exact class match also drops "Stream/Input/Audio/Internal", the BlueZ
    HFP node, which goes running alongside the real stream and would double it.
    """
    labels = []
    for node in dump:
        if not isinstance(node, dict) or node.get("type") != _NODE_TYPE:
            continue
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != STREAM_CLASS:
            continue
        if info.get("state") != "running" or _is_monitor(props):
            continue
        labels.append(_label(node, props))
    return sorted(set(labels))


class MicWatcher:
    """Polls `pw-dump`, and copes with it not being there.

    `available` reports whether microphone detection is currently working.
    It flips either way at runtime -- PipeWire can be installed, started or
    stopped under a running daemon -- and every flip is logged.

    ponytail: polling, not libpipewire. One poll costs ~14 ms (anecdotal),
    so the default 2 s interval is well under 1% of a core. `pw-dump -m`
    streams events but its deltas omit unchanged props -- including
    stream.monitor, the filter above -- so it needs full shadow state to be
    correct. Swap it in only if sub-second latency on the *microphone* ever
    matters; the camera, which is the privacy-critical one, is already event-driven.
    """

    def __init__(self, command: tuple[str, ...] = _DUMP_COMMAND,
                 timeout: float = _DUMP_TIMEOUT,
                 retry_interval: float = _RETRY_INTERVAL,
                 failures_before_backoff: int = _FAILURES_BEFORE_BACKOFF) -> None:
        self._command = command
        self._timeout = timeout
        self._retry_interval = retry_interval
        self._failures_before_backoff = failures_before_backoff
        self.available = True
        self.failures = 0
        self._next_attempt = 0.0

    def _degrade(self, reason: str, now: float) -> list[str]:
        self.failures += 1
        if self.available:
            self.available = False
            log.warning(
                "microphone detection unavailable (%s); continuing with the "
                "camera only -- audio-only calls will not be detected", reason)
        if self.failures >= self._failures_before_backoff:
            self._next_attempt = now + self._retry_interval
        return []

    def _restore(self) -> None:
        if not self.available:
            log.info("microphone detection restored after %d failed attempt(s)",
                     self.failures)
        self.available = True
        self.failures = 0
        self._next_attempt = 0.0

    def active(self, now: float | None = None) -> list[str]:
        """Capture streams right now, or [] if PipeWire cannot be reached.

        Reporting "no microphone" while degraded is the quieter wrong answer
        than a call that never ends; the log is what surfaces the fault.
        """
        now = time.monotonic() if now is None else now
        if now < self._next_attempt:
            return []  # backed off, camera still live
        # A PATH lookup, not a subprocess: "PipeWire is not installed" then
        # costs a few stat calls per retry instead of a doomed fork+exec.
        if shutil.which(self._command[0]) is None:
            return self._degrade(f"{self._command[0]} not found in PATH", now)
        try:
            result = subprocess.run(self._command, capture_output=True,
                                    timeout=self._timeout, check=True)
            dump = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return self._degrade(f"{self._command[0]}: {exc}", now)
        self._restore()
        return active_streams(dump if isinstance(dump, list) else [])
