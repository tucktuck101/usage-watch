import json
from pathlib import Path

import pytest

from usage_watch import cli, config, pool, sh, watcher
from usage_watch.errors import Problem
from usage_watch.pool import Pools
from usage_watch.topology import scan

from fakes import Machine, usage_json

ACCOUNTS = {"omp": {"claude": "claude@team"}, "claude": {"claude": "claude"}}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    def make(screens, usage=None, accounts=ACCOUNTS, overrides=()):
        m = Machine(tmp_path, screens, usage)
        monkeypatch.setattr(sh, "run", m)
        monkeypatch.setattr(pool, "SOURCES", [m.source])
        monkeypatch.setattr(sh, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        m.cfg = config.Config(accounts=accounts, overrides=list(overrides))
        return m
    return make


def screens(**kw):
    base = {"%1": "omp_idle", "%2": "omp_busy", "%3": "claude_idle"}
    base.update({f"%{k[1:]}": v for k, v in kw.items()})
    return base


def run_scan(m, memory=None):
    return {o.pane.id: o for o in watcher.observe(scan(), m.cfg, Pools(), memory or watcher.Memory())}


def test_topology_finds_harness_project_and_role(machine):
    m = machine(screens())
    panes = {p.id: p for p in scan().agents}
    assert set(panes) == {"%1", "%2", "%3"}          # %4 is a bare shell
    assert panes["%1"].harness.name == "omp" and panes["%1"].role == "orchestrator"
    assert panes["%2"].role == "lane" and panes["%2"].lane == "lane-a"
    assert panes["%2"].workmux_status == "done"
    assert panes["%3"].harness.name == "claude"
    assert panes["%1"].project == m.project == panes["%2"].project


def test_stalled_pane_with_capacity_is_nudged_once(machine):
    m = machine(screens(p1="omp_stalled_plain"))
    m.after_nudge = {"%1": "omp_busy"}
    obs = run_scan(m)
    assert obs["%1"].action == "nudge" and obs["%1"].provider == "claude@team"
    memory = watcher.Memory()
    result = watcher.nudge(obs["%1"], m.cfg, memory, settle=1)
    assert m.typed == [("%1", "continue")]
    assert "now busy" in result


def test_repeated_restalls_escalate_until_the_pane_works(machine):
    m = machine(screens(p1="omp_stalled_plain"))
    memory = watcher.Memory()
    for key in ("a", "b", "c"):  # three different stalls, never seen busy in between
        o = run_scan(m, memory)["%1"]
        o.reading.error_key = key
        assert o.action == "nudge"
        watcher.nudge(o, m.cfg, memory, settle=0)
    assert run_scan(m, memory)["%1"].action == "escalate"
    m.screens["%1"] = "omp_busy"
    run_scan(m, memory)                       # seen working: record cleared
    m.screens["%1"] = "omp_stalled_plain"
    assert run_scan(m, memory)["%1"].action == "nudge"


def test_no_capacity_means_wait(machine):
    m = machine(screens(p1="omp_stalled_plain"), usage=usage_json(team_session=0))
    o = run_scan(m)["%1"]
    assert o.action == "wait" and "session 0% left" in o.reason
    assert m.typed == []


def test_weekly_exhausted_means_wait(machine):
    m = machine(screens(p1="omp_stalled_plain"), usage=usage_json(team_weekly=0))
    assert run_scan(m)["%1"].action == "wait"


def test_never_types_over_text(machine):
    m = machine(screens(p1="omp_owner_typing", p3="claude_typing"))
    obs = run_scan(m)
    assert obs["%1"].reading.state == "typing" and obs["%1"].action == "none"
    assert obs["%3"].reading.state == "typing" and obs["%3"].action == "none"


def test_self_resuming_claude_is_left_alone(machine):
    m = machine(screens(p3="claude_resuming"))
    assert run_scan(m)["%3"].action == "none"


def test_finished_lane_is_not_nudged(machine):
    m = machine(screens(p2="omp_stalled_todo"))
    Path(m.lane, ".workmux", "HANDOFF.md").write_text("done")
    o = run_scan(m)["%2"]
    assert o.action == "none" and "handoff" in o.reason


def test_same_stall_after_nudge_escalates(machine):
    m = machine(screens(p1="omp_stalled_plain"))
    memory = watcher.Memory()
    first = run_scan(m, memory)["%1"]
    watcher.nudge(first, m.cfg, memory, settle=0)   # the screen does not change
    again = run_scan(m, memory)["%1"]
    assert again.action == "escalate" and "still stalled after a nudge" in again.reason


def test_two_accounts_without_config_escalates_with_the_fix(machine):
    m = machine(screens(p1="omp_stalled_plain"), accounts={})
    o = run_scan(m)["%1"]
    assert o.action == "escalate"
    assert "claude, claude@team" in o.reason and "[accounts.omp]" in o.reason


def test_ignored_and_overridden_nudge(machine):
    m = machine(screens(p1="omp_stalled_plain"), overrides=[{"title": "Orchestrate", "ignore": True}])
    assert run_scan(m)["%1"].reason == "ignored by config"
    m = machine(screens(p1="omp_stalled_plain"), overrides=[{"pane": "%1", "nudge": "resume please"}])
    watcher.nudge(run_scan(m)["%1"], m.cfg, watcher.Memory(), settle=0)
    assert m.typed[-1] == ("%1", "resume please")


def test_role_nudge_text(machine):
    m = machine(screens(p1="omp_stalled_plain"))
    m.cfg.roles = {"orchestrator": {"nudge": "re-read the runbook"}}
    watcher.nudge(run_scan(m)["%1"], m.cfg, watcher.Memory(), settle=0)
    assert m.typed[-1] == ("%1", "re-read the runbook")


def test_run_once_nudges_and_logs(machine, tmp_path, capsys):
    m = machine(screens(p1="omp_stalled_plain"))
    m.after_nudge = {"%1": "omp_busy"}
    watcher.run(m.cfg, once=True)
    assert m.typed == [("%1", "continue")]
    log = (tmp_path / "state" / "usage-watch" / "usage-watch.log").read_text()
    assert "stalled" in log and "typed 'continue'" in log


def test_second_watcher_is_refused(machine):
    machine(screens())
    lock = watcher.acquire_lock()
    with pytest.raises(Problem, match="already running"):
        watcher.acquire_lock()
    lock.close()


def test_nudge_command_refuses_busy_pane_with_a_fix(machine, capsys, monkeypatch, tmp_path):
    machine(screens())
    monkeypatch.setenv("USAGE_WATCH_CONFIG", str(tmp_path / "none.toml"))
    assert cli.main(["nudge", "%2"]) == 1
    err = capsys.readouterr().err
    assert "%2 is busy" in err and "fix:" in err


def test_without_a_capacity_source_stalled_panes_wait_quietly(machine, monkeypatch):
    m = machine(screens(p1="omp_stalled_plain"))
    monkeypatch.setattr(pool, "SOURCES", [])
    o = run_scan(m)["%1"]
    assert o.action == "wait" and "no capacity source" in o.reason
    assert m.typed == []
