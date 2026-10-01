"""The `panes` collector: session -> pane evidence from Claude's session files,
omp's terminal-session files, and the session file an omp or Codex process
holds open. Fixtures under fixtures/panes are synthetic."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from usage_watch import sh, topology
from usage_watch.collectors import panes
from usage_watch.model import AttributionEvidence
from usage_watch.runtime import attribution
from usage_watch.runtime.core import Runtime

FIXTURES = Path(__file__).parent / "fixtures" / "panes"
T0 = 1_790_000_000.0
ALIVE = {4242, 4244, 4245}
CLAUDE_SID = "c1aade00-0000-4000-8000-000000000001"
OMP_SID = "0a1b2c3d4e5f6a7b"
OMP_SID2 = "9f8e7d6c5b4a3f2e"
CODEX_SID = "c0dec0de-0000-4000-8000-000000000001"
CODEX_FILE = "rollout-2026-10-01T09-00-00-c0dec0de.jsonl"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Claude session files, omp terminal-session files, and tmux with panes %20
    and %12. Under %20's shell runs omp 201, holding s1 open; under %12's, claude.
    Panes %30 (omp) and %31 (Codex) exist only when a test adds them to `tmux`."""
    claude = tmp_path / "claude-sessions"
    shutil.copytree(FIXTURES / "claude", claude)
    omp_sessions = tmp_path / "omp-sessions"
    shutil.copytree(FIXTURES / "omp", omp_sessions / "--home-dev-proj--")
    codex_sessions = tmp_path / "codex-sessions"
    shutil.copytree(FIXTURES / "codex", codex_sessions / "2026" / "10" / "01")
    s1 = str(omp_sessions / "--home-dev-proj--" / "s1.jsonl")
    s2 = str(omp_sessions / "--home-dev-proj--" / "s2.jsonl")
    rollout = str(codex_sessions / "2026" / "10" / "01" / CODEX_FILE)
    terminal = tmp_path / "terminal-sessions"
    terminal.mkdir()
    # %20 is live; %21 is gone from tmux; ttys files are not panes.
    (terminal / "tmux-%20").write_text(f"/home/dev/proj\n{s1}\n")
    (terminal / "tmux-%21").write_text(f"/home/dev/proj\n{s2}\n")
    (terminal / "ttys003").write_text(f"/home/dev/proj\n{s2}\n")

    tmux_panes = {"%12", "%20"}
    shells = {"%12": 100, "%20": 200, "%30": 300, "%31": 310}
    ps = [(101, 100, "claude"), (200, 1, "-zsh"), (201, 200, "omp"), (301, 300, "omp"),
          (311, 310, "codex"), (312, 311, "rg")]
    open_files = {
        201: ["/dev/ttys001", s1, "/usr/lib/libc.dylib"],
        301: ["/dev/ttys002", str(tmp_path), s2, s2],  # one file on two fds
        311: ["/dev/ttys003", rollout, str(tmp_path / "elsewhere.jsonl")],
    }
    calls = []

    def run(cmd, cwd=None, timeout=15):
        calls.append(cmd)
        if cmd == ["tmux", "list-panes", "-a", "-F", topology.PANE_FORMAT]:
            return sh.Result(0, "".join(
                f"{p}\twork\t1\tw\t0\t{shells[p]}\t/home/dev/proj\tt\n"
                for p in sorted(tmux_panes)), "")
        if cmd == ["ps", "-Ao", "pid=,ppid=,comm=,args="]:
            return sh.Result(0, "".join(f"{pid} {ppid} {comm} {comm} --x\n"
                                        for pid, ppid, comm in ps), "")
        if cmd[:2] == ["lsof", "-p"] and cmd[3:] == ["-Fn"]:
            pid = int(cmd[2])
            out = f"p{pid}\n" + "".join(f"f{i}\nn{f}\n" for i, f in enumerate(open_files.get(pid, [])))
            return sh.Result(0 if pid in open_files else 1, out, "")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(sh, "run", run)
    monkeypatch.setattr(panes, "_alive", lambda pid: pid in ALIVE)
    monkeypatch.setattr(panes, "PLATFORM", "darwin")
    return {"claude": claude, "terminal": terminal, "tmux": tmux_panes, "calls": calls,
            "omp_sessions": omp_sessions, "codex_sessions": codex_sessions,
            "open_files": open_files, "ps": ps, "s1": s1, "s2": s2, "rollout": rollout}


def source(m, t=T0):
    return panes.PanesSource(claude_sessions=m["claude"], omp_terminal_sessions=m["terminal"],
                             omp_sessions=m["omp_sessions"], codex_sessions=m["codex_sessions"],
                             interval_s=0, clock=Clock(t))


def by_subject(items):
    return {ev.subject_id: ev for ev in items}


def test_contract(machine):
    s = source(machine)
    assert s.name == "panes" and s.primary is False and s.merge is None
    assert panes.PanesSource().interval_s == 15


def test_both_methods_emit_live_authoritative_evidence(machine):
    items, _ = source(machine).collect(None)
    now = int(T0 * 1000)
    assert by_subject(items) == {
        f"claude:{CLAUDE_SID}": AttributionEvidence(
            subject_kind="session", subject_id=f"claude:{CLAUDE_SID}", dimension="pane",
            value="%12", method="claude_session_file", source="panes",
            confidence="authoritative", validity="live",
            first_observed_at=now, last_confirmed_at=now),
        f"omp:{OMP_SID}": AttributionEvidence(
            subject_kind="session", subject_id=f"omp:{OMP_SID}", dimension="pane",
            value="%20", method="omp_terminal_session", source="panes",
            confidence="authoritative", validity="live",
            first_observed_at=now, last_confirmed_at=now),
    }


def test_dead_pid_emits_nothing(machine, monkeypatch):
    monkeypatch.setattr(panes, "_alive", lambda pid: False)
    items, _ = source(machine).collect(None)
    assert [ev.method for ev in items] == ["omp_terminal_session"]


def test_missing_pane_emits_nothing(machine):
    machine["tmux"].clear()
    items, _ = source(machine).collect(None)
    assert [ev.method for ev in items] == ["claude_session_file"]


def test_no_tmux_server_emits_no_omp_evidence(machine, monkeypatch):
    monkeypatch.setattr(sh, "run", lambda cmd, cwd=None, timeout=15:
                        sh.Result(1, "", "no server running on /tmp/tmux-501/default"))
    items, _ = source(machine).collect(None)
    assert {ev.method for ev in items} == {"claude_session_file"}


def test_malformed_or_absent_tmux_field_is_skipped(machine):
    # 4244 (alive, malformed tmux) and 4245 (alive, no tmux) emit nothing.
    items, _ = source(machine).collect(None)
    assert not any(ev.subject_id.endswith(("0003", "0004")) for ev in items)
    for bad in [None, 12, "", "work", "work:@3", "work:@3.12", "work:3.%12", "work:@3.%12x",
                "work:@3.%12.%13"]:
        assert panes.pane_of(bad) is None
    assert panes.pane_of("my-session_1:@0.%7") == "%7"
    assert panes.pane_of(":@0.%7") == "%7"


def test_unreadable_files_are_skipped(machine):
    (machine["claude"] / "9999.json").write_text("{not json")
    (machine["claude"] / "9998.json").write_text("[]")
    (machine["terminal"] / "tmux-%12").write_text("/home/dev/proj\n/nowhere/gone.jsonl\n")
    (machine["terminal"] / "tmux-x").write_text("garbage")
    items, _ = source(machine).collect(None)
    assert len(items) == 2


def test_missing_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "run", lambda *a, **k: sh.Result(127, "", "tmux: not found"))
    s = panes.PanesSource(claude_sessions=tmp_path / "a", omp_terminal_sessions=tmp_path / "b",
                          omp_sessions=tmp_path / "c", codex_sessions=tmp_path / "d")
    assert s.collect(None) == ([], "{}")


def test_window_grows_across_passes(machine):
    first, wm = source(machine, T0).collect(None)
    second, wm = source(machine, T0 + 15).collect(wm)
    third, wm = source(machine, T0 + 30).collect(wm)
    for ev in by_subject(third).values():
        assert ev.first_observed_at == int(T0 * 1000)
        assert ev.last_confirmed_at == int((T0 + 30) * 1000)
    assert json.loads(wm) == {f"claude:{CLAUDE_SID}|%12": int(T0 * 1000),
                              f"omp:{OMP_SID}|%20": int(T0 * 1000)}


def test_a_broken_pairing_starts_a_new_window(machine):
    _, wm = source(machine, T0).collect(None)
    machine["tmux"].discard("%20")
    _, wm = source(machine, T0 + 15).collect(wm)
    machine["tmux"].add("%20")
    items, _ = source(machine, T0 + 30).collect(wm)
    assert by_subject(items)[f"omp:{OMP_SID}"].first_observed_at == int((T0 + 30) * 1000)
    assert by_subject(items)[f"claude:{CLAUDE_SID}"].first_observed_at == int(T0 * 1000)


def test_unreadable_watermark_starts_fresh(machine):
    for wm in ["not json", "[]", '{"x": "y"}']:
        items, _ = source(machine).collect(wm)
        assert all(ev.first_observed_at == int(T0 * 1000) for ev in items)


def test_alive_uses_signal_zero():
    assert panes._alive(os.getpid())
    assert not panes._alive(None) and not panes._alive(True) and not panes._alive(0)
    assert not panes._alive(2**70)  # overflow is caught


def test_through_the_runtime_into_the_store(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store_db = tmp_path / "usage.db"
    clock = Clock(T0)
    machine["tmux"].add("%30")
    s = panes.PanesSource(claude_sessions=machine["claude"],
                          omp_terminal_sessions=machine["terminal"],
                          omp_sessions=machine["omp_sessions"],
                          codex_sessions=machine["codex_sessions"], interval_s=0, clock=clock)
    rt = Runtime([s], db_path=store_db, lock_path=tmp_path / "run.lock", clock=clock)
    rt.start()
    try:
        rt.run_once()
        clock.t = T0 + 15
        rt.run_once()
    finally:
        rt.stop()
    conn = sqlite3.connect(store_db)
    try:
        errors = conn.execute("select collector, last_error from collector_status").fetchall()
        assert all(e is None for _, e in errors), errors
        for skey, pane, conf in [(f"claude:{CLAUDE_SID}", "%12", "authoritative"),
                                 (f"omp:{OMP_SID}", "%20", "authoritative"),
                                 (f"omp:{OMP_SID2}", "%30", "observed")]:
            eff = attribution.effective(conn, "session", skey, "pane")
            assert (eff.state, eff.value, eff.confidence) == ("attributed", pane, conf)
        rows = conn.execute(
            "select first_observed_at, last_confirmed_at from attribution_evidence"
            " where dimension = 'pane'").fetchall()
        assert rows == [(int(T0 * 1000), int((T0 + 15) * 1000))] * 3
    finally:
        conn.close()


# -- open_session_file: omp and Codex, from the harness's open files --

def observed(skey, pane, t=T0):
    now = int(t * 1000)
    return AttributionEvidence(
        subject_kind="session", subject_id=skey, dimension="pane", value=pane,
        method="open_session_file", source="panes", confidence="observed", validity="live",
        first_observed_at=now, last_confirmed_at=now)


def test_open_session_file_joins_omp_and_codex_through_lsof(machine):
    machine["tmux"].update({"%30", "%31"})
    items = by_subject(source(machine).collect(None)[0])
    assert items[f"omp:{OMP_SID2}"] == observed(f"omp:{OMP_SID2}", "%30")
    assert items[f"codex:{CODEX_SID}"] == observed(f"codex:{CODEX_SID}", "%31")
    assert ["lsof", "-p", "301", "-Fn"] in machine["calls"]
    assert ["lsof", "-p", "311", "-Fn"] in machine["calls"]


def test_open_session_file_on_linux_reads_proc(machine, tmp_path, monkeypatch):
    machine["tmux"].update({"%30", "%31"})
    proc = tmp_path / "proc"
    for pid, files in machine["open_files"].items():
        fd = proc / str(pid) / "fd"
        fd.mkdir(parents=True)
        for i, target in enumerate(files):
            os.symlink(target, fd / str(i))
    (proc / "301" / "fd" / "9").mkdir()  # not a link: skipped
    monkeypatch.setattr(panes, "PLATFORM", "linux")
    monkeypatch.setattr(panes, "PROC", proc)
    items = by_subject(source(machine).collect(None)[0])
    assert items[f"omp:{OMP_SID2}"].value == "%30"
    assert items[f"codex:{CODEX_SID}"].value == "%31"
    assert not any(c[0] == "lsof" for c in machine["calls"])
    assert panes.open_files(4040) == []  # no such process


def test_session_ids_are_read_from_the_headers(machine):
    assert panes.codex_session_id(machine["rollout"]) == CODEX_SID  # first session_meta, not the parent's
    assert panes.codex_session_id("/nowhere/gone.jsonl") is None
    bad = machine["codex_sessions"] / "bad.jsonl"
    bad.write_text('{"type":"turn_context"}\n{"type":"session_meta","payload":{"id":7}}\n')
    assert panes.codex_session_id(str(bad)) is None
    machine["tmux"].add("%30")
    items = by_subject(source(machine).collect(None)[0])
    assert f"omp:{OMP_SID2}" in items  # s2's `type:"session"` header id


def test_an_authoritative_method_wins_over_open_session_file(machine):
    # omp 201 in %20 holds a different session open than its terminal-session
    # file names: the pane already has authoritative evidence, so it's skipped.
    machine["open_files"][201] = [machine["s2"]]
    items = source(machine).collect(None)[0]
    assert {(ev.subject_id, ev.method) for ev in items} == {
        (f"claude:{CLAUDE_SID}", "claude_session_file"),
        (f"omp:{OMP_SID}", "omp_terminal_session")}
    assert not any(c[0] == "lsof" for c in machine["calls"])
    # Without the terminal-session file, the same pane joins by its open file.
    (machine["terminal"] / "tmux-%20").unlink()
    items = by_subject(source(machine).collect(None)[0])
    assert items[f"omp:{OMP_SID2}"] == observed(f"omp:{OMP_SID2}", "%20")


@pytest.mark.parametrize("files", [
    [],                                     # lsof lists nothing
    ["/dev/ttys002", "/tmp/notes.txt"],     # no .jsonl
    ["/elsewhere/s.jsonl"],                 # a .jsonl outside the session root
    ["CODEX_ROLLOUT"],                      # another harness's session root
    ["S1", "S2"],                           # two session files: ambiguous
    ["GONE"],                               # under the root, but no header
])
def test_no_matching_open_file_gives_no_evidence(machine, files):
    machine["tmux"].add("%30")
    swap = {"CODEX_ROLLOUT": machine["rollout"], "S1": machine["s1"], "S2": machine["s2"],
            "GONE": str(machine["omp_sessions"] / "gone.jsonl")}
    machine["open_files"][301] = [swap.get(f, f) for f in files]
    items = source(machine).collect(None)[0]
    assert not any(ev.value == "%30" for ev in items)


def test_harness_pid_walks_the_pane_process_tree():
    children = {1: [(2, "zsh", "zsh")], 2: [(3, "claude", "claude"), (4, "bash", "bash")],
                4: [(5, "codex", "codex resume")], 5: [(6, "omp", "omp")]}
    assert panes.harness_pid(1, children) == (5, "codex")
    assert panes.harness_pid(2, {2: [(3, "claude", "claude")]}) is None
    assert panes.harness_pid(9, {}) is None


def test_codex_root_follows_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert panes.default_codex_sessions() == tmp_path / "sessions"
    monkeypatch.delenv("CODEX_HOME")
    assert panes.default_codex_sessions() == Path("~/.codex/sessions").expanduser()
