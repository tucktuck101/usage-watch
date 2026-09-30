"""Configuration: overrides only. Everything works without a config file,
except choosing between two accounts of the same family, which the screen
cannot show.

    [defaults]
    nudge = "continue"
    interval = 300          # seconds between scans
    min_remaining = 5       # session % a pool needs before a pane is nudged
    max_strikes = 3         # nudges that did not hold before escalating

    [accounts.omp]          # provider id per harness and model family
    claude = "claude@1a2b3c4d"
    codex = "codex"

    [roles.orchestrator]    # nudge text per role: lane, orchestrator, standalone
    nudge = "Usage limit cleared. Continue; if context was lost, re-read your runbook."

    [[override]]            # per pane; match on any of pane, title, cwd_under
    title = "Orchestrate PRD"
    nudge = "..."
    ignore = false
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Problem

DEFAULT_NUDGE = "continue"


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
    nudge: str = DEFAULT_NUDGE
    interval: int = 300
    min_remaining: float = 5
    max_strikes: int = 3
    accounts: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    overrides: list = field(default_factory=list)
    path: Path | None = None
    exists: bool = False

    def rule_for(self, pane) -> dict:
        for o in self.overrides:
            if "pane" in o and o["pane"] != pane.id:
                continue
            if "title" in o and o["title"] not in (pane.title or ""):
                continue
            if "cwd_under" in o and not pane.cwd.startswith(os.path.expanduser(o["cwd_under"])):
                continue
            if not any(k in o for k in ("pane", "title", "cwd_under")):
                continue
            return o
        return {}

    def nudge_for(self, pane) -> str:
        rule = self.rule_for(pane)
        return rule.get("nudge") or self.roles.get(pane.role, {}).get("nudge") or self.nudge

    def ignored(self, pane) -> bool:
        return bool(self.rule_for(pane).get("ignore"))


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise Problem(f"{path} is not valid TOML: {e}",
                      fix=f"correct the file, or move it aside and run `usage-watch init`") from None
    d = raw.get("defaults", {})
    return Config(
        nudge=d.get("nudge", DEFAULT_NUDGE),
        interval=int(d.get("interval", 300)),
        min_remaining=float(d.get("min_remaining", 5)),
        max_strikes=int(d.get("max_strikes", 3)),
        accounts=raw.get("accounts", {}),
        roles=raw.get("roles", {}),
        overrides=raw.get("override", []),
        path=path,
        exists=True,
    )


def render(accounts: dict) -> str:
    """A starter config file holding the chosen accounts."""
    out = [
        "# usage-watch configuration. Run `usage-watch primer` for what each part does.",
        "",
        "[defaults]",
        f'nudge = "{DEFAULT_NUDGE}"',
        "interval = 300",
        "min_remaining = 5",
        "max_strikes = 3",
    ]
    for harness in sorted(accounts):
        out += ["", f"[accounts.{harness}]"]
        out += [f'{family} = "{provider}"' for family, provider in sorted(accounts[harness].items())]
    out += [
        "",
        "# [roles.orchestrator]",
        '# nudge = "Usage limit cleared. Continue; if context was lost, re-read your runbook."',
        "",
        "# [[override]]",
        '# title = "part of a pane title"',
        '# nudge = "text to send instead"',
        "# ignore = false",
        "",
    ]
    return "\n".join(out)
