"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__
from .camera import CameraWatcher
from .config import ConfigError, load, search_paths
from .daemon import Daemon
from .inotify import Inotify
from .mic import MicWatcher

log = logging.getLogger("tallyd")

# sd-daemon(3) log level prefixes, so journald files our lines at the right
# priority instead of flattening everything to "info".
_JOURNAL_PREFIX = {
    logging.CRITICAL: "<2>", logging.ERROR: "<3>", logging.WARNING: "<4>",
    logging.INFO: "<6>", logging.DEBUG: "<7>",
}


class _JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _JOURNAL_PREFIX.get(record.levelno, "<6>") + super().format(record)


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler()
    under_systemd = bool(os.environ.get("JOURNAL_STREAM"))
    formatter_class = _JournalFormatter if under_systemd else logging.Formatter
    handler.setFormatter(formatter_class(
        "%(message)s" if under_systemd else "%(asctime)s %(levelname)-7s %(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        handlers=[handler], force=True)


def _check() -> int:
    """Print the current detection state as JSON and exit. For debugging and
    for status bars that want a one-shot answer."""
    inotify = Inotify()
    try:
        mic, camera = MicWatcher(), CameraWatcher(inotify)
        sources = mic.active() + camera.active()
        report = {
            "in_call": bool(sources),
            "sources": sources,
            # Surfaces a degraded, camera-only daemon to status bars and to
            # whoever is wondering why an audio-only call went unnoticed.
            "microphone_available": mic.available,
            "cameras": sorted(camera.devices),
        }
    finally:
        inotify.close()
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tallyd",
        description="Detect ongoing calls by watching microphone and camera capture.")
    parser.add_argument("-c", "--config", metavar="PATH",
                        help="configuration file (default: first of %s)"
                             % ", ".join(str(p) for p in search_paths()))
    parser.add_argument("--check", action="store_true",
                        help="report the current state as JSON and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    if args.check:
        if not args.verbose:  # device discovery is noise in a one-shot query
            logging.disable(logging.INFO)
        return _check()

    try:
        from pathlib import Path
        config = load(Path(args.config) if args.config else None)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if config.source:
        log.info("configuration from %s", config.source)
    else:
        log.info("no configuration file found; logging transitions only")
    if not (config.on_call_start or config.on_call_end):
        log.warning("no actions configured; transitions will only be logged")

    try:
        return Daemon(config).run()
    except OSError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
