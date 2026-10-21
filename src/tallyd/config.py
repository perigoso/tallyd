"""Configuration loading (TOML, stdlib `tomllib`)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "tallyd/tallyd.conf"
SYSTEM_CONFIG = Path("/etc") / CONFIG_NAME


class ConfigError(Exception):
    """Raised for a malformed or unusable configuration file."""


@dataclass(frozen=True)
class Config:
    poll_interval: float = 2.0
    dwell: float = 10.0
    mic_retry_interval: float = 30.0
    mic_failures_before_backoff: int = 3
    on_call_start: str = ""
    on_call_end: str = ""
    source: Path | None = None


def search_paths() -> list[Path]:
    """User config first, then the packaged system default."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    user_dir = Path(xdg) if xdg else Path.home() / ".config"
    return [user_dir / CONFIG_NAME, SYSTEM_CONFIG]


def _number(table: dict, key: str, default: float, *, minimum: float) -> float:
    value = table.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return float(value)


def _count(table: dict, key: str, default: int) -> int:
    value = _number(table, key, default, minimum=1)
    if value != int(value):
        raise ConfigError(f"{key} must be a whole number, got {value}")
    return int(value)


def _command(table: dict, key: str) -> str:
    value = table.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a shell command string, got {value!r}")
    return value


def from_mapping(data: dict, source: Path | None = None) -> Config:
    detection = data.get("detection", {})
    actions = data.get("actions", {})
    for name, table in (("detection", detection), ("actions", actions)):
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
    return Config(
        # A zero interval would spin the loop; a zero dwell is legitimate
        # (fire the end action the instant capture stops).
        poll_interval=_number(detection, "poll_interval", 2.0, minimum=0.1),
        dwell=_number(detection, "dwell", 10.0, minimum=0.0),
        # 0 disables the backoff: a degraded microphone is then re-probed at
        # every poll.
        mic_retry_interval=_number(detection, "mic_retry_interval", 30.0, minimum=0.0),
        mic_failures_before_backoff=_count(detection, "mic_failures_before_backoff", 3),
        on_call_start=_command(actions, "on_call_start"),
        on_call_end=_command(actions, "on_call_end"),
        source=source,
    )


def load(path: Path | None = None) -> Config:
    """Load `path`, or the first file found in search_paths().

    Running with no configuration at all is valid and useful: the daemon then
    just logs call transitions, which is how you verify detection before
    wiring destructive actions to it.
    """
    candidates = [path] if path is not None else search_paths()
    for candidate in candidates:
        try:
            raw = candidate.read_bytes()
        except FileNotFoundError:
            if path is not None:
                raise ConfigError(f"no such configuration file: {candidate}") from None
            continue
        except OSError as exc:
            raise ConfigError(f"cannot read {candidate}: {exc}") from None
        try:
            data = tomllib.loads(raw.decode())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{candidate}: {exc}") from None
        return from_mapping(data, source=candidate)
    return Config()
