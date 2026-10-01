"""Capacity anchors read from the store, for display (views and alerts).

An anchor is a capacity sample whose effective account is attributed. Times
in the store are Unix milliseconds.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .runtime import accounts

__all__ = ["DISPLAY_AGE_MS", "Anchor", "account_anchors"]

MIN = 60 * 1000

# D6: the display age per anchor source (views and alerts).
DISPLAY_AGE_MS: dict[str, int] = {
    "claude.statusline": 15 * MIN,
    "codex.rollout": 15 * MIN,
    "omp.usage_cache": 15 * MIN,
    "claude.cached_utilization": 60 * MIN,
    "omp.usage_history": 120 * MIN,
}


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
        """The anchor's window instance is current when resets_at is in the future.
        Reading: an anchor with no resets_at can't be placed in an instance; not current."""
        return self.resets_at is not None and self.resets_at > now

    def fresh(self, now: int) -> bool:
        """Within its source's display age (D6). A source with no declared age is never fresh."""
        age = DISPLAY_AGE_MS.get(self.source)
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
