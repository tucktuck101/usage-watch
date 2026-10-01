"""Configuration and where usage-watch keeps its files. Everything works
without a config file.

    [defaults]
    interval = 300          # seconds between scans

A config file written by an earlier version may still hold nudge settings
(`[defaults] nudge`, `[accounts.*]`, `[roles.*]`, `[[override]]`). Nudging
was removed, so those keys are ignored without complaint.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import Problem


def config_path() -> Path:
    if os.environ.get("USAGE_WATCH_CONFIG"):
        return Path(os.environ["USAGE_WATCH_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "usage-watch" / "config.toml"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "usage-watch"


@dataclass
class Config:
    interval: int = 300
    path: Path | None = None
    exists: bool = False


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise Problem(f"{path} is not valid TOML: {e}",
                      fix="correct the file, or move it aside: usage-watch works without one") from None
    d = raw.get("defaults", {})
    return Config(interval=int(d.get("interval", 300)), path=path, exists=True)
