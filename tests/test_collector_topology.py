"""The topology collector (F3): checkouts from agent panes, and their write path."""

import os
import sqlite3

import pytest

from usage_watch import identity, model, sh, store
from usage_watch.collectors.topology import TopologySource
from usage_watch.runtime.core import Runtime, WritePath

SECRET = b"s" * 32


class Box:
    """A fake machine: agent panes in given directories, git answers per directory.

    `repos` maps a directory to (main checkout, top level, branch); a directory
    not in it is outside git. `remotes` maps a main checkout to its origin URL
    (absent: no remote).
    """

    def __init__(self, cwds, repos, remotes):
        self.cwds, self.repos, self.remotes = cwds, repos, remotes
        self.calls = []

    def __call__(self, cmd, cwd=None, timeout=15):
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "list-panes"]:
            rows = [f"%{i}\tdev\t0\tw\t{i}\t{100 + i}\t{c}\tt" for i, c in enumerate(self.cwds)]
            return sh.Result(0, "\n".join(rows), "")
        if cmd[0] == "ps":
            rows = []
            for i in range(len(self.cwds)):
                rows += [f"{100 + i} 1 zsh -zsh", f"{200 + i} {100 + i} claude claude"]
            return sh.Result(0, "\n".join(rows), "")
        if cmd[0] == "git" and cmd[3] == "rev-parse":
            if cmd[2] not in self.repos:
                return sh.Result(128, "", "fatal: not a git repository")
            main, top, branch = self.repos[cmd[2]]
            return sh.Result(0, f"{top}\n{main}/.git\n{branch}\n", "")
        if cmd[0] == "git" and cmd[3:] == ["remote", "get-url", "origin"]:
            url = self.remotes.get(cmd[2])
            return sh.Result(0, url + "\n", "") if url else sh.Result(2, "", "error: No such remote 'origin'")
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(sh, "which", lambda name: None)


def machine(monkeypatch, cwds, repos, remotes=None):
    box = Box(cwds, repos, remotes or {})
    monkeypatch.setattr(sh, "run", box)
    return box


def collect(**kw):
    items, wm = TopologySource(secret=SECRET, **kw).collect(None)
    assert wm is None
    return items


def test_satisfies_pull_source():
    src = TopologySource()
    assert (src.name, src.primary, src.merge, src.interval_s) == ("topology", False, None, 60)
    assert TopologySource(interval_s=5).interval_s == 5


def test_two_worktrees_of_one_repository_are_one_checkout(monkeypatch, tmp_path):
    main, lane = str(tmp_path / "proj"), str(tmp_path / "proj__worktrees" / "a")
    machine(monkeypatch, [main, lane, main],
            {main: (main, main, "main"), lane: (main, lane, "a")},
            {main: "git@github.com:Org/Proj.git"})
    [c] = collect()
    assert c == model.Checkout(
        checkout_id=identity.checkout(os.path.realpath(f"{main}/.git"), SECRET),
        repository_id=identity.repository("https://github.com/org/proj", SECRET),
        display_name="proj", local_path=main, non_repo=False)


def test_alike_names_are_disambiguated_with_distinct_ids(monkeypatch, tmp_path):
    a, b = str(tmp_path / "work" / "app"), str(tmp_path / "play" / "app")
    machine(monkeypatch, [a, b], {a: (a, a, "main"), b: (b, b, "main")})
    items = collect()
    assert sorted(c.display_name for c in items) == ["play/app", "work/app"]
    assert len({c.checkout_id for c in items}) == 2


def test_unique_name_keeps_bare_name(monkeypatch, tmp_path):
    a, b = str(tmp_path / "x" / "app"), str(tmp_path / "y" / "lib")
    machine(monkeypatch, [a, b], {a: (a, a, "m"), b: (b, b, "m")})
    assert sorted(c.display_name for c in collect()) == ["app", "lib"]


def test_ssh_and_https_remotes_give_one_repository_id(monkeypatch, tmp_path):
    a, b = str(tmp_path / "one" / "r"), str(tmp_path / "two" / "r")
    machine(monkeypatch, [a, b], {a: (a, a, "m"), b: (b, b, "m")},
            {a: "git@github.com:org/r.git", b: "https://user@github.com/org/r"})
    x, y = collect()
    assert x.repository_id and x.repository_id == y.repository_id
    assert x.checkout_id != y.checkout_id


def test_no_remote_gives_none(monkeypatch, tmp_path):
    a = str(tmp_path / "r")
    machine(monkeypatch, [a], {a: (a, a, "m")})
    [c] = collect()
    assert c.repository_id is None and not c.non_repo


def test_local_path_remote_gives_none(monkeypatch, tmp_path):
    a = str(tmp_path / "r")
    machine(monkeypatch, [a], {a: (a, a, "m")}, {a: "/srv/git/r.git"})
    [c] = collect()
    assert c.repository_id is None


def test_non_repo_cwd(monkeypatch, tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    machine(monkeypatch, [str(d)], {})
    [c] = collect()
    assert c.non_repo and c.repository_id is None
    assert c.checkout_id == identity.checkout(str(d.resolve()), SECRET)
    assert c.display_name == "notes"


def test_non_agent_panes_are_ignored(monkeypatch, tmp_path):
    box = machine(monkeypatch, [str(tmp_path)], {})
    orig = box.__call__

    def run(cmd, cwd=None, timeout=15):
        if cmd[0] == "ps":
            return sh.Result(0, "100 1 zsh -zsh", "")
        return orig(cmd, cwd, timeout)
    monkeypatch.setattr(sh, "run", run)
    assert collect() == []


# --- the write path ---------------------------------------------------------------

class Src:
    name, primary, merge, interval_s = "topology", False, None, 60


def test_write_path_upserts_checkouts_without_blanking(tmp_path):
    conn = store.connect(tmp_path / "u.db")
    store.migrate(conn)
    wp = WritePath(conn)
    assert wp.apply(Src(), [model.Checkout("c1", "r1", "proj", "/p", False)])
    assert wp.apply(Src(), [model.Checkout("c1", None, None, None, False),
                            model.Checkout("c2", non_repo=True)])
    rows = conn.execute("SELECT checkout_id, repository_id, display_name, local_path, non_repo"
                        " FROM checkouts ORDER BY checkout_id").fetchall()
    assert rows == [("c1", "r1", "proj", "/p", 0), ("c2", None, None, None, 1)]
    assert wp.apply(Src(), [model.Checkout("c2", "r2", "notes", "/n", True)])
    assert conn.execute("SELECT repository_id, display_name FROM checkouts WHERE checkout_id = 'c2'"
                        ).fetchone() == ("r2", "notes")


def test_runtime_integration(monkeypatch, tmp_path):
    main, lane, other = (str(tmp_path / "proj"), str(tmp_path / "proj__wt" / "a"),
                         str(tmp_path / "loose"))
    box = machine(monkeypatch, [main, lane, other],
                  {main: (main, main, "main"), lane: (main, lane, "a")},
                  {main: "https://github.com/org/proj.git"})
    clock = [1000.0]
    rt = Runtime([TopologySource(secret=SECRET)], db_path=tmp_path / "usage.db",
                 lock_path=tmp_path / "run.lock", clock=lambda: clock[0])
    rt.start()
    try:
        rt.run_once()
        box.remotes.clear()  # the remote disappears: a later pass must not blank it
        clock[0] += 61
        rt.run_once()
    finally:
        rt.stop()
    conn = sqlite3.connect(tmp_path / "usage.db")
    rows = conn.execute("SELECT checkout_id, repository_id, display_name, local_path, non_repo"
                        " FROM checkouts ORDER BY non_repo").fetchall()
    assert rows == [
        (identity.checkout(os.path.realpath(f"{main}/.git"), SECRET),
         identity.repository("git@github.com:org/proj", SECRET),
         "proj", main, 0),
        (identity.checkout(os.path.realpath(other), SECRET), None, "loose", other, 1),
    ]
    assert conn.execute("SELECT last_error FROM collector_status WHERE collector = 'topology'"
                        ).fetchone() == (None,)
    assert sum(c[3:] == ["remote", "get-url", "origin"] for c in box.calls) == 2
