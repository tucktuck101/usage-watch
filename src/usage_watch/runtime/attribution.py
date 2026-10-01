"""Attribution evidence and effective attribution (D1, D2).

Every piece of evidence is kept. `resolve` applies D1's resolution rule and
stores one effective row per subject and dimension; inherited values (a
usage event falling back to its session) are computed at query time by
`effective` and never stored.

No function here opens a transaction: the caller owns them.
"""

from __future__ import annotations

import sqlite3

from usage_watch import model
from usage_watch.runtime import accounts

_EVIDENCE_COLS = (
    "evidence_id, subject_kind, subject_id, dimension, value, method, source, confidence,"
    " validity, valid_from, valid_to, first_observed_at, last_confirmed_at"
)


def _evidence(row: tuple) -> model.AttributionEvidence:
    (evidence_id, subject_kind, subject_id, dimension, value, method, source, confidence,
     validity, valid_from, valid_to, first_observed_at, last_confirmed_at) = row
    return model.AttributionEvidence(
        subject_kind=subject_kind, subject_id=subject_id, dimension=dimension, value=value,
        method=method, source=source, confidence=confidence, validity=validity,
        first_observed_at=first_observed_at, last_confirmed_at=last_confirmed_at,
        valid_from=valid_from, valid_to=valid_to, evidence_id=evidence_id,
    )


def add_evidence(conn: sqlite3.Connection, ev: model.AttributionEvidence) -> int:
    """Upsert by identity (subject_kind, subject_id, dimension, method, value, valid_from):
    same identity -> update last_confirmed_at only. A new value from the same
    (subject_kind, subject_id, dimension, method) closes the previous open row's
    valid_to (at the new evidence's valid_from or first_observed_at), then inserts.
    Returns evidence_id."""
    subject_id = str(ev.subject_id)
    identity = (ev.subject_kind, subject_id, ev.dimension, ev.method, ev.value, ev.valid_from)
    row = conn.execute(
        "SELECT evidence_id FROM attribution_evidence WHERE subject_kind = ? AND subject_id = ?"
        " AND dimension = ? AND method = ? AND value = ? AND valid_from = ?",
        identity,
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE attribution_evidence SET last_confirmed_at = max(last_confirmed_at, ?)"
            " WHERE evidence_id = ?",
            (ev.last_confirmed_at, row[0]),
        )
        return row[0]
    close_at = ev.valid_from if ev.valid_from != model.UNBOUNDED_START else ev.first_observed_at
    conn.execute(
        "UPDATE attribution_evidence SET valid_to = ? WHERE subject_kind = ? AND subject_id = ?"
        " AND dimension = ? AND method = ? AND value <> ? AND valid_to IS NULL",
        (close_at, ev.subject_kind, subject_id, ev.dimension, ev.method, ev.value),
    )
    cur = conn.execute(
        "INSERT INTO attribution_evidence (subject_kind, subject_id, dimension, value, method,"
        " source, confidence, validity, valid_from, valid_to, first_observed_at,"
        " last_confirmed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ev.subject_kind, subject_id, ev.dimension, ev.value, ev.method, ev.source,
         ev.confidence, ev.validity, ev.valid_from, ev.valid_to, ev.first_observed_at,
         ev.last_confirmed_at),
    )
    return cur.lastrowid


def _valid_at(ev: model.AttributionEvidence, t: int) -> bool:
    """D1 rule 1, inclusive bounds. Live evidence holds over
    [first_observed_at, last_confirmed_at] (D1, D2)."""
    if ev.validity == "live":
        if not ev.first_observed_at <= t <= ev.last_confirmed_at:
            return False
    if t < ev.valid_from:
        return False
    return ev.valid_to is None or t <= ev.valid_to


def _all_evidence(conn, subject_kind: str, subject_id: str, dimension: str) -> list[model.AttributionEvidence]:
    rows = conn.execute(
        f"SELECT {_EVIDENCE_COLS} FROM attribution_evidence"
        " WHERE subject_kind = ? AND subject_id = ? AND dimension = ?",
        (subject_kind, subject_id, dimension),
    ).fetchall()
    return [_evidence(r) for r in rows]


def _store(conn: sqlite3.Connection, ea: model.EffectiveAttribution) -> None:
    conn.execute(
        "INSERT INTO effective_attributions (subject_kind, subject_id, dimension, state, value,"
        " confidence, evidence_id, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (subject_kind, subject_id, dimension) DO UPDATE SET"
        " state = excluded.state, value = excluded.value, confidence = excluded.confidence,"
        " evidence_id = excluded.evidence_id, note = excluded.note",
        (ea.subject_kind, ea.subject_id, ea.dimension, ea.state, ea.value, ea.confidence,
         ea.evidence_id, ea.note),
    )


def resolve(conn: sqlite3.Connection, subject_kind: str, subject_id: str, dimension: str,
            subject_time: int) -> model.EffectiveAttribution:
    """Apply D1's resolution rule over evidence valid at subject_time (live evidence:
    [first_observed_at, last_confirmed_at]); store the result in effective_attributions
    (one row per subject/dimension, replacing any previous row); return it.
    No evidence -> state 'unattributed', value None, evidence_id None.

    Account values compare as canonical accounts (D7), so evidence naming two
    merged accounts agrees, and the stored value is the canonical key.

    A subject with no evidence at all for the dimension gets no row: D1 keeps
    rows only for subjects with evidence, or failed attempts
    (`record_failed_attempt`), and a usage event with no evidence of its own
    inherits from its session."""
    subject_id = str(subject_id)
    everything = _all_evidence(conn, subject_kind, subject_id, dimension)
    if not everything:
        return _own_row(conn, subject_kind, subject_id, dimension) or model.EffectiveAttribution(
            subject_kind, subject_id, dimension, "unattributed", note="no-evidence")
    valid = [ev for ev in everything if _valid_at(ev, subject_time)]
    if not valid:
        result = model.EffectiveAttribution(
            subject_kind, subject_id, dimension, "unattributed",
            note="no-evidence-valid-at-subject-time")
    else:
        top = max(model.CONFIDENCE_RANK[ev.confidence] for ev in valid)
        at_top = [ev for ev in valid if model.CONFIDENCE_RANK[ev.confidence] == top]
        confidence = at_top[0].confidence

        def value_of(ev: model.AttributionEvidence) -> str:
            return accounts.canonical(conn, ev.value) if dimension == "account" else ev.value

        values = {value_of(ev) for ev in at_top}
        if len(values) == 1:
            chosen = max(at_top, key=lambda ev: (ev.last_confirmed_at, ev.evidence_id))
            result = model.EffectiveAttribution(
                subject_kind, subject_id, dimension, "attributed", value=values.pop(),
                confidence=confidence, evidence_id=chosen.evidence_id)
        else:
            result = model.EffectiveAttribution(
                subject_kind, subject_id, dimension, "ambiguous", confidence=confidence,
                note=f"{len(values)} values disagree at {confidence}")
    _store(conn, result)
    return result


def record_failed_attempt(conn: sqlite3.Connection, subject_kind: str, subject_id: str,
                          dimension: str, note: str) -> None:
    """Zero-evidence attempt: store an 'unattributed' row with the note, only if no evidence exists."""
    subject_id = str(subject_id)
    if conn.execute(
        "SELECT 1 FROM attribution_evidence WHERE subject_kind = ? AND subject_id = ?"
        " AND dimension = ? LIMIT 1",
        (subject_kind, subject_id, dimension),
    ).fetchone():
        return
    _store(conn, model.EffectiveAttribution(
        subject_kind, subject_id, dimension, "unattributed", note=note))


def _own_row(conn, subject_kind: str, subject_id: str, dimension: str) -> model.EffectiveAttribution | None:
    row = conn.execute(
        "SELECT state, value, confidence, evidence_id, note FROM effective_attributions"
        " WHERE subject_kind = ? AND subject_id = ? AND dimension = ?",
        (subject_kind, subject_id, dimension),
    ).fetchone()
    if row is None:
        return None
    return model.EffectiveAttribution(subject_kind, subject_id, dimension, *row)


def effective(conn: sqlite3.Connection, subject_kind: str, subject_id: str,
              dimension: str) -> model.EffectiveAttribution:
    """Read-only lookup. A usage_event with no row of its own inherits its session's
    effective value, computed at query time (session from usage_events.session_key).

    Reading of D1: inheritance turns on the event having evidence of its own,
    so an event whose only row is a failed-attempt row still inherits."""
    subject_id = str(subject_id)
    own = _own_row(conn, subject_kind, subject_id, dimension)
    if subject_kind == "usage_event" and not _has_evidence(conn, subject_kind, subject_id, dimension):
        row = conn.execute(
            "SELECT session_key FROM usage_events WHERE usage_id = ?", (subject_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return own or model.EffectiveAttribution(
                subject_kind, subject_id, dimension, "unattributed", note="no-session-id")
        s = effective(conn, "session", row[0], dimension)
        if own is not None and s.state == "unattributed":
            return own  # the event's own failed-attempt note says more than the session's
        return model.EffectiveAttribution(
            subject_kind, subject_id, dimension, s.state, value=s.value,
            confidence=s.confidence, evidence_id=s.evidence_id, note=s.note)
    if own is not None:
        return own
    return model.EffectiveAttribution(
        subject_kind, subject_id, dimension, "unattributed", note="not-resolved")


def _has_evidence(conn, subject_kind: str, subject_id: str, dimension: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM attribution_evidence WHERE subject_kind = ? AND subject_id = ?"
        " AND dimension = ? LIMIT 1",
        (subject_kind, subject_id, dimension),
    ).fetchone() is not None


LIVE_GRACE_MS = 120_000  # live evidence still counts this long after its last confirmation


def account_at(conn: sqlite3.Connection, session_key: str, at: int) -> model.EffectiveAttribution:
    """D2's resolution rule for a session's account, applied at time `at`
    rather than over the session's whole span (assumption A19): a live pane
    is shown with the account it is using now. omp rotates logins within a
    session, so its session-level attribution can be ambiguous while the
    account at any moment is not. Evidence counts at `at` when its window
    covers it (live evidence: first observed to last confirmed, plus a grace).
    Highest confidence wins; disagreement at that confidence is ambiguous."""
    rows = conn.execute(
        "SELECT evidence_id, value, confidence, validity, valid_from, valid_to,"
        " first_observed_at, last_confirmed_at FROM attribution_evidence"
        " WHERE subject_kind = 'session' AND subject_id = ? AND dimension = 'account'",
        (session_key,)).fetchall()
    if not rows:  # nothing to resolve at a time: the stored effective attribution stands
        return effective(conn, "session", session_key, "account")
    rank = {"authoritative": 3, "observed": 2, "inferred": 1}
    valid = []
    for eid, value, conf, validity, vfrom, vto, first, last in rows:
        if validity == "live":
            ok = first <= at <= last + LIVE_GRACE_MS
        else:
            ok = vfrom <= at and (vto is None or vto >= at)
        if ok:
            valid.append((rank.get(conf, 0), accounts.canonical(conn, value), conf, eid))
    EA = model.EffectiveAttribution
    if not valid:
        return EA(subject_kind="session", subject_id=session_key, dimension="account",
                  state="unattributed", value=None, confidence=None, evidence_id=None,
                  note=f"no account evidence valid at {at}")
    top = max(v[0] for v in valid)
    best = [v for v in valid if v[0] == top]
    values = {v[1] for v in best}
    if len(values) > 1:
        return EA(subject_kind="session", subject_id=session_key, dimension="account",
                  state="ambiguous", value=None, confidence=best[0][2], evidence_id=None,
                  note="several accounts valid at once")
    return EA(subject_kind="session", subject_id=session_key, dimension="account",
              state="attributed", value=values.pop(), confidence=best[0][2],
              evidence_id=max(v[3] for v in best), note=None)
