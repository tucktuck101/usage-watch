"""Read-only queries over the store (D3 "Queries"): pools, agents, usage
breakdowns (V1) and "since I last looked" (V2).

Every function except the look markers takes a connection the caller opened
with `store.connect(readonly=True)` and only reads. The look markers write
only the caller's own `looks` row, through a separate read-write connection
the caller passes (D3: each view owns its own row).

Totals follow D1: a sum over records with unknown values reports the known
sum plus how many records had unknowns, and every counting event appears
exactly once, under one value or under `ambiguous` or `unattributed`
(invariants 1, 2 and 8).

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import asdict, dataclass

from .runtime import accounts as _accounts
from .runtime import attribution as _attribution

__all__ = [
    "AGENT_WINDOW_MS", "BY", "DISPLAY_AGE_S", "TOKEN_FIELDS", "LastLook",
    "account_label", "agents", "close_look", "open_look", "pools", "since_last_look",
    "touch_look", "usage",
]

# D6 freshness, the display column: how long a reading is shown as current.
DISPLAY_AGE_S: dict[str, float] = {
    "claude.statusline": 15 * 60,
    "codex.rollout": 15 * 60,
    "omp.usage_cache": 15 * 60,
    "claude.cached_utilization": 60 * 60,
    "omp.usage_history": 2 * 3600,
}
# Reading: D6 lists no display age for other anchor sources; they get the
# shortest listed one, so an unknown source is never shown as current for long.
DEFAULT_DISPLAY_AGE_S = 15 * 60

AGENT_WINDOW_MS = 2 * 60 * 1000  # agents: state samples from the last 2 minutes

TOKEN_FIELDS = ("uncached_input_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")
# Components whose absence makes a request's count incomplete. Reasoning is a
# subset of output (D1), so its absence alone leaves the counts whole.
_COMPONENTS = TOKEN_FIELDS[:4]

BY = ("harness", "provider", "model", "account", "project", "branch", "session", "day")
ATTRIBUTED_DIMENSIONS = ("account", "project", "branch")

AMBIGUOUS = "ambiguous"
UNATTRIBUTED = "unattributed"


# --- Accounts -------------------------------------------------------------------------

def account_label(conn: sqlite3.Connection, account_key: str | None) -> str | None:
    """The user's label, else `"<provider> account <first 6 hex>"`."""
    if account_key is None:
        return None
    row = conn.execute("SELECT provider, label FROM accounts WHERE account_key = ?",
                       (account_key,)).fetchone()
    if row is None:
        return f"account {account_key[:6]}"
    provider, label = row
    return label or f"{provider} account {account_key[:6]}"


def _canonical(conn: sqlite3.Connection, key: str) -> str:
    try:
        return _accounts.canonical(conn, key)
    except Exception:  # a hand-edited merge cycle: show the key as stored
        return key


# --- Pools ----------------------------------------------------------------------------

def _age_text(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h < 24 else f"{h // 24}d{h % 24}h"


def pools(conn: sqlite3.Connection, now: float) -> list[dict]:
    """The newest anchor per account and window. Anchors whose account isn't
    attributed are grouped by source instead, under one row per source and
    window. `now` is epoch seconds.

    Each row: `account` (canonical key or None), `account_label`,
    `account_state`, `window`, `used_pct`, `remaining_pct`, `resets_at`,
    `status`, `source`, `confidence`, `observed_at`, `age_s`, `age`,
    `display_age_s` and `stale` (older than D6's display age).
    """
    now_ms = int(now * 1000)
    # Newest sample per (source, stream, window) first, so the attribution
    # lookups that follow are per stream, not per sample.
    rows = conn.execute(
        'SELECT source, stream_key, "window", used_pct, resets_at, status, confidence, observed_at'
        " FROM (SELECT *, row_number() OVER (PARTITION BY source, stream_key, \"window\""
        "                                 ORDER BY observed_at DESC) AS n"
        "       FROM capacity_samples) WHERE n = 1"
    ).fetchall()
    best: dict[tuple, dict] = {}
    for source, stream_key, window, used_pct, resets_at, status, confidence, observed_at in rows:
        subject = f"{source}|{stream_key}|{window}|{observed_at}"  # prototype assumption A6
        eff = conn.execute(
            "SELECT state, value FROM effective_attributions"
            " WHERE subject_kind = 'capacity_sample' AND subject_id = ? AND dimension = 'account'",
            (subject,)).fetchone()
        state, value = eff if eff else (UNATTRIBUTED, None)
        account = _canonical(conn, value) if state == "attributed" and value else None
        group = ("account", account, window) if account else ("source", source, window)
        if group in best and best[group]["observed_at"] >= observed_at:
            continue
        display = DISPLAY_AGE_S.get(source, DEFAULT_DISPLAY_AGE_S)
        age_s = max(0.0, (now_ms - observed_at) / 1000)
        best[group] = {
            "account": account,
            "account_label": account_label(conn, account) if account else f"{state} ({source})",
            "account_state": state if not account else "attributed",
            "window": window,
            "used_pct": used_pct,
            "remaining_pct": None if used_pct is None else round(100 - used_pct, 2),
            "resets_at": resets_at,
            "status": status,
            "source": source,
            "confidence": confidence,
            "observed_at": observed_at,
            "age_s": round(age_s, 1),
            "age": _age_text(age_s),
            "display_age_s": display,
            "stale": age_s > display,
        }

    def order(r: dict):
        w = r["window"]
        rank = 0 if w == "session" else 1 if w == "weekly" else 2
        return (r["account"] is None, r["account_label"] or "", rank, w)

    return sorted(best.values(), key=order)


# --- Agents ---------------------------------------------------------------------------

def agents(conn: sqlite3.Connection, now: float) -> list[dict]:
    """The latest state sample per pane from the last 2 minutes, with the
    pane's session (live `pane` evidence, D2) and that session's effective
    account. `now` is epoch seconds."""
    now_ms = int(now * 1000)
    since = now_ms - AGENT_WINDOW_MS
    rows = conn.execute(
        "SELECT s.pane, s.observed_at, s.harness, s.session_key, s.state, s.model, s.reset_hint,"
        " s.error_key, s.note FROM state_samples s"
        " JOIN (SELECT pane, max(observed_at) AS t FROM state_samples WHERE observed_at >= ?"
        "       GROUP BY pane) latest ON latest.pane = s.pane AND latest.t = s.observed_at"
        " ORDER BY s.pane",
        (since,)).fetchall()
    out = []
    for pane, observed_at, harness, sample_session, state, model_, reset_hint, error_key, note in rows:
        session, join = sample_session, "sample" if sample_session else None
        if session is None:
            # Reading: the pairing is the most recently confirmed live
            # evidence still confirmed within the agents window; two sessions
            # confirmed at that same moment leave the pane unjoined.
            joined = conn.execute(
                "SELECT subject_id, last_confirmed_at FROM attribution_evidence"
                " WHERE subject_kind = 'session' AND dimension = 'pane' AND value = ?"
                " AND last_confirmed_at >= ? AND (valid_to IS NULL OR valid_to >= ?)"
                " ORDER BY last_confirmed_at DESC",
                (pane, since, since)).fetchall()
            if joined:
                top = [sk for sk, t in joined if t == joined[0][1]]
                if len(set(top)) == 1:
                    session, join = top[0], "pane_evidence"
                else:
                    join = AMBIGUOUS
        account = account_state = None
        if session:
            # The account in use now (A19), the same answer the nudge policy uses.
            from .policy import account_at
            eff = account_at(conn, session, now_ms)
            account_state = eff.state
            if eff.state == "attributed" and eff.value:
                account = _canonical(conn, eff.value)
        out.append({
            "pane": pane, "harness": harness, "state": state, "model": model_,
            "observed_at": observed_at, "age_s": round(max(0, now_ms - observed_at) / 1000, 1),
            "reset_hint": reset_hint, "error_key": error_key, "note": note,
            "session_key": session, "session_join": join,
            "account": account, "account_state": account_state,
            "account_label": account_label(conn, account),
        })
    return out


# --- Usage (V1) -----------------------------------------------------------------------

def _token_columns(prefix: str = "e.") -> str:
    parts = []
    for f in TOKEN_FIELDS:
        parts.append(f"sum({prefix}{f})")
        parts.append(f"count(*) - count({prefix}{f})")
    known = " AND ".join(f"{prefix}{f} IS NOT NULL" for f in _COMPONENTS[:3])
    parts.append(f"sum(CASE WHEN {known} THEN {prefix}uncached_input_tokens"
                 f" + {prefix}cache_read_input_tokens + {prefix}cache_write_input_tokens END)")
    parts.append(f"sum(CASE WHEN {known} THEN 0 ELSE 1 END)")
    anynull = " OR ".join(f"{prefix}{f} IS NULL" for f in _COMPONENTS)
    parts.append(f"sum(CASE WHEN {anynull} THEN 1 ELSE 0 END)")
    parts.append("count(*)")
    return ", ".join(parts)


def _zero() -> dict:
    d: dict = {"requests": 0, "total_input_tokens": 0, "unknown_total_input": 0, "unknown_requests": 0}
    for f in TOKEN_FIELDS:
        d[f] = 0
    d["unknown"] = {f: 0 for f in TOKEN_FIELDS}
    return d


def _from_row(vals: tuple) -> dict:
    d = _zero()
    i = 0
    for f in TOKEN_FIELDS:
        d[f] = vals[i] or 0
        d["unknown"][f] = vals[i + 1] or 0
        i += 2
    d["total_input_tokens"] = vals[i] or 0
    d["unknown_total_input"] = vals[i + 1] or 0
    d["unknown_requests"] = vals[i + 2] or 0
    d["requests"] = vals[i + 3] or 0
    return d


def _add(into: dict, more: dict) -> None:
    for k, v in more.items():
        if k == "unknown":
            for f, n in v.items():
                into["unknown"][f] += n
        elif isinstance(v, int):
            into[k] += v


def _where(since_ms: int | None, until_ms: int | None, include_auxiliary: bool) -> tuple[str, dict]:
    clauses, args = [], {}
    if since_ms is not None:
        clauses.append("e.observed_at >= :since")
        args["since"] = since_ms
    if until_ms is not None:
        clauses.append("e.observed_at < :until")
        args["until"] = until_ms
    if not include_auxiliary:
        clauses.append("e.auxiliary = 0")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def _resolve_ambiguous_accounts_at_time(conn: sqlite3.Connection, base: str, where: str,
                                        args: dict) -> bool:
    """For sessions whose account is ambiguous over their whole span (omp
    rotates logins), resolve each request at its own time: D2's rule with the
    request's `observed_at` as the subject time (A19). Results go to a temp
    table, so the read-only store is never written. Returns whether any were."""
    from .policy import account_at
    sessions = [r[0] for r in conn.execute(
        "SELECT subject_id FROM effective_attributions WHERE subject_kind = 'session'"
        " AND dimension = 'account' AND state = 'ambiguous'")]
    if not sessions:
        return False
    marks = ",".join("?" * len(sessions))
    cond = f"e.session_key IN ({marks})"
    sql_where = (where + " AND " + cond) if where else (" WHERE " + cond)
    named = {k: v for k, v in args.items()}
    # mix named and positional safely: inline the named values as positional
    q = f"SELECT e.usage_id, e.session_key, e.observed_at{base}{sql_where}"
    for k in sorted(named, key=len, reverse=True):
        q = q.replace(f":{k}", "?")
    order = [k for k in ("since", "until") if k in named]
    params = [named[k] for k in order] + sessions
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS uw_account_at"
                 " (usage_id INTEGER PRIMARY KEY, st TEXT NOT NULL, val TEXT)")
    conn.execute("DELETE FROM temp.uw_account_at")
    rows = []
    for usage_id, session_key, observed_at in conn.execute(q, params).fetchall():
        ea = account_at(conn, session_key, observed_at)
        rows.append((usage_id, ea.state, ea.value if ea.state == "attributed" else None))
    conn.executemany("INSERT INTO temp.uw_account_at VALUES (?, ?, ?)", rows)
    return True


def _project_of(cwd: str | None, checkouts: list[tuple[str, str]]) -> str | None:
    """The display name of the checkout whose local path is the longest
    prefix of `cwd`, on a path boundary."""
    if not cwd:
        return None
    for path, name in checkouts:  # longest path first
        if cwd == path or cwd.startswith(path.rstrip(os.sep) + os.sep):
            return name
    return None


def usage(conn: sqlite3.Connection, since_ms: int | None, until_ms: int | None, by: str,
          include_auxiliary: bool = True) -> dict:
    """Token sums over canonical usage events in `[since_ms, until_ms)`,
    grouped `by` one of BY.

    Returns `{"by", "since_ms", "until_ms", "include_auxiliary", "rows",
    "total", "shares"}`. Each row has `key`, `state` (`value`, or for
    account, project and branch `attributed`, `ambiguous` or
    `unattributed`), `label`, and the sums: each token field's known sum,
    `unknown[field]` (requests with that field unknown), `total_input_tokens`
    (over requests whose three input parts are known), `unknown_total_input`,
    `unknown_requests` (requests with any component unknown) and `requests`.
    `total` is computed separately from the rows, so the invariant "the
    total equals the sum of the rows" (D1 1, 2) can be checked.
    `--no-auxiliary` is the named filter of invariant 2.
    """
    if by not in BY:
        raise ValueError(f"by must be one of {', '.join(BY)}")
    where, args = _where(since_ms, until_ms, include_auxiliary)
    cols = _token_columns()
    base = (" FROM usage_events e"
            " JOIN usage_observations o ON o.observation_id = e.accounting_observation_id")

    total = _from_row(conn.execute(f"SELECT {cols}{base}{where}", args).fetchone())

    groups: dict[tuple[str, str | None], dict] = {}

    def put(state: str, key: str | None, sums: dict) -> None:
        g = groups.setdefault((state, key), _zero())
        _add(g, sums)

    if by in ("harness", "provider", "model"):
        for row in conn.execute(f"SELECT o.{by}, {cols}{base}{where} GROUP BY o.{by}", args):
            put("value", row[0], _from_row(row[1:]))
    elif by == "session":
        for row in conn.execute(f"SELECT e.session_key, {cols}{base}{where} GROUP BY e.session_key", args):
            put("value", row[0], _from_row(row[1:]))
    elif by == "day":
        # Reading: days are local calendar days, the way a person counts them.
        for row in conn.execute(
                f"SELECT date(e.observed_at / 1000, 'unixepoch', 'localtime'), {cols}{base}{where}"
                " GROUP BY 1", args):
            put("value", row[0], _from_row(row[1:]))
    elif by == "project":
        checkouts = sorted(
            ((p, n or os.path.basename(p.rstrip(os.sep)))
             for p, n in conn.execute(
                 "SELECT local_path, display_name FROM checkouts WHERE local_path IS NOT NULL")),
            key=lambda pn: len(pn[0]), reverse=True)
        cwd = dict(conn.execute("SELECT session_key, cwd FROM sessions"))
        for row in conn.execute(f"SELECT e.session_key, {cols}{base}{where} GROUP BY e.session_key", args):
            name = _project_of(cwd.get(row[0]), checkouts) if row[0] else None
            put("attributed" if name else UNATTRIBUTED, name, _from_row(row[1:]))
    else:  # account, branch: effective attribution, inherited from the session (D1, D2)
        at_time = by == "account" and _resolve_ambiguous_accounts_at_time(conn, base, where, args)
        has_own = ("EXISTS (SELECT 1 FROM attribution_evidence ae WHERE ae.subject_kind = 'usage_event'"
                   " AND ae.subject_id = CAST(e.usage_id AS TEXT) AND ae.dimension = :dim)")
        t_st = "WHEN t.usage_id IS NOT NULL THEN t.st " if at_time else ""
        t_val = "WHEN t.usage_id IS NOT NULL THEN t.val " if at_time else ""
        t_join = " LEFT JOIN temp.uw_account_at t ON t.usage_id = e.usage_id" if at_time else ""
        sql = (
            f"SELECT st, val, {cols} FROM ("
            f"  SELECT e.*, CASE WHEN {has_own} THEN COALESCE(own.state, 'unattributed')"
            f"                    {t_st}ELSE COALESCE(ses.state, 'unattributed') END AS st,"
            f"              CASE WHEN {has_own} THEN own.value {t_val}ELSE ses.value END AS val"
            f"  {base}"
            "   LEFT JOIN effective_attributions own ON own.subject_kind = 'usage_event'"
            "        AND own.subject_id = CAST(e.usage_id AS TEXT) AND own.dimension = :dim"
            "   LEFT JOIN effective_attributions ses ON ses.subject_kind = 'session'"
            "        AND ses.subject_id = e.session_key AND ses.dimension = :dim"
            f"  {t_join}"
            f"  {where}"
            ") e GROUP BY st, val"
        )
        for row in conn.execute(sql, {"dim": by, **args}):
            state, value = row[0], row[1]
            if state != "attributed":
                value = None
            elif by == "account":
                value = _canonical(conn, value)
            put(state, value, _from_row(row[2:]))

    rows = []
    for (state, key), sums in groups.items():
        if state == "value":
            label = key if key is not None else "unknown"
        elif state == "attributed":
            label = account_label(conn, key) if by == "account" else key
        else:
            label = f"({state})"
        rows.append({"key": key, "state": state, "label": label, **sums})
    rank = {"value": 0, "attributed": 0, AMBIGUOUS: 1, UNATTRIBUTED: 2}
    if by == "day":
        rows.sort(key=lambda r: (r["key"] is None, r["key"] or ""))
    else:
        rows.sort(key=lambda r: (rank[r["state"]], -r["requests"], str(r["label"])))

    shares = None
    if by in ATTRIBUTED_DIMENSIONS:
        n = total["requests"]
        counts = {s: sum(r["requests"] for r in rows if r["state"] == s)
                  for s in ("attributed", AMBIGUOUS, UNATTRIBUTED)}
        shares = {s: (c / n if n else None) for s, c in counts.items()}
        shares["basis"] = "requests"

    return {"by": by, "since_ms": since_ms, "until_ms": until_ms,
            "include_auxiliary": include_auxiliary, "rows": rows, "total": total, "shares": shares}


# --- "Since I last looked" (V2) ---------------------------------------------------------

@dataclass(frozen=True)
class LastLook:
    """Where V2 starts. `since_ms` is None when there was no previous look.
    `basis` is `closed_at`, `last_seen_at` or `opened_at`; `flagged` is set
    when the previous look didn't close normally, so the view says so."""

    who: str
    view: str
    since_ms: int | None
    basis: str | None
    flagged: bool
    opened_at: int | None = None
    last_seen_at: int | None = None
    closed_at: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def note(self) -> str | None:
        if self.since_ms is None:
            return "no previous look recorded"
        if self.flagged:
            return (f"the previous look ended without closing; counting from its "
                    f"{self.basis.replace('_at', '').replace('_', ' ')} time instead")
        return None


def since_last_look(conn: sqlite3.Connection, who: str, view: str) -> LastLook:
    """D3: since the previous look's `closed_at`; if it has none (the view
    exited abnormally), its `last_seen_at`, flagged. Read before `open_look`
    replaces the row.

    Reading: a previous look with neither (it never rendered) falls back to
    its `opened_at`, also flagged."""
    row = conn.execute('SELECT opened_at, last_seen_at, closed_at FROM looks'
                       ' WHERE who = ? AND "view" = ?', (who, view)).fetchone()
    if row is None:
        return LastLook(who, view, None, None, False)
    opened, seen, closed = row
    if closed is not None:
        return LastLook(who, view, closed, "closed_at", False, opened, seen, closed)
    if seen is not None:
        return LastLook(who, view, seen, "last_seen_at", True, opened, seen, closed)
    if opened is not None:
        return LastLook(who, view, opened, "opened_at", True, opened, seen, closed)
    return LastLook(who, view, None, None, False, opened, seen, closed)


def _write_look(rw: sqlite3.Connection, sql: str, args: tuple) -> None:
    rw.execute("BEGIN IMMEDIATE")
    try:
        rw.execute(sql, args)
        rw.execute("COMMIT")
    except BaseException:
        if rw.in_transaction:
            rw.execute("ROLLBACK")
        raise


def open_look(rw: sqlite3.Connection, who: str, view: str, now_ms: int) -> None:
    """Start a look: `opened_at` now, `last_seen_at` and `closed_at` cleared,
    so an abnormal exit leaves `closed_at` empty. Writes only `(who, view)`."""
    _write_look(rw, 'INSERT INTO looks (who, "view", opened_at, last_seen_at, closed_at)'
                    " VALUES (?, ?, ?, NULL, NULL) ON CONFLICT (who, \"view\") DO UPDATE SET"
                    " opened_at = excluded.opened_at, last_seen_at = NULL, closed_at = NULL",
                (who, view, now_ms))


def touch_look(rw: sqlite3.Connection, who: str, view: str, now_ms: int) -> None:
    """A successful full render: `last_seen_at` now. Never moves V2's marker."""
    _write_look(rw, 'UPDATE looks SET last_seen_at = ? WHERE who = ? AND "view" = ?',
                (now_ms, who, view))


def close_look(rw: sqlite3.Connection, who: str, view: str, now_ms: int) -> None:
    """A normal close: `closed_at` now."""
    _write_look(rw, 'UPDATE looks SET closed_at = ? WHERE who = ? AND "view" = ?',
                (now_ms, who, view))


def parse_since(text: str, now: float) -> int:
    """`24h`, `7d`, `30m`, `90s`, or an ISO date or time (local unless it
    names a zone); return UTC ms. Raises ValueError."""
    t = text.strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    if len(t) >= 2 and t[-1] in units and t[:-1].replace(".", "", 1).isdigit():
        return int((now - float(t[:-1]) * units[t[-1]]) * 1000)
    when = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.astimezone()  # local
    return int(when.timestamp() * 1000)
