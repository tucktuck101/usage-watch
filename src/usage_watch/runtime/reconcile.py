"""The reconciler (D1 "Usage observation", "Usage event"; D6 "Reconciliation").

Turns stored usage observations into canonical usage events:
- a primary observation creates its event once and updates it in place
  thereafter (invariants 10, 11, 12);
- a secondary observation links to a primary one only through the same
  non-null `provider_request_key`, as `supporting` evidence, or stays an
  orphan that never counts (invariant 9);
- link state and orphan counts are derived by query, never stored.

Nothing here opens, commits or rolls back a transaction: the write path owns
it (D6), so every function is safe inside one.

Readings of the design that it leaves open are marked "Reading:".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Callable

from usage_watch import model

__all__ = [
    "CURRENT_RECONCILE_VERSION", "TOKEN_FIELDS",
    "add_metadata_link", "link_state", "orphan_counts", "reconcile_observation",
    "reprocess_outdated", "upsert_observation",
]

CURRENT_RECONCILE_VERSION: int = 1

TOKEN_FIELDS: tuple[str, ...] = (
    "uncached_input_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)

# Every stored column of usage_observations except observation_id, in order.
_OBS_COLUMNS: tuple[str, ...] = (
    "source", "stream_key", "source_request_key", "provider_request_key", "confidence",
    "parser_version", "observed_at", "harness", "provider", "model", "session_key",
    *TOKEN_FIELDS, "native", "auxiliary",
)
_IDENTITY = ("source", "stream_key", "source_request_key")

Merge = Callable[[model.UsageObservation, model.UsageObservation], model.UsageObservation]


# --- Observations ---------------------------------------------------------------

def _row_values(obs: model.UsageObservation) -> tuple:
    return tuple(
        int(bool(obs.auxiliary)) if col == "auxiliary" else getattr(obs, col)
        for col in _OBS_COLUMNS
    )


def _load_observation(conn: sqlite3.Connection, observation_id: int) -> model.UsageObservation:
    row = conn.execute(
        f"SELECT {', '.join(_OBS_COLUMNS)} FROM usage_observations WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"reconcile: no usage observation with observation_id {observation_id}")
    fields = dict(zip(_OBS_COLUMNS, row))
    fields["auxiliary"] = bool(fields["auxiliary"])
    return model.UsageObservation(observation_id=observation_id, **fields)


def upsert_observation(conn: sqlite3.Connection, obs: model.UsageObservation,
                       merge: Merge | None = None) -> int:
    """Insert, or update by identity (source, stream_key, source_request_key).
    On an existing row, the stored observation becomes merge(old, new) if merge
    is given (the source's counting rule, e.g. Claude keeps the snapshot with the
    largest output_tokens), else new replaces old. Returns observation_id.

    The identity, and so `stream_key`, never changes on update (D1): whatever
    identity the merge returns, the stored row keeps its own.
    """
    row = conn.execute(
        "SELECT observation_id FROM usage_observations"
        " WHERE source = ? AND stream_key = ? AND source_request_key = ?",
        (obs.source, obs.stream_key, obs.source_request_key),
    ).fetchone()
    if row is None:
        placeholders = ", ".join("?" for _ in _OBS_COLUMNS)
        cur = conn.execute(
            f"INSERT INTO usage_observations ({', '.join(_OBS_COLUMNS)}) VALUES ({placeholders})",
            _row_values(obs),
        )
        return cur.lastrowid

    observation_id = row[0]
    old = _load_observation(conn, observation_id)
    new = merge(old, obs) if merge is not None else obs
    new = replace(new, source=old.source, stream_key=old.stream_key,
                  source_request_key=old.source_request_key, observation_id=observation_id)
    mutable = [c for c in _OBS_COLUMNS if c not in _IDENTITY]
    values = dict(zip(_OBS_COLUMNS, _row_values(new)))
    conn.execute(
        f"UPDATE usage_observations SET {', '.join(f'{c} = ?' for c in mutable)}"
        " WHERE observation_id = ?",
        (*(values[c] for c in mutable), observation_id),
    )
    return observation_id


# --- Events ---------------------------------------------------------------------

def _event_for_accounting(conn: sqlite3.Connection, observation_id: int) -> int | None:
    row = conn.execute(
        "SELECT usage_id FROM usage_events WHERE accounting_observation_id = ?",
        (observation_id,),
    ).fetchone()
    return row[0] if row else None


def _supported_event(conn: sqlite3.Connection, observation_id: int) -> int | None:
    row = conn.execute(
        "SELECT usage_id FROM event_observations"
        " WHERE observation_id = ? AND role = 'supporting'",
        (observation_id,),
    ).fetchone()
    return row[0] if row else None


def _disagreement(conn: sqlite3.Connection, usage_id: int) -> str | None:
    """The largest absolute difference per token field between the event's
    accounting observation and all its supporting observations (D1, D6).

    Reading: stored as a JSON object of {field: largest absolute difference},
    only for fields that differ, keys sorted; its largest value is "the largest
    per-field difference". A field null on either side is skipped: an unknown
    is not a 0 (D1 null arithmetic). Null when nothing differs.
    """
    acct_cols = ", ".join(f"a.{f}" for f in TOKEN_FIELDS)
    accounting = conn.execute(
        f"SELECT {acct_cols} FROM usage_events e"
        " JOIN usage_observations a ON a.observation_id = e.accounting_observation_id"
        " WHERE e.usage_id = ?",
        (usage_id,),
    ).fetchone()
    sup_cols = ", ".join(f"o.{f}" for f in TOKEN_FIELDS)
    supporting = conn.execute(
        f"SELECT {sup_cols} FROM event_observations l"
        " JOIN usage_observations o ON o.observation_id = l.observation_id"
        " WHERE l.usage_id = ? AND l.role = 'supporting'",
        (usage_id,),
    ).fetchall()
    largest: dict[str, int] = {}
    for values in supporting:
        for field, mine, theirs in zip(TOKEN_FIELDS, accounting, values):
            if mine is None or theirs is None:
                continue
            diff = abs(mine - theirs)
            if diff and diff > largest.get(field, 0):
                largest[field] = diff
    if not largest:
        return None
    return json.dumps(largest, sort_keys=True, separators=(",", ":"))


def _record_disagreement(conn: sqlite3.Connection, usage_id: int) -> None:
    conn.execute("UPDATE usage_events SET disagreement = ? WHERE usage_id = ?",
                 (_disagreement(conn, usage_id), usage_id))


def _link_supporting(conn: sqlite3.Connection, usage_id: int, observation_id: int) -> None:
    conn.execute(
        "INSERT INTO event_observations (usage_id, observation_id, role, field)"
        " VALUES (?, ?, 'supporting', ?)",
        (usage_id, observation_id, model.SUPPORTING_FIELD),
    )


def reconcile_observation(conn: sqlite3.Connection, observation_id: int, *, primary: bool) -> int | None:
    """primary=True: ensure the canonical event for this accounting observation
    (create it, or update it IN PLACE, never a new usage_id), copying token
    fields and auxiliary from the observation, and setting reconciled_version;
    then link any existing orphan secondary observations with the same non-null
    provider_request_key as 'supporting', and recompute disagreement. Returns
    usage_id.

    Reading: observed_at and session_key are copied only when the event is
    created; D1 makes them immutable afterwards. model and provider are not
    event columns (D3 schema): they are read through accounting_observation_id.

    Reading: only observations from a different source are linked, since a
    supporting observation is "a linked observation from another source" (D1).

    primary=False: if a primary observation with the same non-null
    provider_request_key has an event, add a 'supporting' link (a secondary
    observation supports at most one event) and recompute disagreement; return
    usage_id. Otherwise leave it unlinked (an orphan) and return None.
    """
    obs = _load_observation(conn, observation_id)
    if primary:
        return _reconcile_primary(conn, obs)
    return _reconcile_secondary(conn, obs)


def _reconcile_primary(conn: sqlite3.Connection, obs: model.UsageObservation) -> int:
    oid = obs.observation_id
    if _supported_event(conn, oid) is not None:
        raise ValueError(
            f"reconcile: observation {oid} supports an event as a secondary observation,"
            " so it can't also be a primary (accounting) observation"
        )
    tokens = tuple(getattr(obs, f) for f in TOKEN_FIELDS)
    usage_id = _event_for_accounting(conn, oid)
    if usage_id is None:
        cols = ("accounting_observation_id", "observed_at", "session_key", *TOKEN_FIELDS,
                "auxiliary", "reconciled_version")
        usage_id = conn.execute(
            f"INSERT INTO usage_events ({', '.join(cols)})"
            f" VALUES ({', '.join('?' for _ in cols)})",
            (oid, obs.observed_at, obs.session_key, *tokens, int(obs.auxiliary),
             CURRENT_RECONCILE_VERSION),
        ).lastrowid
    else:
        # In place: usage_id, accounting_observation_id, observed_at and
        # session_key never change (D1, invariant 12). Tokens and auxiliary
        # move together, as one unit, from the accounting observation.
        conn.execute(
            f"UPDATE usage_events SET {', '.join(f'{f} = ?' for f in TOKEN_FIELDS)},"
            " auxiliary = ?, reconciled_version = ? WHERE usage_id = ?",
            (*tokens, int(obs.auxiliary), CURRENT_RECONCILE_VERSION, usage_id),
        )

    if obs.provider_request_key is not None:
        orphans = conn.execute(
            "SELECT o.observation_id FROM usage_observations o"
            " WHERE o.provider_request_key = ? AND o.source <> ? AND o.observation_id <> ?"
            " AND NOT EXISTS (SELECT 1 FROM usage_events e"
            "                 WHERE e.accounting_observation_id = o.observation_id)"
            " AND NOT EXISTS (SELECT 1 FROM event_observations l"
            "                 WHERE l.observation_id = o.observation_id"
            "                 AND l.role = 'supporting')"
            " ORDER BY o.observation_id",
            (obs.provider_request_key, obs.source, obs.observation_id),
        ).fetchall()
        for (orphan_id,) in orphans:
            _link_supporting(conn, usage_id, orphan_id)

    _record_disagreement(conn, usage_id)
    return usage_id


def _reconcile_secondary(conn: sqlite3.Connection, obs: model.UsageObservation) -> int | None:
    oid = obs.observation_id
    if _event_for_accounting(conn, oid) is not None:
        raise ValueError(
            f"reconcile: observation {oid} is an event's accounting observation,"
            " so it can't be reconciled as a secondary observation"
        )
    usage_id = _supported_event(conn, oid)
    if usage_id is not None:
        # Already linked: it supports at most one event. Its values may have
        # changed on upsert, so the disagreement is recomputed.
        _record_disagreement(conn, usage_id)
        return usage_id
    if obs.provider_request_key is None:
        return None
    # Reading: should two primary events share one provider_request_key, the
    # lowest usage_id is chosen, so the choice is deterministic.
    row = conn.execute(
        "SELECT e.usage_id FROM usage_events e"
        " JOIN usage_observations a ON a.observation_id = e.accounting_observation_id"
        " WHERE a.provider_request_key = ? AND a.source <> ?"
        " ORDER BY e.usage_id LIMIT 1",
        (obs.provider_request_key, obs.source),
    ).fetchone()
    if row is None:
        return None
    usage_id = row[0]
    _link_supporting(conn, usage_id, oid)
    _record_disagreement(conn, usage_id)
    return usage_id


def add_metadata_link(conn: sqlite3.Connection, usage_id: int, observation_id: int, field: str) -> None:
    """Record that a linked observation supplied one named request-level
    metadata field the accounting observation lacks (D1, D6).

    Refuses a token field (token fields never come this way), an observation
    that isn't a supporting observation of this event (metadata comes "from a
    linked observation"), and a second source for the same event and field.
    Recording the same link again is a no-op.
    """
    if not field:
        raise ValueError("add_metadata_link: field is empty; a metadata link names its field")
    if field in TOKEN_FIELDS:
        raise ValueError(f"add_metadata_link: {field} is a token field; token fields come"
                         " only from the accounting observation")
    if _supported_event(conn, observation_id) != usage_id:
        raise ValueError(f"add_metadata_link: observation {observation_id} is not a supporting"
                         f" observation of event {usage_id}")
    row = conn.execute(
        "SELECT observation_id FROM event_observations"
        " WHERE usage_id = ? AND role = 'metadata' AND field = ?",
        (usage_id, field),
    ).fetchone()
    if row is not None:
        if row[0] == observation_id:
            return
        raise ValueError(f"add_metadata_link: event {usage_id} already takes {field} from"
                         f" observation {row[0]}; one metadata source per event and field")
    conn.execute(
        "INSERT INTO event_observations (usage_id, observation_id, role, field)"
        " VALUES (?, ?, 'metadata', ?)",
        (usage_id, observation_id, field),
    )


def reprocess_outdated(conn: sqlite3.Connection) -> int:
    """Reprocess, in place from their stored observations, every event whose
    reconciled_version is below the current one (D1, D6). usage_id is kept
    (invariant 12). Returns the number of events reprocessed."""
    rows = conn.execute(
        "SELECT accounting_observation_id FROM usage_events"
        " WHERE reconciled_version < ? ORDER BY usage_id",
        (CURRENT_RECONCILE_VERSION,),
    ).fetchall()
    for (observation_id,) in rows:
        reconcile_observation(conn, observation_id, primary=True)
    return len(rows)


# --- Derived link state -------------------------------------------------------------

def link_state(conn: sqlite3.Connection, observation_id: int) -> str:
    """'primary' if the observation is some event's accounting observation,
    'linked' if it supports an event, else 'orphan'. Derived, never stored (D1).

    Reading: "linked" means a supporting row (D1). A metadata row is always
    from a supporting observation, so D6's "an event_observations row" agrees.
    """
    row = conn.execute(
        "SELECT"
        " EXISTS (SELECT 1 FROM usage_events WHERE accounting_observation_id = o.observation_id),"
        " EXISTS (SELECT 1 FROM event_observations"
        "         WHERE observation_id = o.observation_id AND role = 'supporting')"
        " FROM usage_observations o WHERE o.observation_id = ?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"link_state: no usage observation with observation_id {observation_id}")
    is_primary, is_linked = row
    if is_primary:
        return "primary"
    if is_linked:
        return "linked"
    return "orphan"


def orphan_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Unlinked observations per source, counted by query (D1, D6). Sources
    with no orphans are absent."""
    rows = conn.execute(
        "SELECT o.source, count(*) FROM usage_observations o"
        " WHERE NOT EXISTS (SELECT 1 FROM usage_events e"
        "                   WHERE e.accounting_observation_id = o.observation_id)"
        " AND NOT EXISTS (SELECT 1 FROM event_observations l"
        "                 WHERE l.observation_id = o.observation_id AND l.role = 'supporting')"
        " GROUP BY o.source ORDER BY o.source"
    ).fetchall()
    return {source: count for source, count in rows}
