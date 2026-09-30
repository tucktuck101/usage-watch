"""The collector runtime core (D3, D6): the two source contracts, the one
write path, the lock, the heartbeat and the watermarks.

Every data write goes through `WritePath.apply`, one transaction per batch,
on the runtime's own thread and connection. Push sources hand their items to
a thread-safe queue that the runtime drains; they never write themselves.

The runtime makes no network calls. Sources are passed in.

Readings of the design that it leaves open are marked "Reading:".
"""

from __future__ import annotations

import dataclasses
import fcntl
import importlib
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import __version__, model, store
from ..config import state_dir
from ..errors import Problem

__all__ = [
    "HEARTBEAT_S", "KEEP_PAST_RUNS", "PullSource", "PushSource", "Runtime", "WritePath",
    "acquire_lock", "default_lock_path",
]

HEARTBEAT_S = 10.0
KEEP_PAST_RUNS = 20  # D3: `runtime` keeps the current run plus the last 20
LOCK_FILE = "run.lock"

# Reading: the watermarks table is keyed (collector, file) with a path, an
# inode and an offset per file (D6), but the pull contract passes one opaque
# `str | None`. The opaque watermark is kept in one row per collector with
# file '' and the string in `path`. A per-file split belongs to the tail
# sources, which don't exist yet.
WATERMARK_FILE = ""

_UNCHANGED = object()


# --- The two contracts (D6) ------------------------------------------------------

class PullSource(Protocol):
    name: str
    primary: bool          # its usage observations are the primary counting source
    interval_s: float
    merge: Callable | None  # counting rule, passed to upsert_observation

    def collect(self, watermark: str | None) -> tuple[list, str | None]: ...


class PushSource(Protocol):
    name: str
    primary: bool
    merge: Callable | None

    def start(self, sink: Callable[[list], None]) -> None: ...

    def stop(self) -> None: ...


# --- Collaborators, looked up at call time -------------------------------------------

def _op(module: str, name: str) -> Callable:
    """reconcile.py and attribution.py are separate modules; looking them up
    per call keeps this module importable without them, and lets tests
    replace them."""
    return getattr(importlib.import_module(f"{__package__}.{module}"), name)


def _now_ms(clock: Callable[[], float]) -> int:
    return int(clock() * 1000)


# --- Insert-or-update by identity (D3) ------------------------------------------------

# item type -> (table, identity columns, surrogate id field or None)
_TABLES: dict[type, tuple[str, tuple[str, ...], str | None]] = {
    model.Session: ("sessions", ("session_key",), None),
    model.CapacitySample: ("capacity_samples", ("source", "stream_key", "window", "observed_at"),
                           "capacity_sample_id"),
    model.LimitEvent: ("limit_events", ("source", "stream_key", "source_key"), "limit_event_id"),
    model.AgentStateSample: ("state_samples", ("pane", "observed_at"), None),
    model.ContextEvent: ("context_events", ("context_event_id",), "context_event_id"),
    model.CostEvent: ("cost_events", ("scope_kind", "scope_id", "source", "basis", "price_version"),
                      None),
    model.Checkout: ("checkouts", ("checkout_id",), None),
}

# Columns a later record may clear or must overwrite, rather than keep when absent.
_NOT_NULL_UPDATES = {"live"}


def _q(col: str) -> str:
    return f'"{col}"'


def _upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any],
            identity: tuple[str, ...]) -> None:
    """Insert a row, or update the one with the same identity.

    Reading: a repeat that leaves a nullable field unstated (None) keeps the
    stored value, so a later, thinner record never erases what an earlier
    one said.
    """
    row = {k: (int(v) if isinstance(v, bool) else v) for k, v in row.items()}
    cols = list(row)
    updates = [c for c in cols if c not in identity]
    sets = ", ".join(
        f"{_q(c)} = excluded.{_q(c)}" if c in _NOT_NULL_UPDATES
        else f"{_q(c)} = COALESCE(excluded.{_q(c)}, {table}.{_q(c)})"
        for c in updates
    )
    sql = (f"INSERT INTO {table} ({', '.join(map(_q, cols))})"
           f" VALUES ({', '.join('?' for _ in cols)})")
    if sets:
        sql += f" ON CONFLICT ({', '.join(map(_q, identity))}) DO UPDATE SET {sets}"
    else:
        sql += " ON CONFLICT DO NOTHING"
    conn.execute(sql, [row[c] for c in cols])


def _write_record(conn: sqlite3.Connection, item: Any) -> None:
    table, identity, surrogate = _TABLES[type(item)]
    row = dataclasses.asdict(item)
    if surrogate is not None and row.get(surrogate) is None:
        row.pop(surrogate)
        if identity == (surrogate,):  # no natural identity (context_events): always a new row
            conn.execute(
                f"INSERT INTO {table} ({', '.join(map(_q, row))}) VALUES ({', '.join('?' for _ in row)})",
                list(row.values()))
            return
    _upsert(conn, table, row, identity)


# --- Errors without values (D5) -----------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONSTRAINT = re.compile(r"constraint failed: (.*)$")


class _At(Exception):
    """Wraps a failure with where it happened: a path, never a value."""

    def __init__(self, path: str, exc: BaseException, item: Any = None):
        super().__init__(path)
        self.path = path
        self.exc = exc
        self.item = item


def _field_names(item: Any) -> set[str]:
    return {f.name for f in dataclasses.fields(item)} if dataclasses.is_dataclass(item) else set()


def _error_text(err: _At) -> str:
    """`<ExceptionType> at <path>[.<field>]`. The field comes from an
    exception's own `field` attribute, or from SQLite's constraint message,
    and is kept only when it names a field of the item. Exception messages
    are never copied: they can carry values."""
    exc = err.exc
    fields: list[str] = []
    own = getattr(exc, "field", None)
    if isinstance(own, str) and _IDENT.fullmatch(own):
        fields = [own]
    elif isinstance(exc, sqlite3.Error):
        m = _CONSTRAINT.search(str(exc))
        if m:
            known = _field_names(err.item)
            for name in _IDENT.findall(m.group(1)):
                if name in known and name not in fields:
                    fields.append(name)
    where = err.path + (f".{','.join(fields)}" if fields else "")
    return f"{type(exc).__name__} at {where}"


# --- The write path (D6) -----------------------------------------------------------------

class WritePath:
    """One write path for every source: one transaction per batch.

    Dispatch by item type; then `collector_status`. A failing batch is rolled
    back whole and reported as the exception type and field path, never
    values; it never raises past the runtime.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def apply(self, source: Any, items: list, *, watermark: Any = _UNCHANGED) -> bool:
        """Write `items` from `source`, and its new `watermark` if given, in
        one transaction. Return True on success."""
        conn = self.conn
        name = source.name
        now = _now_ms(self.clock)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for i, item in enumerate(items):
                    try:
                        self._dispatch(source, item)
                    except Exception as exc:
                        raise _At(f"items[{i}]:{type(item).__name__}", exc, item) from exc
                if watermark is not _UNCHANGED and watermark is not None:
                    try:
                        _store_watermark(conn, name, watermark)
                    except Exception as exc:
                        raise _At("watermark", exc) from exc
                try:
                    conn.execute(
                        "INSERT INTO collector_status (collector, last_run_at, last_success_at, last_new_at,"
                        " last_error) VALUES (?, ?, ?, ?, NULL)"
                        " ON CONFLICT (collector) DO UPDATE SET last_run_at = excluded.last_run_at,"
                        " last_success_at = excluded.last_success_at,"
                        " last_new_at = COALESCE(excluded.last_new_at, collector_status.last_new_at),"
                        " last_error = NULL",
                        (name, now, now, now if items else None))
                except Exception as exc:
                    raise _At("collector_status", exc) from exc
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        except _At as err:
            self.record_error(name, _error_text(err))
            return False
        except Exception as exc:  # BEGIN or COMMIT itself failed (e.g. busy)
            self.record_error(name, f"{type(exc).__name__} at transaction")
            return False
        return True

    def record_error(self, name: str, text: str) -> None:
        """Record a failed run in its own short transaction. Never raises."""
        now = _now_ms(self.clock)
        try:
            self.conn.execute(
                "INSERT INTO collector_status (collector, last_run_at, last_error) VALUES (?, ?, ?)"
                " ON CONFLICT (collector) DO UPDATE SET last_run_at = excluded.last_run_at,"
                " last_error = excluded.last_error",
                (name, now, text))
        except Exception:
            pass

    def _dispatch(self, source: Any, item: Any) -> None:
        conn = self.conn
        if isinstance(item, model.UsageObservation):
            observation_id = _op("reconcile", "upsert_observation")(
                conn, item, getattr(source, "merge", None))
            _op("reconcile", "reconcile_observation")(
                conn, observation_id, primary=bool(getattr(source, "primary", False)))
        elif isinstance(item, model.AttributionEvidence):
            item = _resolve_account_alias(conn, item)
            _op("attribution", "add_evidence")(conn, item)
            # Resolved at the evidence's first_observed_at, session subjects
            # included; resolve stores the effective row itself.
            _op("attribution", "resolve")(
                conn, item.subject_kind, item.subject_id, item.dimension, item.first_observed_at)
        elif type(item) in _TABLES:
            _write_record(conn, item)
        else:
            raise TypeError("unknown item type")


ALIAS_PREFIX = "alias:"


def account_alias_value(provider: str, alias_kind: str, alias_hash: str) -> str:
    """The value a collector puts on `account` evidence. Collectors see only an
    alias (already keyed-hashed); the write path turns it into the canonical
    account key through the registry (D7), since collectors never touch the store."""
    return f"{ALIAS_PREFIX}{provider}:{alias_kind}:{alias_hash}"


def _resolve_account_alias(conn: sqlite3.Connection, ev: "model.AttributionEvidence"):
    if ev.dimension != "account" or not ev.value.startswith(ALIAS_PREFIX):
        return ev
    provider, alias_kind, alias_hash = ev.value[len(ALIAS_PREFIX):].split(":", 2)
    account_key = _op("accounts", "assert_alias")(
        conn, provider=provider, alias_kind=alias_kind, alias_hash=alias_hash,
        asserted_by=ev.source, evidence="reported", confidence=ev.confidence)
    return dataclasses.replace(ev, value=account_key)


# --- Watermarks (D6) ---------------------------------------------------------------------

def _store_watermark(conn: sqlite3.Connection, collector: str, watermark: str) -> None:
    if not isinstance(watermark, str):
        raise TypeError("watermark must be a string")
    conn.execute(
        "INSERT INTO watermarks (collector, file, path, inode, byte_offset) VALUES (?, ?, ?, NULL, 0)"
        " ON CONFLICT (collector, file) DO UPDATE SET path = excluded.path",
        (collector, WATERMARK_FILE, watermark))


def load_watermark(conn: sqlite3.Connection, collector: str) -> str | None:
    row = conn.execute("SELECT path FROM watermarks WHERE collector = ? AND file = ?",
                       (collector, WATERMARK_FILE)).fetchone()
    return row[0] if row else None


# --- The lock (D3) -------------------------------------------------------------------------

def default_lock_path(db_path: str | os.PathLike | None = None) -> Path:
    """`run.lock` in the state directory: the store's directory when a store
    path is given, else the same file `watcher.acquire_lock` uses."""
    if db_path is not None:
        return Path(db_path).parent / LOCK_FILE
    return state_dir() / LOCK_FILE


def acquire_lock(path: str | os.PathLike):
    """Take the exclusive, non-blocking lock on `path`; return the open file,
    which holds it until closed. Raise Problem when another process holds it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "unknown"
        f.close()
        raise Problem(
            f"another collector runtime is already running (pid {holder})",
            expected="one collector runtime per machine, holding run.lock",
            fix=f"use the running one, or stop it first: kill {holder}",
        ) from None
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


# --- The runtime (D3, D6) --------------------------------------------------------------------

def _is_push(source: Any) -> bool:
    return callable(getattr(source, "start", None)) and not callable(getattr(source, "collect", None))


class Runtime:
    """Hosts the collector runtime: holds the lock, migrates, heartbeats,
    polls pull sources, drains push sources, and writes through one path.

    `start`, `run_once`, `run_forever` and `stop` are called on one thread,
    the runtime's own; every data write happens there, on its connection.
    """

    def __init__(self, sources: list, db_path: str | os.PathLike | None = None,
                 lock_path: str | os.PathLike | None = None,
                 clock: Callable[[], float] = time.time, heartbeat_s: float = HEARTBEAT_S):
        self.sources = list(sources)
        self.pull = [s for s in self.sources if not _is_push(s)]
        self.push = [s for s in self.sources if _is_push(s)]
        self.db_path = Path(db_path) if db_path is not None else store.default_path()
        self.lock_path = Path(lock_path) if lock_path is not None else default_lock_path(db_path)
        self.clock = clock
        self.heartbeat_s = heartbeat_s
        self.runtime_id: int | None = None
        self.conn: sqlite3.Connection | None = None
        self.write: WritePath | None = None
        self._lock = None
        self._queue: queue.Queue = queue.Queue()
        self._next_due: dict[str, float] = {}
        self._started_push: list = []
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # -- life cycle --

    def start(self) -> None:
        self._lock = acquire_lock(self.lock_path)
        try:
            self.conn = store.connect(self.db_path)
            store.migrate(self.conn)  # D3: only here, while holding the lock
            self.write = WritePath(self.conn, self.clock)
            self._insert_runtime_row()
            self._start_heartbeat()
            for source in self.push:
                self._start_push(source)
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        """Stop push sources, drain what they handed over, stop the
        heartbeat, close the store and release the lock. Safe to repeat."""
        for source in self._started_push:
            try:
                source.stop()
            except Exception as exc:
                if self.write is not None:
                    self.write.record_error(source.name, f"{type(exc).__name__} at stop")
        self._started_push = []
        if self.write is not None:
            self._drain()
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join()
            self._hb_thread = None
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            self.write = None
        if self._lock is not None:
            self._lock.close()  # closing the file releases the flock
            self._lock = None

    # -- work --

    def run_once(self) -> None:
        """Poll every due pull source, then drain the push queue."""
        assert self.write is not None, "start() the runtime first"
        now = self.clock()
        for source in self.pull:
            if now < self._next_due.get(source.name, 0.0):
                continue
            self._next_due[source.name] = now + float(source.interval_s)
            self._poll(source)
        self._drain()

    def run_forever(self, stop_event: threading.Event, tick_s: float = 0.2) -> None:
        """Run until `stop_event` is set. The caller then calls `stop()`."""
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(tick_s)

    def _poll(self, source: Any) -> None:
        try:
            watermark = load_watermark(self.conn, source.name)
            items, new_watermark = source.collect(watermark)
            items = list(items or [])
        except Exception as exc:
            # D6: a failing source is reported and retried; it never stops the others.
            self.write.record_error(source.name, f"{type(exc).__name__} at collect")
            return
        self.write.apply(source, items, watermark=new_watermark)

    def _drain(self) -> None:
        while True:
            try:
                source, items = self._queue.get_nowait()
            except queue.Empty:
                return
            self.write.apply(source, items)

    def _start_push(self, source: Any) -> None:
        def sink(items: list, _source=source) -> None:
            self._queue.put((_source, list(items)))
        try:
            source.start(sink)
        except Exception as exc:
            self.write.record_error(source.name, f"{type(exc).__name__} at start")
            return
        self._started_push.append(source)

    # -- the runtime row and heartbeat (D3) --

    def _insert_runtime_row(self) -> None:
        # Reading: D3 asks for a random runtime_id, and the schema makes it an
        # INTEGER PRIMARY KEY, so it is 56 random bits (14 hex digits) as an integer.
        self.runtime_id = int(secrets.token_hex(7), 16)
        now = _now_ms(self.clock)
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO runtime (runtime_id, pid, started_at, heartbeat_at, version)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.runtime_id, os.getpid(), now, now, __version__))
            conn.execute(
                "DELETE FROM runtime WHERE runtime_id <> ?1 AND runtime_id NOT IN"
                " (SELECT runtime_id FROM runtime WHERE runtime_id <> ?1"
                "  ORDER BY started_at DESC, rowid DESC LIMIT ?2)",
                (self.runtime_id, KEEP_PAST_RUNS))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _start_heartbeat(self) -> None:
        # Reading: the heartbeat runs on its own thread so it proves the
        # process alive between polls. It needs its own connection (SQLite
        # connections are single-thread) and writes only this run's `runtime`
        # row, a table the collector runtime owns.
        self._hb_stop.clear()
        db_path = self.db_path
        runtime_id, clock, interval, stop = self.runtime_id, self.clock, self.heartbeat_s, self._hb_stop

        def beat() -> None:
            conn = store.connect(db_path)
            try:
                while not stop.wait(interval):
                    try:
                        conn.execute("UPDATE runtime SET heartbeat_at = ? WHERE runtime_id = ?",
                                     (_now_ms(clock), runtime_id))
                    except sqlite3.Error:
                        pass  # a missed beat reads as stalled, which is the truth
            finally:
                conn.close()

        self._hb_thread = threading.Thread(target=beat, name="usage-watch-heartbeat", daemon=True)
        self._hb_thread.start()
