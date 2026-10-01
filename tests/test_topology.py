"""Topology scan over a fake machine, and the adapters reading its panes."""

from pathlib import Path

import pytest

from usage_watch import screen, sh
from usage_watch.topology import scan

from fakes import Machine


@pytest.fixture
def machine(tmp_path, monkeypatch):
    def make(screens):
        m = Machine(tmp_path, screens)
        monkeypatch.setattr(sh, "run", m)
        monkeypatch.setattr(sh, "which", lambda name: f"/usr/bin/{name}")
        return m
    return make


def screens(**kw):
    base = {"%1": "omp_idle", "%2": "omp_busy", "%3": "claude_idle"}
    base.update({f"%{k[1:]}": v for k, v in kw.items()})
    return base


def read_all():
    out = {}
    for pane in scan().agents:
        plain, styled = screen.capture(pane.id)
        out[pane.id] = (pane, pane.harness.read(plain, styled))
    return out


def test_topology_finds_harness_project_and_role(machine):
    m = machine(screens())
    panes = {p.id: p for p in scan().agents}
    assert set(panes) == {"%1", "%2", "%3"}          # %4 is a bare shell
    assert panes["%1"].harness.name == "omp" and panes["%1"].role == "orchestrator"
    assert panes["%2"].role == "lane" and panes["%2"].lane == "lane-a"
    assert panes["%2"].workmux_status == "done"
    assert panes["%3"].harness.name == "claude"
    assert panes["%1"].project == m.project == panes["%2"].project


def test_owner_typing_reads_as_typing(machine):
    machine(screens(p1="omp_owner_typing", p3="claude_typing"))
    r = read_all()
    assert r["%1"][1].state == "typing"
    assert r["%3"][1].state == "typing"


def test_self_resuming_claude_reads_as_resuming(machine):
    machine(screens(p3="claude_resuming"))
    assert read_all()["%3"][1].state == "resuming"


def test_finished_lane_is_marked_done(machine):
    m = machine(screens(p2="omp_stalled_todo"))
    assert not {p.id: p for p in scan().agents}["%2"].lane_done
    Path(m.lane, ".workmux", "HANDOFF.md").write_text("done")
    assert {p.id: p for p in scan().agents}["%2"].lane_done
