"""inotify(7) bound through ctypes.

Python has no stdlib inotify and pyinotify is unmaintained; this is the whole
API we need in ~80 lines, with no third-party dependency.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import struct
from typing import NamedTuple

IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000

IN_CLOSE = IN_CLOSE_WRITE | IN_CLOSE_NOWRITE

_IN_CLOEXEC = 0o2000000
_IN_NONBLOCK = 0o0004000

# struct inotify_event { int wd; uint32 mask; uint32 cookie; uint32 len; char name[]; }
_HEADER = struct.Struct("=iIII")

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.inotify_init1.argtypes = [ctypes.c_int]
_libc.inotify_init1.restype = ctypes.c_int
_libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_libc.inotify_add_watch.restype = ctypes.c_int
_libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
_libc.inotify_rm_watch.restype = ctypes.c_int


def _check(ret: int, what: str) -> int:
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")
    return ret


class Event(NamedTuple):
    wd: int
    mask: int
    name: str  # empty when the watch is on the file itself, not a directory


class Inotify:
    """A non-blocking inotify instance; register fileno() with select/poll."""

    def __init__(self) -> None:
        self._fd = _check(_libc.inotify_init1(_IN_CLOEXEC | _IN_NONBLOCK), "inotify_init1")
        self._paths: dict[int, str] = {}

    def fileno(self) -> int:
        return self._fd

    def add(self, path: str, mask: int) -> int:
        wd = _check(_libc.inotify_add_watch(self._fd, os.fsencode(path), mask),
                    f"inotify_add_watch({path})")
        self._paths[wd] = path
        return wd

    def remove(self, wd: int) -> None:
        self._paths.pop(wd, None)
        _libc.inotify_rm_watch(self._fd, wd)  # already gone if the device vanished

    def path_of(self, wd: int) -> str | None:
        return self._paths.get(wd)

    def read(self) -> list[Event]:
        """Drain every queued event. Returns [] rather than blocking."""
        events: list[Event] = []
        while True:
            try:
                buf = os.read(self._fd, 8192)
            except BlockingIOError:
                return events
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise
            if not buf:
                return events
            offset = 0
            while offset < len(buf):
                wd, mask, _cookie, length = _HEADER.unpack_from(buf, offset)
                offset += _HEADER.size
                name = buf[offset:offset + length].split(b"\0", 1)[0].decode(errors="replace")
                offset += length
                if mask & IN_IGNORED:
                    self._paths.pop(wd, None)
                events.append(Event(wd, mask, name))

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
