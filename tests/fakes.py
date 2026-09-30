"""A fake machine: tmux panes, processes, git, workmux and openusage, all canned."""

import json
from pathlib import Path

from usage_watch import sh

FIXTURES = Path(__file__).parent / "fixtures"


def openusage_json(team_session=92, team_weekly=81, reset="2026-09-30T09:00:00Z"):
    c = {"kind": "consumption"}
    return json.dumps({
        "providers": {
            "claude": {"resources": {"session": {**c, "remaining": 54}, "weekly": {**c, "remaining": 79}}},
            "claude@team": {"resources": {
                "session": {**c, "remaining": team_session, "resetsAt": reset},
                "weekly": {**c, "remaining": team_weekly},
                "fable": {**c, "remaining": 81},
            }},
            "codex": {"resources": {"session": {**c, "remaining": 100}, "weekly": {**c, "remaining": 4}}},
        },
        "errors": [],
    })


class Machine:
    def __init__(self, tmp: Path, screens: dict, usage: str | None = None):
        self.tmp = tmp
        self.screens = dict(screens)      # pane id -> fixture name
        self.usage = usage or openusage_json()
        self.typed: list[tuple[str, str]] = []
        self.after_nudge: dict = {}    # pane id -> fixture shown after typing
        self.project = str(tmp / "proj")
        self.lane = str(tmp / "proj__worktrees" / "lane-a")
        Path(self.lane, ".workmux").mkdir(parents=True, exist_ok=True)

    panes = [
        # id, pid, cwd key, title
        ("%1", 101, "project", "π Orchestrate"),
        ("%2", 102, "lane", "π Lane A"),
        ("%3", 103, "project", "Claude"),
        ("%4", 104, "project", "zsh"),
    ]

    def cwd(self, key):
        return self.project if key == "project" else self.lane

    def __call__(self, cmd, cwd=None, timeout=15):
        prog = cmd[0]
        if prog == "tmux" and cmd[1] == "list-panes":
            rows = [f"{pid}\tdev\t0\tmain\t{i}\t{ppid}\t{self.cwd(c)}\t{t}"
                    for i, (pid, ppid, c, t) in enumerate(self.panes)]
            return sh.Result(0, "\n".join(rows), "")
        if prog == "tmux" and cmd[1] == "capture-pane":
            pane = cmd[cmd.index("-t") + 1]
            if "-e" in cmd:
                return sh.Result(0, "", "")
            return sh.Result(0, (FIXTURES / f"{self.screens[pane]}.txt").read_text(), "")
        if prog == "tmux" and cmd[1] == "send-keys":
            pane = cmd[cmd.index("-t") + 1]
            if "-l" in cmd:
                self.typed.append((pane, cmd[-1]))
                self.screens[pane] = self.after_nudge.get(pane, self.screens[pane])
            return sh.Result(0, "", "")
        if prog == "ps":
            rows = ["101 1 zsh -zsh", "201 101 omp omp", "102 1 zsh -zsh", "202 102 omp omp",
                    "103 1 zsh -zsh", "203 103 claude claude", "104 1 zsh -zsh"]
            return sh.Result(0, "\n".join(rows), "")
        if prog == "git":
            where = cmd[2]
            if where == self.lane:
                return sh.Result(0, f"{self.lane}\n{self.project}/.git\nlane-a\n", "")
            return sh.Result(0, f"{self.project}\n.git\nmain\n", "")
        if prog == "workmux" and cmd[1] == "list":
            return sh.Result(0, json.dumps([
                {"handle": "proj", "path": self.project, "is_main": True},
                {"handle": "lane-a", "path": self.lane, "is_main": False},
            ]), "")
        if prog == "workmux" and cmd[1] == "status":
            return sh.Result(0, json.dumps([{"pane_id": "%2", "status": "done"}]), "")
        if prog == "openusage":
            return sh.Result(0, self.usage, "")
        if prog == "osascript":
            return sh.Result(0, "", "")
        raise AssertionError(f"unexpected command {cmd}")

