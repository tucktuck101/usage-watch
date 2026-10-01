"""The D8 nudge policy, read from the store (F5).

A nudge types into someone's agent, so the policy acts only on evidence and
waits when evidence is missing. docs/design/D8-nudge-policy.md is the only
place the conditions are defined; this module implements them:

  1. the pane is stalled, freshly (state sample under 30 s) and with an
     error_key, and is re-read immediately before typing;
  2. the session's effective account is attributed on authoritative or
     observed evidence, or on inferred evidence with D7's discovery-complete
     single-account fallback;
  3. the blocking window is clear, by a fresh anchor or a linked recovery
     signal (an unknown window needs every known window clear by a fresh
     anchor);
  4. the set of known applicable windows is known, and every other window
     in it is clear by a fresh anchor (stale-window fallback off, A1).

Every decision but `none` is recorded in `nudges`, which only this module
writes. Times in the store are Unix milliseconds; `clock` returns seconds.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import adapters, config, screen, store, topology
from .adapters.base import model_family
from .errors import Problem
from .runtime import accounts, attribution

__all__ = [
    "CONTROL_AGE_MS", "COMPLETE_DISCOVERY", "Anchor", "Decision", "Policy",
    "account_anchors", "applicable", "family_word", "pane_session", "stall_id",
]

MIN = 60 * 1000

# D6: the control age per anchor source. None = never used for control.
# A source not listed has no declared control age and is never used either.
CONTROL_AGE_MS: dict[str, int | None] = {
    "claude.statusline": 15 * MIN,
    "codex.rollout": 15 * MIN,
    "omp.usage_cache": 10 * MIN,
    "claude.cached_utilization": 15 * MIN,
    "omp.usage_history": None,
}
# D6: the display age per anchor source (views and alerts).
DISPLAY_AGE_MS: dict[str, int] = {
    "claude.statusline": 15 * MIN,
    "codex.rollout": 15 * MIN,
    "omp.usage_cache": 15 * MIN,
    "claude.cached_utilization": 60 * MIN,
    "omp.usage_history": 120 * MIN,
}
# A14: window discovery per source. Anything else is partial.
COMPLETE_DISCOVERY = frozenset({"omp.usage_cache", "claude.cached_utilization", "codex.rollout"})
# D7: harnesses whose account discovery is complete, with the alias kind that
# enumerates their logins and the collector whose failure makes it partial.
ACCOUNT_DISCOVERY = {"omp": ("omp.identity_key", "omp.session")}
FAMILY_PROVIDER = {"claude": "anthropic", "codex": "openai"}

STATE_FRESH_MS = 30 * 1000          # D6 / D8 condition 1
SESSION_JOIN_MS = 60 * 1000         # pane -> session evidence must be this recent
PANE_GONE_MS = 5 * MIN              # a pane with no sample this recent is not evaluated
LOOKBACK_MS = 8 * 24 * 60 * MIN     # D8: one weekly window plus a day
# Reading: "near stall onset" for the transcript hit naming the window.
NEAR_BEFORE_MS = 5 * MIN
NEAR_AFTER_MS = 2 * MIN

_GENERIC_WORDS = frozenset({"claude", "anthropic", "openai", "model"})


# --- small helpers ---------------------------------------------------------------------

def stall_id(key: str, error_key: str | None, onset: int) -> str:
    """D8: one stall occurrence, from (session_key or pane, error_key, onset)."""
    return hashlib.sha256(f"{key}|{error_key or ''}|{onset}".encode()).hexdigest()[:16]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z]+", text.lower()) if w]


def family_word(model: str | None) -> str | None:
    """The family word of a model ("Fable 5.1" -> "fable", "claude-opus-4" -> "opus")."""
    if not model:
        return None
    return next((w for w in _words(model) if w not in _GENERIC_WORDS), None)


def applicable(window: str, model: str | None) -> bool:
    """D8: session, weekly and other:* always; weekly:<m> when <m> is the
    session's model family, or for every <m> when the model is unknown.
    Reading: a suffix that holds the family word among its words matches,
    so omp's model-id suffixes (weekly:claude-opus-4) are applicable too."""
    if not window.startswith("weekly:"):
        return True
    fam = family_word(model)
    return fam is None or fam in _words(window.split(":", 1)[1])


LIVE_GRACE_MS = 120_000  # live evidence still counts this long after its last confirmation


def account_at(conn: sqlite3.Connection, session_key: str, at: int) -> attribution.model.EffectiveAttribution:
    """D2's resolution rule for a session's account, applied at time `at`
    rather than over the session's whole span (assumption A19): a live pane
    is nudged for the account it is using now. omp rotates logins within a
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
        return attribution.effective(conn, "session", session_key, "account")
    rank = {"authoritative": 3, "observed": 2, "inferred": 1}
    valid = []
    for eid, value, conf, validity, vfrom, vto, first, last in rows:
        if validity == "live":
            ok = first <= at <= last + LIVE_GRACE_MS
        else:
            ok = vfrom <= at and (vto is None or vto >= at)
        if ok:
            valid.append((rank.get(conf, 0), accounts.canonical(conn, value), conf, eid))
    EA = attribution.model.EffectiveAttribution
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


def pane_session(conn: sqlite3.Connection, pane: str, now: int) -> str | None:
    """The session joined to a pane by live `pane` evidence confirmed within 60 s."""
    row = conn.execute(
        "SELECT subject_id FROM attribution_evidence WHERE subject_kind = 'session'"
        " AND dimension = 'pane' AND value = ? AND validity = 'live' AND last_confirmed_at >= ?"
        " ORDER BY last_confirmed_at DESC, evidence_id DESC LIMIT 1",
        (pane, now - SESSION_JOIN_MS)).fetchone()
    return row[0] if row else None


@dataclass
class Anchor:
    source: str
    stream_key: str
    window: str
    used_pct: float | None
    resets_at: int | None
    status: str
    confidence: str
    observed_at: int
    account: str

    def remaining(self) -> float | None:
        return None if self.used_pct is None else 100 - self.used_pct

    def current(self, now: int) -> bool:
        """D8: the anchor's window instance is current when resets_at is in the future.
        Reading: an anchor with no resets_at can't be placed in an instance; not current."""
        return self.resets_at is not None and self.resets_at > now

    def fresh(self, now: int) -> bool:
        age = CONTROL_AGE_MS.get(self.source)
        return age is not None and now - self.observed_at <= age

    def describe(self, now: int) -> dict:
        return {"source": self.source, "window": self.window,
                "age_s": round((now - self.observed_at) / 1000), "confidence": self.confidence,
                "used_pct": self.used_pct, "resets_at": self.resets_at, "status": self.status,
                "account": self.account}


def account_anchors(conn: sqlite3.Connection, account: str | None, since: int,
                    sources=None) -> list[Anchor]:
    """Capacity samples since `since` whose effective account (A6 subject id) is
    attributed; `account` None returns every account's. Newest first. Accounts
    compare canonically (D7)."""
    rows = conn.execute(
        "SELECT c.source, c.stream_key, c.\"window\", c.used_pct, c.resets_at, c.status,"
        " c.confidence, c.observed_at, e.value FROM capacity_samples c"
        " JOIN effective_attributions e ON e.subject_kind = 'capacity_sample'"
        " AND e.dimension = 'account' AND e.state = 'attributed'"
        " AND e.subject_id = c.source || '|' || c.stream_key || '|' || c.\"window\""
        " || '|' || c.observed_at"
        " WHERE c.observed_at >= ? ORDER BY c.observed_at DESC, c.capacity_sample_id DESC",
        (since,)).fetchall()
    cache: dict[str, str] = {}
    out = []
    for *fields, value in rows:
        if sources is not None and fields[0] not in sources:
            continue
        if value not in cache:
            cache[value] = accounts.canonical(conn, value)
        acct = cache[value]
        if account is None or acct == account:
            out.append(Anchor(*fields, account=acct))
    return out


@dataclass
class Decision:
    pane: str
    harness: str | None
    state: str
    session_key: str | None
    account: str | None
    account_label: str | None
    action: str            # 'none' | 'nudge' | 'wait' | 'escalate'
    reason: str            # human-readable, names the D8 condition that failed or passed
    stall_id: str | None
    evidence: dict = field(default_factory=dict)
    reason_code: str = ""  # recorded in nudges.reason_code


class _Wait(Exception):
    """A D8 condition failed: the pane waits."""

    def __init__(self, code: str, reason: str, **evidence):
        super().__init__(reason)
        self.code, self.reason, self.evidence = code, reason, evidence


# --- the policy ----------------------------------------------------------------------------

class Policy:
    """D8 over the store. Parameters are D8's, at A1's defaults."""

    min_remaining_pct = 5.0
    stale_window_fallback = False
    stale_window_max_age_ms = 60 * MIN
    stale_window_min_remaining_pct = 50.0

    def __init__(self, conn: sqlite3.Connection, cfg: config.Config, *, act: bool = True,
                 clock=time.time, notify=None, settle_s: float = 0):
        self.conn = conn
        self.cfg = cfg
        self.act = act
        self.clock = clock
        if notify is None:
            from .watcher import notify  # imported late: watcher pulls in the old pool code
        self.notify = notify
        self.settle_ms = int(settle_s * 1000)
        self.schema_message: str | None = None
        self._panes: dict | None = None
        self._anchor_cache: dict = {}

    # -- the tick ---------------------------------------------------------------------------

    def tick(self) -> list[Decision]:
        """Evaluate every pane from its latest state sample; see the module docstring."""
        status = store.schema_status(self.conn)
        if not status.readable:
            # D8: an older schema means wait, without reading (D3).
            self.schema_message = status.message
            return []
        self.schema_message = None
        self._panes = None
        self._anchor_cache = {}
        now = int(self.clock() * 1000)
        self.now_ms = now
        rows = self.conn.execute(
            "SELECT s.pane, s.observed_at, s.harness, s.state, s.model, s.error_key, s.note,"
            " s.reset_hint"
            " FROM state_samples s JOIN (SELECT pane, max(observed_at) AS t FROM state_samples"
            " GROUP BY pane) m ON m.pane = s.pane AND m.t = s.observed_at"
            " WHERE s.observed_at >= ? ORDER BY s.pane",
            (now - PANE_GONE_MS,)).fetchall()
        return [self._decide(now, *row) for row in rows]

    def _decide(self, now, pane, observed_at, harness, state, model, error_key, note,
                reset_hint=None) -> Decision:
        session_key = pane_session(self.conn, pane, now)
        d = Decision(pane, harness, state, session_key, None, None, "none",
                     note or state, None, {})
        if state != "stalled":
            return d
        onset = self._onset(pane, error_key, observed_at)
        d.stall_id = stall_id(session_key or pane, error_key, onset)
        d.evidence = {"stall_onset": onset, "state_age_s": round((now - observed_at) / 1000)}

        target = self._pane(pane)
        if self.cfg.ignored(target):
            d.reason = "ignored by config"
            return d
        if target.lane_done:
            d.reason = "lane has written its handoff; nothing left to continue"
            return d

        try:
            self._escalation(d, now, pane, error_key, onset, observed_at)
            if d.action == "escalate":
                return self._record(d, now, error_key)
            self._condition_1(now, observed_at, error_key)
            if reset_hint is not None and reset_hint > now:
                # The harness's own message says when this stall's limit lifts
                # (e.g. omp's retry-after, pinned to onset). That is the most
                # direct evidence about the condition that stopped the pane:
                # no anchor overrides it (prototype assumption A20).
                raise _Wait("c3-harness-retry-after",
                            "condition 3: the harness says retry after "
                            + time.strftime("%H:%M", time.localtime(reset_hint / 1000)),
                            retry_after_until=reset_hint)
            self._condition_2(d, harness, model)
            blocking = self._blocking_window(session_key, onset)
            d.evidence["blocking_window"] = blocking
            known, windows, why = self._window_set(d.account, model, now)
            d.evidence["known_windows"] = sorted(windows)
            self._condition_3(d, now, blocking, onset, session_key, known, windows, why)
            self._condition_4(d, now, blocking, known, windows, why)
        except _Wait as w:
            d.action, d.reason_code = "wait", w.code
            d.reason = w.reason
            d.evidence.update(w.evidence)
            return self._record(d, now, error_key)

        d.action, d.reason_code = "nudge", "all-conditions-hold"
        d.reason = "D8: all four conditions hold"
        if not self.act:
            d.reason += " (not acting: nothing typed)"
            return d
        return self._nudge(d, now, harness, error_key, target)

    # -- stall occurrence -------------------------------------------------------------------

    def _onset(self, pane: str, error_key: str | None, latest: int) -> int:
        """D8: the first sample showing this stall after the pane was last seen not
        stalled. Reading (as the screen collector): `unknown` neither ends nor starts
        a stall; a different error_key while stalled is a new occurrence."""
        brk = self.conn.execute(
            "SELECT max(observed_at) FROM state_samples WHERE pane = ? AND observed_at <= ?"
            " AND (state NOT IN ('stalled', 'unknown') OR (state = 'stalled'"
            " AND error_key IS NOT ?))", (pane, latest, error_key)).fetchone()[0]
        onset = self.conn.execute(
            "SELECT min(observed_at) FROM state_samples WHERE pane = ? AND state = 'stalled'"
            " AND error_key IS ? AND observed_at > ? AND observed_at <= ?",
            (pane, error_key, brk if brk is not None else -1, latest)).fetchone()[0]
        return onset if onset is not None else latest

    def _nudged(self, stall: str, pane: str, error_key, onset: int):
        """The nudge already made for this occurrence, if any. Matching on (pane,
        error_key, decided after onset) too keeps "nudge once" even if the session
        join changes the stall_id mid-stall."""
        return self.conn.execute(
            "SELECT nudge_id, decided_at FROM nudges WHERE action = 'nudge' AND (stall_id = ?"
            " OR (pane = ? AND error_key IS ? AND decided_at >= ?)) ORDER BY decided_at LIMIT 1",
            (stall, pane, error_key, onset)).fetchone()

    def _escalation(self, d: Decision, now, pane, error_key, onset, observed_at) -> None:
        """D8 (unchanged from today): the same stall persisting after a nudge, or
        max_strikes nudges without the pane being seen busy, escalates."""
        done = self._nudged(d.stall_id, pane, error_key, onset)
        if done is not None:
            if observed_at > done[1] + self.settle_ms:
                d.action, d.reason_code = "escalate", "stall-persisted-after-nudge"
                d.reason = "escalate: the same stall persists after its nudge"
            else:
                raise _Wait("nudged-settling", "nudged; waiting for the pane to react")
            return
        busy = self.conn.execute(
            "SELECT max(observed_at) FROM state_samples WHERE pane = ? AND state = 'busy'",
            (pane,)).fetchone()[0]
        strikes = self.conn.execute(
            "SELECT count(*) FROM nudges WHERE pane = ? AND action = 'nudge' AND decided_at > ?",
            (pane, busy if busy is not None else -1)).fetchone()[0]
        if strikes >= self.cfg.max_strikes:
            d.action, d.reason_code = "escalate", "strikes"
            d.reason = (f"escalate: {strikes} nudges without the pane being seen busy "
                        f"(max_strikes {self.cfg.max_strikes})")

    # -- the four conditions -----------------------------------------------------------------

    def _condition_1(self, now: int, observed_at: int, error_key) -> None:
        if now - observed_at > STATE_FRESH_MS:
            raise _Wait("c1-state-stale",
                        f"condition 1: the stalled state sample is {(now - observed_at) // 1000} s"
                        " old (needs under 30 s)")
        if not error_key:
            raise _Wait("c1-no-error-key",
                        "condition 1: the stall has no error_key identifying the limit")

    def _condition_2(self, d: Decision, harness: str | None, model: str | None) -> None:
        if d.session_key is None:
            raise _Wait("c2-no-session", "condition 2: account unknown (no session joined "
                        "to this pane)")
        ea = account_at(self.conn, d.session_key, self.now_ms)
        d.evidence["account_attribution"] = {"state": ea.state, "confidence": ea.confidence,
                                             "evidence_id": ea.evidence_id, "note": ea.note}
        if ea.state != "attributed":
            raise _Wait(f"c2-{ea.state}", f"condition 2: the session's account is {ea.state}")
        acct = accounts.canonical(self.conn, ea.value)
        d.account = acct
        d.evidence["account"] = acct
        row = self.conn.execute("SELECT label FROM accounts WHERE account_key = ?",
                                (acct,)).fetchone()
        d.account_label = row[0] if row else None
        if ea.confidence in ("authoritative", "observed"):
            return
        # Inferred: D8's fallback, only where D7 says discovery is complete.
        if harness not in ACCOUNT_DISCOVERY:
            raise _Wait("c2-inferred-partial-discovery",
                        f"condition 2: account rests on inferred evidence and {harness}'s "
                        "account discovery is partial")
        alias_kind, collector = ACCOUNT_DISCOVERY[harness]
        failed = self.conn.execute(
            "SELECT last_error FROM collector_status WHERE collector = ?", (collector,)).fetchone()
        if failed and failed[0]:
            raise _Wait("c2-inferred-partial-discovery",
                        f"condition 2: {collector} failed on its last pass, so {harness}'s "
                        "account discovery counts as partial")
        provider = FAMILY_PROVIDER.get(model_family(model))
        if provider is None:
            raise _Wait("c2-inferred-family-unknown",
                        "condition 2: account rests on inferred evidence and the model family "
                        "is unknown")
        keys = {accounts.canonical(self.conn, k) for (k,) in self.conn.execute(
            "SELECT DISTINCT a.account_key FROM account_aliases a JOIN accounts c"
            " ON c.account_key = a.account_key WHERE a.alias_kind = ? AND a.revoked_at IS NULL"
            " AND c.provider = ? AND c.removed_at IS NULL", (alias_kind, provider))}
        if keys != {acct}:
            raise _Wait("c2-inferred-not-single",
                        f"condition 2: account rests on inferred evidence and {harness} has "
                        f"{len(keys)} {provider} accounts, not exactly this one")

    def _blocking_window(self, session_key: str | None, onset: int) -> str | None:
        """The window a transcript `hit` of the same session near onset names.
        Screen limit events carry no window, and no adapter declares one for a
        generic message, so otherwise the window is unknown (never `session`)."""
        if session_key is None:
            return None
        row = self.conn.execute(
            "SELECT \"window\" FROM limit_events WHERE stream_key = ? AND kind = 'hit'"
            " AND \"window\" IS NOT NULL AND source <> 'screen' AND observed_at BETWEEN ? AND ?"
            " ORDER BY abs(observed_at - ?) LIMIT 1",
            (session_key, onset - NEAR_BEFORE_MS, onset + NEAR_AFTER_MS, onset)).fetchone()
        return row[0] if row else None

    def _anchors(self, account: str, now: int) -> dict[str, list[Anchor]]:
        """Nudge-eligible anchors of the account within the look-back, newest first per window.
        omp.usage_history and sources with no control age are never used; estimates are
        never stored (D1), so none can appear here."""
        if (account, now) in self._anchor_cache:
            return self._anchor_cache[(account, now)]
        eligible = {s for s, age in CONTROL_AGE_MS.items() if age is not None}
        out: dict[str, list[Anchor]] = {}
        for a in account_anchors(self.conn, account, now - LOOKBACK_MS, eligible):
            out.setdefault(a.window, []).append(a)
        self._anchor_cache[(account, now)] = out
        return out

    def _window_set(self, account: str, model: str | None, now: int):
        """D8: (set known?, known applicable windows, why not)."""
        by_window = self._anchors(account, now)
        windows = {w for w in by_window if applicable(w, model)}
        complete = [a for ws in by_window.values() for a in ws if a.source in COMPLETE_DISCOVERY]
        if not complete:
            return False, windows, "no report from a source with complete window discovery"
        latest = max(complete, key=lambda a: a.observed_at)
        report = [a for a in complete if (a.source, a.stream_key, a.observed_at)
                  == (latest.source, latest.stream_key, latest.observed_at)]
        windows |= {a.window for a in report if applicable(a.window, model)}
        if not latest.fresh(now):
            return False, windows, (f"the latest complete report ({latest.source}) is "
                                    f"{(now - latest.observed_at) // 1000} s old, past its "
                                    "control age")
        if not any(a.current(now) for a in report):
            return False, windows, "the latest complete report is for an ended window instance"
        ended = sorted(w for w in windows if not any(a.current(now) for a in by_window.get(w, [])))
        if ended:
            return False, windows, (f"window {ended[0]} was seen but not in its current "
                                    "instance, so it can't be evaluated")
        return True, windows, ""

    def _fresh_clear(self, account: str, window: str, now: int) -> tuple[bool, str, dict | None]:
        """D8 3(a): the newest anchor is fresh, current, has min_remaining_pct left
        and isn't exhausted."""
        anchors = self._anchors(account, now).get(window, [])
        if not anchors:
            return False, f"no anchor for {window}", None
        a = anchors[0]
        ev = a.describe(now)
        if not a.fresh(now):
            return False, (f"{window}'s newest anchor ({a.source}) is "
                           f"{(now - a.observed_at) // 1000} s old, past its control age"), ev
        if not a.current(now):
            return False, f"{window}'s newest anchor is for an ended window instance", ev
        if a.status == "exhausted":
            return False, f"{window} is exhausted ({a.source})", ev
        rem = a.remaining()
        if rem is None or rem < self.min_remaining_pct:
            return False, (f"{window} has {'unknown' if rem is None else f'{rem:g}%'} remaining"
                           f" (needs at least {self.min_remaining_pct:g}%)"), ev
        return True, f"{window} has {rem:g}% remaining ({a.source})", ev

    def _recovery(self, account: str, window: str, onset: int, session_key: str | None,
                  now: int) -> dict | None:
        """D8 3(b): a recovery signal linked to this stall, or None."""
        # A linked resets_at: same account and window, observed at or before onset,
        # the first reset after onset among all such records.
        linked = []
        for a in self._anchors(account, now).get(window, []):
            if a.observed_at <= onset and a.resets_at is not None and a.resets_at > onset:
                linked.append((a.resets_at, {"kind": "anchor_resets_at", **a.describe(now)}))
        for src, sk, key, resets_at, conf, obs, value in self.conn.execute(
                "SELECT l.source, l.stream_key, l.source_key, l.resets_at, l.confidence,"
                " l.observed_at, e.value FROM limit_events l JOIN effective_attributions e"
                " ON e.subject_kind = 'limit_event' AND e.dimension = 'account'"
                " AND e.state = 'attributed'"
                " AND e.subject_id = l.source || '|' || l.stream_key || '|' || l.source_key"
                " WHERE l.\"window\" = ? AND l.observed_at <= ? AND l.resets_at > ?",
                (window, onset, onset)):
            if accounts.canonical(self.conn, value) == account:
                linked.append((resets_at, {
                    "kind": "limit_event_resets_at", "source": src, "window": window,
                    "age_s": round((now - obs) / 1000), "confidence": conf,
                    "resets_at": resets_at, "account": account}))
        if linked:
            first, ev = min(linked, key=lambda x: x[0])
            if first <= now:
                return ev
        # A harness reset notice in the same session after this stall's onset.
        if session_key is not None:
            row = self.conn.execute(
                "SELECT source, confidence, observed_at, \"window\" FROM limit_events"
                " WHERE stream_key = ? AND kind = 'reset' AND observed_at > ?"
                " AND (\"window\" IS NULL OR \"window\" = ?) ORDER BY observed_at LIMIT 1",
                (session_key, onset, window)).fetchone()
            if row:
                return {"kind": "reset_notice", "source": row[0], "confidence": row[1],
                        "age_s": round((now - row[2]) / 1000), "window": row[3] or window,
                        "account": account}
        return None

    def _condition_3(self, d: Decision, now, blocking, onset, session_key, known, windows, why):
        if blocking is None:
            # Unknown window: met only when the set is known and every window is clear
            # by a fresh anchor. Recovery signals and stale-window anchors don't apply.
            if not known:
                raise _Wait("c3-window-unknown-set-unknown",
                            f"condition 3: the blocking window is unknown and the set of known "
                            f"windows isn't known ({why})")
            anchors = {}
            for w in sorted(windows):
                ok, msg, ev = self._fresh_clear(d.account, w, now)
                anchors[w] = ev
                if not ok:
                    raise _Wait("c3-window-unknown-not-all-clear",
                                f"condition 3: the blocking window is unknown and {msg}",
                                anchors=anchors, anchor=ev)
            d.evidence["anchors"] = anchors
            return
        ok, msg, ev = self._fresh_clear(d.account, blocking, now)
        d.evidence["anchor"] = ev
        if ok:
            return
        signal = self._recovery(d.account, blocking, onset, session_key, now)
        if signal is None:
            raise _Wait("c3-blocking-not-clear",
                        f"condition 3: blocking window {blocking} is not clear: {msg}, and no "
                        "linked recovery signal")
        d.evidence["signal"] = signal

    def _condition_4(self, d: Decision, now, blocking, known, windows, why):
        if not known:
            raise _Wait("c4-window-set-unknown",
                        f"condition 4: the set of known windows isn't known ({why})")
        if blocking is None:
            return  # condition 3 already required every known window clear by a fresh anchor
        anchors = d.evidence.setdefault("anchors", {})
        for w in sorted(windows - {blocking}):
            ok, msg, ev = self._fresh_clear(d.account, w, now)
            anchors[w] = ev
            if ok:
                continue
            if self.stale_window_fallback and self._stale_clear(d.account, w, now):
                continue
            raise _Wait("c4-window-not-clear", f"condition 4: {msg}")

    def _stale_clear(self, account: str, window: str, now: int) -> bool:
        """D8 condition 4's stale-window anchor (only when the fallback is on)."""
        anchors = self._anchors(account, now).get(window, [])
        if not anchors:
            return False
        a = anchors[0]
        rem = a.remaining()
        if not (a.current(now) and now - a.observed_at <= self.stale_window_max_age_ms
                and a.status != "exhausted" and rem is not None
                and rem >= self.stale_window_min_remaining_pct):
            return False
        for value, in self.conn.execute(
                "SELECT e.value FROM limit_events l JOIN effective_attributions e"
                " ON e.subject_kind = 'limit_event' AND e.dimension = 'account'"
                " AND e.state = 'attributed'"
                " AND e.subject_id = l.source || '|' || l.stream_key || '|' || l.source_key"
                " WHERE l.kind = 'hit' AND l.\"window\" = ? AND l.observed_at >= ?",
                (window, a.observed_at)):
            if accounts.canonical(self.conn, value) == account:
                return False
        return True

    # -- acting ------------------------------------------------------------------------------

    def _pane(self, pane: str) -> topology.Pane:
        """The pane as topology sees it (for config overrides, role, lane state);
        a bare pane when it can't be scanned."""
        if self._panes is None:
            try:
                self._panes = {p.id: p for p in topology.scan().panes}
            except Problem:
                self._panes = {}
        return self._panes.get(pane) or topology.Pane(pane, "", "", "", "", 0, "", "")

    def _nudge(self, d: Decision, now: int, harness, error_key, target) -> Decision:
        """Re-read the pane, then type, only if it still shows this stall, idle and empty."""
        adapter = adapters.by_name(harness) if harness else None
        try:
            if adapter is None:
                raise Problem(f"no adapter for harness {harness!r}",
                              "add the harness's adapter to usage_watch.adapters")
            plain, styled = screen.capture(d.pane)
            reading = adapter.read(plain, styled)
        except Problem as e:
            d.action, d.reason_code = "wait", "c1-reread-failed"
            d.reason = f"condition 1: re-reading the pane before typing failed: {e.what}"
            return self._record(d, now, error_key)
        if reading.state != "stalled" or reading.error_key != error_key:
            d.action, d.reason_code = "wait", "c1-reread-changed"
            d.reason = (f"condition 1: re-read before typing shows {reading.state}"
                        + ("" if reading.state != "stalled" else " with a different error_key")
                        + "; not typing")
            return self._record(d, now, error_key)
        text = self.cfg.nudge_for(target)
        try:
            screen.type_into(d.pane, text)
        except Problem as e:
            d.action, d.reason_code = "escalate", "type-failed"
            d.reason = f"escalate: typing the nudge failed: {e.what}"
            return self._record(d, now, error_key)
        d.evidence["typed"] = text
        return self._record(d, now, error_key)

    def _record(self, d: Decision, now: int, error_key) -> Decision:
        """D8 recording: waits and escalations identical to the stall's latest row
        update it; anything else writes a row. Escalation notifies once per stall.
        With act=False nothing is written or notified."""
        if not self.act:
            return d
        evidence = json.dumps(d.evidence, sort_keys=True, default=str)
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute(
                "SELECT nudge_id, action, reason_code FROM nudges WHERE stall_id = ?"
                " ORDER BY nudge_id DESC LIMIT 1", (d.stall_id,)).fetchone()
            escalated_before = conn.execute(
                "SELECT 1 FROM nudges WHERE stall_id = ? AND action = 'escalate' LIMIT 1",
                (d.stall_id,)).fetchone() is not None
            if (d.action != "nudge" and latest is not None
                    and latest[1:] == (d.action, d.reason_code)):
                conn.execute("UPDATE nudges SET last_seen_at = ?, count = count + 1, evidence = ?"
                             " WHERE nudge_id = ?", (now, evidence, latest[0]))
            else:
                conn.execute(
                    "INSERT INTO nudges (stall_id, pane, session_key, error_key, action,"
                    " reason_code, evidence, decided_at, last_seen_at) VALUES"
                    " (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (d.stall_id, d.pane, d.session_key, error_key, d.action, d.reason_code,
                     evidence, now, now))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if d.action == "escalate" and not escalated_before:
            self.notify("usage-watch", f"{d.pane} ({d.harness}): {d.reason}")
        return d
