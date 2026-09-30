"""The `topology` collector (D6): which checkouts the agent panes are in.

A pull source. Each pass runs `topology.scan()` and emits one
`model.Checkout` per distinct checkout among agent panes (D2):

  checkout_id    keyed hash of the real path of the git common directory,
                 so worktrees of one clone are one checkout; for a directory
                 outside git, a keyed hash of its real path, `non_repo`
  repository_id  keyed hash of the normalised `origin` remote, or None when
                 there is no remote or it has no host (a local path)
  display_name   the main checkout's directory name, with its parent added
                 only when two checkouts in the pass would share a name
  local_path     the main checkout (or the directory); local only (D5)

It has no position, so its watermark is always None. It makes no network
calls: the git commands it runs are local.

Reading: the git common directory is taken as `<main checkout>/.git`,
which is how `topology.git_place` finds the main checkout in the first
place, so the two agree on what one project is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .. import identity, model, sh
from .. import topology as topo

DEFAULT_INTERVAL_S = 60.0


@dataclass
class _Found:
    checkout_id: str
    path: str  # main checkout, or the directory for non_repo
    non_repo: bool
    repository_id: str | None = None


def _repository_id(main: str, secret: bytes | None) -> str | None:
    r = sh.run(["git", "-C", main, "remote", "get-url", "origin"])
    url = r.out.strip() if r.code == 0 else ""
    if not url:
        return None
    try:
        return identity.repository(url, secret)
    except ValueError:  # no host: a local path or file:// remote
        return None


def _display_names(found: list[_Found]) -> dict[str, str]:
    """checkout_id -> label; parent/name only where names collide."""
    by_name: dict[str, set[str]] = {}
    for f in found:
        by_name.setdefault(os.path.basename(f.path), set()).add(f.checkout_id)
    names = {}
    for f in found:
        name = os.path.basename(f.path)
        if len(by_name[name]) > 1:
            name = f"{os.path.basename(os.path.dirname(f.path))}/{name}"
        names[f.checkout_id] = name
    return names


class TopologySource:
    """PullSource for checkouts seen in agent panes."""

    name = "topology"
    primary = False
    merge = None

    def __init__(self, interval_s: float = DEFAULT_INTERVAL_S, secret: bytes | None = None):
        self.interval_s = interval_s
        self._secret = secret  # None: the per-install secret (D5)

    def collect(self, watermark: str | None) -> tuple[list[model.Checkout], None]:
        found: dict[str, _Found] = {}
        seen_projects: set[str] = set()
        for pane in topo.scan().agents:
            if pane.project:
                if pane.project in seen_projects:
                    continue
                seen_projects.add(pane.project)
                common = os.path.realpath(os.path.join(pane.project, ".git"))
                cid = identity.checkout(common, self._secret)
                if cid not in found:
                    found[cid] = _Found(cid, pane.project, False,
                                        _repository_id(pane.project, self._secret))
            elif pane.cwd:
                real = os.path.realpath(pane.cwd)
                cid = identity.checkout(real, self._secret)
                found.setdefault(cid, _Found(cid, pane.cwd, True))
        items = list(found.values())
        names = _display_names(items)
        return [
            model.Checkout(
                checkout_id=f.checkout_id,
                repository_id=f.repository_id,
                display_name=names[f.checkout_id],
                local_path=f.path,
                non_repo=f.non_repo,
            )
            for f in items
        ], None
