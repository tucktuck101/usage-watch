"""Build a map of every agent pane: where it is, what runs in it, whose it is.

Rebuilt from scratch on every scan, so panes that come and go, new lanes and
new projects need no configuration. Sources, in order:

  tmux      every pane in every session, with its shell pid and directory
  ps        the process tree under each pane, which names the harness
  git       the project a pane's directory belongs to, and whether it is a
            worktree of that project
  workmux   lanes and their status, where workmux manages the project
"""

import json
import os
import time
from dataclasses import dataclass, field

from . import sh
from .adapters import Adapter, for_process
from .errors import Problem

PANE_FORMAT = "\t".join([
    "#{pane_id}", "#{session_name}", "#{window_index}", "#{window_name}",
    "#{pane_index}", "#{pane_pid}", "#{pane_current_path}", "#{pane_title}",
])


@dataclass
class Pane:
    id: str
    session: str
    window: str
    window_name: str
    index: str
    pid: int
    cwd: str
    title: str
    harness: Adapter | None = None
    project: str | None = None      # main checkout path of the git project
    worktree: str | None = None     # this pane's checkout, when it is a worktree
    branch: str | None = None
    role: str = "standalone"        # lane, orchestrator or standalone
    lane: str | None = None         # workmux handle
    lane_done: bool = False         # the lane has written its handoff
    workmux_status: str | None = None

    @property
    def where(self) -> str:
        return f"{self.session}:{self.window}.{self.index}"

    def as_dict(self) -> dict:
        return {
            "pane": self.id, "where": self.where, "window_name": self.window_name,
            "title": self.title, "cwd": self.cwd,
            "harness": self.harness.name if self.harness else None,
            "project": self.project, "worktree": self.worktree, "branch": self.branch,
            "role": self.role, "lane": self.lane, "lane_done": self.lane_done,
            "workmux_status": self.workmux_status,
        }


@dataclass
class Topology:
    panes: list[Pane] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def agents(self) -> list[Pane]:
        return [p for p in self.panes if p.harness]


def tmux_panes() -> list[Pane]:
    r = sh.run(["tmux", "list-panes", "-a", "-F", PANE_FORMAT])
    if r.code == 127:
        raise Problem(
            "tmux is not installed or not on PATH",
            expected="usage-watch finds agents by reading tmux panes",
            fix="install tmux (macOS: brew install tmux), then run agents inside it",
        )
    if r.code != 0:
        if "no server running" in r.err:
            return []
        raise Problem(f"tmux list-panes failed: {r.err.strip()}",
                      fix="check that `tmux list-panes -a` works in this shell")
    panes = []
    for row in r.out.splitlines():
        f = row.split("\t")
        if len(f) < 8 or not f[5].isdigit():
            continue
        panes.append(Pane(f[0], f[1], f[2], f[3], f[4], int(f[5]), f[6], f[7]))
    return panes


def process_table() -> dict[int, list[tuple[int, str, str]]]:
    """Children of each pid, as (pid, argv0 basename, full args)."""
    r = sh.run(["ps", "-Ao", "pid=,ppid=,comm=,args="])
    children: dict[int, list[tuple[int, str, str]]] = {}
    for row in r.out.splitlines():
        parts = row.split(None, 3)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid, comm = int(parts[0]), int(parts[1]), parts[2]
        args = parts[3] if len(parts) > 3 else comm
        children.setdefault(ppid, []).append((pid, os.path.basename(comm), args))
    return children


def find_harness(pid: int, children) -> Adapter | None:
    """Breadth-first under the pane's shell, nearest harness wins."""
    queue, seen = [pid], set()
    while queue:
        p = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        for child, argv0, args in children.get(p, []):
            adapter = for_process(argv0, args)
            if adapter:
                return adapter
            queue.append(child)
    return None


def git_place(cwd: str, cache: dict) -> tuple[str | None, str | None, str | None]:
    """(project main checkout, worktree path or None, branch) for a directory."""
    if cwd in cache:
        return cache[cwd]
    r = sh.run(["git", "-C", cwd, "rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD"])
    place = (None, None, None)
    if r.code == 0:
        top, common, branch = (r.out.splitlines() + ["", "", ""])[:3]
        common = os.path.normpath(os.path.join(top, common)) if not os.path.isabs(common) else common
        main = os.path.dirname(common) if os.path.basename(common) == ".git" else top
        place = (main, None if main == top else top, branch or None)
    cache[cwd] = place
    return place


TREES_TTL = 60  # `workmux list` is slow and worktrees change rarely
_trees_cache: dict[str, tuple[float, dict]] = {}


def workmux_trees(project: str) -> dict:
    hit = _trees_cache.get(project)
    if hit and time.monotonic() - hit[0] < TREES_TTL:
        return hit[1]
    trees = {}
    r = sh.run(["workmux", "list", "--json"], cwd=project)
    if r.code == 0:
        try:
            for t in json.loads(r.out):
                trees[os.path.normpath(t["path"])] = t
        except (ValueError, KeyError, TypeError):
            pass
    _trees_cache[project] = (time.monotonic(), trees)
    return trees


def workmux_facts(project: str) -> tuple[dict, dict]:
    """(worktrees by path, status by pane id) for one project, if workmux manages it."""
    trees, status = workmux_trees(project), {}
    r = sh.run(["workmux", "status", "--json"], cwd=project)
    if r.code == 0:
        try:
            for s in json.loads(r.out):
                if s.get("pane_id"):
                    status[s["pane_id"]] = s
        except (ValueError, TypeError):
            pass
    return trees, status


def scan() -> Topology:
    topo = Topology(panes=tmux_panes())
    children = process_table()
    cache: dict = {}
    for p in topo.panes:
        p.harness = find_harness(p.pid, children)
        if p.harness:
            p.project, p.worktree, p.branch = git_place(p.cwd, cache)

    has_workmux = sh.which("workmux") is not None
    projects = {p.project for p in topo.agents if p.project}
    for project in sorted(projects):
        trees, status = workmux_facts(project) if has_workmux else ({}, {})
        members = [p for p in topo.agents if p.project == project]
        has_lanes = any(not t.get("is_main") for t in trees.values()) or any(p.worktree for p in members)
        for p in members:
            here = os.path.normpath(p.worktree or p.project)
            tree = trees.get(here)
            if p.worktree:
                p.role = "lane"
                p.lane = tree["handle"] if tree else os.path.basename(p.worktree)
                p.lane_done = os.path.exists(os.path.join(p.worktree, ".workmux", "HANDOFF.md"))
            elif has_lanes:
                p.role = "orchestrator"
            if p.id in status:
                p.workmux_status = status[p.id].get("status")
    if not has_workmux:
        topo.notes.append("workmux not found: lanes are inferred from git worktrees alone")
    return topo
