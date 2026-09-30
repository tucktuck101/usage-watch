"""The store (D3): SQLite from the standard library, WAL mode.

`connect` opens it, `migrate` brings it to the schema this code expects
(collector runtime only, D3), and `schema_status` tells a reader whether
it may read.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..errors import Problem
from .migrations import MIGRATIONS
from .ownership import TABLE_OWNERS

__all__ = [
    "SCHEMA_VERSION", "TABLE_OWNERS", "SchemaStatus",
    "connect", "default_path", "migrate", "schema_status", "schema_version",
]

SCHEMA_VERSION = len(MIGRATIONS)
BUSY_TIMEOUT_MS = 5000

OLDER_MESSAGE = "the collector hasn't migrated yet"
NEWER_MESSAGE = "upgrade usage-watch"


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "usage-watch" / "usage.db"


def connect(path: str | os.PathLike | None = None, readonly: bool = False) -> sqlite3.Connection:
    """Open the store. Transactions are explicit: the connection is in
    autocommit mode (isolation_level None), so every write says BEGIN."""
    path = Path(path) if path is not None else default_path()
    if readonly:
        if not path.exists():
            raise Problem(
                f"no usage-watch store at {path}",
                "start a collector with: usage-watch run --no-nudge",
                expected="a store written by the collector runtime",
            )
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        # WAL is persistent in the file, so a read-only open inherits it.
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The store's schema version; 0 for a store with no schema yet."""
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return row[0] if row else 0


@dataclass(frozen=True)
class SchemaStatus:
    state: Literal["older", "equal", "newer"]
    found: int
    expected: int
    message: str | None  # None when equal

    @property
    def readable(self) -> bool:
        return self.state == "equal"


def schema_status(conn: sqlite3.Connection, expected: int = SCHEMA_VERSION) -> SchemaStatus:
    """Read-only: compare the store's schema with the one this code expects (D3).
    Older: the reader waits ("the collector hasn't migrated yet").
    Newer: the reader exits with a prompt to upgrade."""
    found = schema_version(conn)
    if found < expected:
        return SchemaStatus("older", found, expected,
                            f"{OLDER_MESSAGE} (store schema v{found}, expected v{expected})")
    if found > expected:
        return SchemaStatus("newer", found, expected,
                            f"the store's schema v{found} is newer than this usage-watch "
                            f"knows (v{expected}): {NEWER_MESSAGE}")
    return SchemaStatus("equal", found, expected, None)


def _db_file(conn: sqlite3.Connection) -> str:
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file
    return ""


def _backup(conn: sqlite3.Connection, found: int) -> Path | None:
    """Copy a non-empty store before migrating it, via SQLite's backup API
    so the WAL's contents are included."""
    file = _db_file(conn)
    if not file:  # in-memory or temporary database: nothing on disk to protect
        return None
    empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    if empty:
        return None
    dest = Path(f"{file}.v{found}.{int(time.time() * 1000)}.bak")
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending forward-only migrations; return the resulting version.

    Run only by the collector runtime, while it holds the lock (D3). Each
    migration runs in its own transaction with its version record; a backup
    copy is taken first when the store is non-empty. A no-op when current.
    """
    found = schema_version(conn)
    if found > SCHEMA_VERSION:
        raise Problem(
            f"the store's schema v{found} is newer than this usage-watch knows (v{SCHEMA_VERSION})",
            NEWER_MESSAGE,
            expected=f"schema v{SCHEMA_VERSION} or older",
        )
    if found == SCHEMA_VERSION:
        return found
    _backup(conn, found)
    for number in range(found + 1, SCHEMA_VERSION + 1):
        now = int(time.time() * 1000)
        script = (
            "BEGIN IMMEDIATE;\n"
            + MIGRATIONS[number - 1]
            + "\nINSERT INTO schema_version (id, version, migrated_at)"
            + f" VALUES (1, {number}, {now})"
            + " ON CONFLICT (id) DO UPDATE SET version = excluded.version,"
            + " migrated_at = excluded.migrated_at;\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return SCHEMA_VERSION
