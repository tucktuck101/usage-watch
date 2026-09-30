"""The subscription pools, and whether a pane's pool has capacity.

Capacity comes from SOURCES: callables that take `force` and return

    {"providers": {id: {"plan": str, "resources": {name: {
        "kind": "consumption", "remaining": percent, "resetsAt": iso8601}}}},
     "errors": [{"providerId": id, "message": str}]}

No source is registered yet. The direct readers arrive with C1 (see
docs/plan.md). Until then usage-watch cannot confirm capacity, and so never
nudges. A pane draws on one provider: its harness and model family decide
which, and config settles it when more than one account of a family exists.
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from .errors import Problem

SOURCES: list[Callable[[bool], dict]] = []

NO_SOURCE = Problem(
    "usage-watch has no capacity source yet, so it cannot confirm a pool has refilled",
    expected="a direct reader for each provider (plan item C1)",
    fix="nothing to do now: stalled panes wait until C1 lands; see docs/plan.md",
)


@dataclass
class Capacity:
    provider: str
    ok: bool
    session_left: float | None
    weekly_left: float | None
    resets_at: dt.datetime | None   # when a blocking window lifts, if known
    why: str

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "ok": self.ok,
            "session_left": self.session_left, "weekly_left": self.weekly_left,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "why": self.why,
        }


def parse_time(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Pools:
    """One read of every source per scan, refreshed when a reset has passed."""

    def __init__(self):
        self.data: dict | None = None
        self.forced = False

    def load(self, force: bool = False) -> dict:
        if self.data is not None and not (force and not self.forced):
            return self.data
        data: dict = {"providers": {}, "errors": []}
        for source in SOURCES:
            got = source(force)
            data["providers"].update(got.get("providers", {}))
            data["errors"].extend(got.get("errors", []))
        self.data, self.forced = data, self.forced or force
        return data

    def providers(self) -> dict:
        return self.load().get("providers", {})

    def errors(self) -> list[dict]:
        return self.load().get("errors", [])

    def resolve(self, harness: str, family: str | None, accounts: dict) -> str:
        """The provider id a pane draws on, or a Problem saying how to settle it."""
        if not family:
            raise Problem(
                f"cannot tell which model the {harness} pane is using",
                expected="the model name visible on the pane's screen",
                fix=f"set it in config: [accounts.{harness}] default = \"<provider id>\"",
            )
        if not SOURCES:
            raise NO_SOURCE
        chosen = accounts.get(harness, {}).get(family) or accounts.get(harness, {}).get("default")
        if chosen:
            return chosen
        ids = sorted(p for p in self.providers() if p == family or p.startswith(family + "@"))
        if len(ids) == 1:
            return ids[0]
        if not ids:
            raise Problem(
                f"no {family} account found, which the {harness} pane needs",
                expected=f"a provider whose id starts with '{family}'",
                fix=f"sign in to {family} in the harness, then check `usage-watch doctor` lists it",
            )
        raise Problem(
            f"cannot tell which {family} account {harness} uses: found {', '.join(ids)}",
            expected="one account per harness and model family",
            fix=f"run `usage-watch init`, or add to the config file:\n[accounts.{harness}]\n{family} = \"{ids[0]}\"",
        )

    def capacity(self, provider: str, min_left: float, model: str | None = None) -> Capacity:
        info = self.providers().get(provider)
        if info is None:
            raise Problem(
                f"no provider '{provider}' found",
                expected=f"one of: {', '.join(sorted(self.providers())) or 'none reported'}",
                fix="correct the id in the config file, or run `usage-watch init` again",
            )
        res = info.get("resources", {})
        session, weekly = res.get("session", {}), res.get("weekly", {})
        s_left, w_left = session.get("remaining"), weekly.get("remaining")
        # A model-specific weekly pool, such as `fable`, gates panes on that model.
        model_key = (model or "").split()[0].lower() if model else ""
        special = res.get(model_key) if model_key and model_key not in ("session", "weekly") else None

        for name, r, need in (("weekly", weekly, 0.01), (model_key, special or {}, 0.01), ("session", session, min_left)):
            left = r.get("remaining")
            if left is None or left >= need:
                continue
            reset = parse_time(r.get("resetsAt"))
            if reset and reset <= now() and not self.forced:
                self.load(force=True)  # the cache predates the reset
                return self.capacity(provider, min_left, model)
            when = f", resets {reset.astimezone():%a %H:%M}" if reset else ""
            return Capacity(provider, False, s_left, w_left, reset, f"{name} {left}% left, need {need}%{when}")
        return Capacity(provider, True, s_left, w_left, None, f"session {s_left}% left, weekly {w_left}% left")
