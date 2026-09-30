"""Is a collector running? (D3)

The lock is authoritative; the heartbeat is the second signal:

| lock     | heartbeat               | state   |
|----------|-------------------------|---------|
| held     | under 30 s old          | running |
| held     | 30 s or older, missing  | stalled |
| not held | any                     | none    |

`collector_status` gives the data's age, never liveness. Everything here is
read-only: a non-blocking shared lock attempt, released at once, and a
read-only connection.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import store
from ..errors import Problem
from .core import default_lock_path

__all__ = ["STALE_S", "START_HINT", "Liveness", "liveness", "lock_held"]

STALE_S = 30.0
START_HINT = "usage-watch run --no-nudge"

State = Literal["running", "stalled", "none"]


def lock_held(path: str | os.PathLike) -> bool:
    """True when another open file holds `path` exclusively. Never waits,
    never creates the file."""
    try:
        f = open(path, "r")
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    finally:
        f.close()


def _clock_text(ms: int, now_ms: int) -> str:
    """D3's reader times: `14:02:31` today, `yesterday 18:40`, else a date."""
    at = dt.datetime.fromtimestamp(ms / 1000)
    today = dt.datetime.fromtimestamp(now_ms / 1000).date()
    if at.date() == today:
        return at.strftime("%H:%M:%S")
    if at.date() == today - dt.timedelta(days=1):
        return at.strftime("yesterday %H:%M")
    return at.strftime("%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Liveness:
    state: State
    now_ms: int
    heartbeat_age_s: float | None = None  # None: no heartbeat read
    heartbeat_at: int | None = None
    pid: int | None = None
    data_as_of: int | None = None         # the latest collector_status.last_success_at, UTC ms
    schema_message: str | None = None     # set when the store's schema isn't this code's

    def message(self) -> str:
        if self.schema_message is not None:
            return self.schema_message
        data = ("data as of " + _clock_text(self.data_as_of, self.now_ms)
                if self.data_as_of is not None else "no data yet")
        if self.state == "running":
            return f"collector running, {data}"
        if self.state == "stalled":
            if self.heartbeat_at is None:
                return f"collector stalled (no heartbeat); {data}"
            return (f"collector stalled (pid {self.pid}, last heartbeat "
                    f"{_clock_text(self.heartbeat_at, self.now_ms)}); {data}")
        return f"no collector running; {data}; start one with: {START_HINT}"


def liveness(db_path: str | os.PathLike | None = None, lock_path: str | os.PathLike | None = None,
             now: float | None = None) -> Liveness:
    """Combine the lock and the heartbeat (D3). `now` is epoch seconds."""
    now_ms = int((time.time() if now is None else now) * 1000)
    db = Path(db_path) if db_path is not None else store.default_path()
    lock = Path(lock_path) if lock_path is not None else default_lock_path(db_path)
    held = lock_held(lock)

    try:
        conn = store.connect(db, readonly=True)
    except (Problem, sqlite3.Error):
        return Liveness("stalled" if held else "none", now_ms)
    try:
        status = store.schema_status(conn)
        if not status.readable:
            # D3: an older schema means the collector hasn't migrated yet: don't read.
            return Liveness("stalled" if held else "none", now_ms, schema_message=status.message)
        row = conn.execute(
            "SELECT pid, heartbeat_at FROM runtime ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        data_as_of = conn.execute("SELECT max(last_success_at) FROM collector_status").fetchone()[0]
    finally:
        conn.close()

    pid, heartbeat_at = row if row else (None, None)
    age = (now_ms - heartbeat_at) / 1000 if heartbeat_at is not None else None
    if not held:
        state: State = "none"
    elif age is not None and age < STALE_S:
        state = "running"
    else:
        state = "stalled"
    return Liveness(state, now_ms, age, heartbeat_at, pid, data_as_of)
