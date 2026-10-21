"""Camera-in-use detection.

PipeWire is deliberately *not* the source of truth here. Chromium/Electron
(Teams, Slack, Discord), Zoom and stock Firefox open /dev/video* directly, and
stay invisible in the PipeWire graph while doing so. So:

    inotify on /dev/video*  ->  tells us *when* to look  (instant, unprivileged)
    /proc/<pid>/fd scan     ->  tells us *what is true*  (race-free, names the app)

Using the scan as truth rather than counting open/close events means a lost or
overflowed inotify event cannot desynchronise the state.
"""

from __future__ import annotations

import fcntl
import glob
import logging
import os
import struct

from .inotify import IN_CLOSE, IN_CREATE, IN_DELETE, IN_OPEN, Inotify

log = logging.getLogger(__name__)

DEV_DIR = "/dev"
_DEV_GLOB = "video*"

# VIDIOC_QUERYCAP = _IOR('V', 0, struct v4l2_capability); the struct is 104 bytes.
_VIDIOC_QUERYCAP = 0x80685600
_QUERYCAP_SIZE = 104
_CAPS_OFFSET = 84  # driver[16] + card[32] + bus_info[32] + version[4]

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_CAPTURE_MPLANE = 0x00001000
V4L2_CAP_DEVICE_CAPS = 0x80000000

_CAPTURE_MASK = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_VIDEO_CAPTURE_MPLANE


def capture_caps(buf: bytes) -> bool:
    """True if a struct v4l2_capability describes a video *capture* device."""
    capabilities, device_caps = struct.unpack_from("=II", buf, _CAPS_OFFSET)
    effective = device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities
    return bool(effective & _CAPTURE_MASK)


def is_capture_device(path: str) -> bool:
    """Ask the kernel whether `path` can capture video.

    Without this, /dev/video1 and /dev/video3 (uvcvideo *metadata* nodes) and
    /dev/video10 (a v4l2loopback output node) would all count as webcams on a
    stock Arch laptop -- and hardware m2m decoders would report "camera on"
    during ordinary video playback.
    """
    buf = bytearray(_QUERYCAP_SIZE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        log.debug("cannot open %s: %s", path, exc)
        return False
    try:
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, buf)
    except OSError as exc:
        log.debug("VIDIOC_QUERYCAP on %s failed: %s", path, exc)
        return False
    finally:
        os.close(fd)
    return capture_caps(buf)


def _comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return "?"


def open_by(devices: set[str], skip_pid: int) -> dict[str, set[str]]:
    """Map each device in `devices` to the processes holding it open.

    Only processes owned by this user are visible; a camera opened by root or
    another user is not detected. That is the normal case for a desktop call.
    """
    users: dict[str, set[str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == skip_pid:
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue  # process exited, or not ours
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if target in devices:
                users.setdefault(target, set()).add(f"{_comm(entry)}[{entry}]")
    return users


class CameraWatcher:
    """Watches every V4L2 capture device, including ones hotplugged later."""

    def __init__(self, inotify: Inotify, dev_dir: str = DEV_DIR) -> None:
        self._inotify = inotify
        self._dev_dir = dev_dir
        self._pid = os.getpid()
        self._watches: dict[str, int] = {}
        # Hotplug: a USB webcam appearing or vanishing shows up here.
        self._dir_wd = inotify.add(dev_dir, IN_CREATE | IN_DELETE)
        self.rescan_devices()

    @property
    def devices(self) -> set[str]:
        return set(self._watches)

    def handles(self, event_wd: int) -> bool:
        return event_wd == self._dir_wd or event_wd in self._watches.values()

    def is_device_event(self, name: str) -> bool:
        return name.startswith("video")

    def rescan_devices(self) -> None:
        """Sync inotify watches with the capture devices that exist right now."""
        present = set(glob.glob(os.path.join(self._dev_dir, _DEV_GLOB)))
        for path in sorted(present - set(self._watches)):
            # Query caps *before* watching, so our own open() cannot generate an
            # event that would send us straight back here in a loop.
            if not is_capture_device(path):
                continue
            try:
                self._watches[path] = self._inotify.add(path, IN_OPEN | IN_CLOSE)
            except OSError as exc:
                log.warning("cannot watch %s: %s", path, exc)
                continue
            log.info("watching camera %s", path)
        for path in sorted(set(self._watches) - present):
            self._inotify.remove(self._watches.pop(path))
            log.info("camera %s went away", path)

    def active(self) -> list[str]:
        """Labels for every capture device currently held open, e.g.
        ``camera:/dev/video0(firefox[1234])``."""
        if not self._watches:
            return []
        in_use = open_by(self.devices, self._pid)
        return [
            f"camera:{device}({','.join(sorted(procs))})"
            for device, procs in sorted(in_use.items())
        ]
