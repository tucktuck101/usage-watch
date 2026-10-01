"""A fake machine: tmux panes, processes, git and workmux canned."""

import json
from pathlib import Path

from usage_watch import sh

FIXTURES = Path(__file__).parent / "fixtures"


class Machine:
    def __init__(self, tmp: Path, screens: dict):
        self.tmp = tmp
        self.screens = dict(screens)      # pane id -> fixture name
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
        if prog == "osascript":
            return sh.Result(0, "", "")
        raise AssertionError(f"unexpected command {cmd}")

