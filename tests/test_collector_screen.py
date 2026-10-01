"""The `screen` collector (F3): state samples, stall occurrences, watermarks."""

import datetime as dt
import json
import sqlite3

import pytest

from usage_watch import identity, screen, sh, topology
from usage_watch.adapters import Reading, by_name
from usage_watch.collectors import screen as collector
from usage_watch.collectors.screen import ScreenSource
from usage_watch.errors import Problem
from usage_watch.model import AgentStateSample, LimitEvent
from usage_watch.runtime.core import Runtime

from fakes import FIXTURES, Machine

SECRET = b"s" * 32
T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s=5):
        self.t += s


def pane(pid_: str, pid: int, harness: str = "omp") -> topology.Pane:
    return topology.Pane(pid_, "dev", "0", "main", "0", pid, "/tmp", "t", harness=by_name(harness))


class Screens:
    """Patches topology.scan and screen.capture with a scripted machine."""

    def __init__(self, monkeypatch):
        self.panes: list[topology.Pane] = []
        self.shows: dict[str, str] = {}   # pane id -> fixture name, or None to fail capture
        monkeypatch.setattr(topology, "scan", lambda: topology.Topology(panes=list(self.panes)))
        monkeypatch.setattr(screen, "capture", self.capture)

    def capture(self, pane_id):
        name = self.shows[pane_id]
        if name is None:
            raise Problem(f"cannot read pane {pane_id}", fix="none")
        return (FIXTURES / f"{name}.txt").read_text(), ""


@pytest.fixture
def machine(monkeypatch):
    return Screens(monkeypatch)


@pytest.fixture
def source():
    clock = Clock()
    s = ScreenSource(clock=clock, secret=SECRET)
    s.clock_ = clock
    return s


def split(items):
    return ([i for i in items if isinstance(i, AgentStateSample)],
            [i for i in items if isinstance(i, LimitEvent)])


def expected_stall_id(pane_id, pid, error_key, onset):
    raw = f"{pane_id}:{pid}"
    return identity.stream(f"{raw}|{error_key}|{onset}", SECRET)


def test_satisfies_the_pull_contract():
    s = ScreenSource()
    assert (s.name, s.primary, s.merge, s.interval_s) == ("screen", False, None, 5)
    assert ScreenSource(interval_s=300).interval_s == 300
    assert callable(s.collect)


def test_one_state_sample_per_pane(machine, source):
    machine.panes = [pane("%1", 101, "omp"), pane("%2", 102, "claude"), pane("%3", 103, "codex")]
    machine.shows = {"%1": "omp_busy", "%2": "claude_idle", "%3": "codex_idle"}
    samples, events = split(source.collect(None)[0])
    assert events == []
    by = {s.pane: s for s in samples}
    assert set(by) == {"%1", "%2", "%3"}
    assert by["%1"].state == "busy" and by["%1"].harness == "omp"
    assert by["%2"].state == "idle" and by["%2"].harness == "claude"
    assert by["%3"].state == "idle" and by["%3"].harness == "codex"
    for s in samples:
        assert s.source == "screen" and s.confidence == "inferred"
        assert s.session_key is None
        assert s.observed_at == int(T0 * 1000)


def test_first_stall_emits_one_limit_event_and_a_repeat_emits_none(machine, source):
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_stalled_plain"}
    items, wm = source.collect(None)
    samples, events = split(items)
    assert samples[0].state == "stalled" and samples[0].error_key
    key, onset = samples[0].error_key, int(T0 * 1000)
    assert len(events) == 1
    ev = events[0]
    assert (ev.kind, ev.source, ev.confidence, ev.observed_at) == ("hit", "screen", "inferred", onset)
    assert ev.stream_key == identity.stream("%1:101", SECRET)
    assert ev.source_key == expected_stall_id("%1", 101, key, onset)
    assert json.loads(wm) == {"%1": {"error_key": key, "onset": onset, "pane_pid": 101}}

    source.clock_.tick()
    items, wm2 = source.collect(wm)
    samples, events = split(items)
    assert len(samples) == 1 and samples[0].state == "stalled"
    assert events == []
    assert json.loads(wm2)["%1"]["onset"] == onset


def test_a_stall_that_clears_and_recurs_gets_a_new_stall_id(machine, source):
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_stalled_plain"}
    items, wm = source.collect(None)
    first = split(items)[1][0]
    source.clock_.tick()
    machine.shows["%1"] = "omp_busy"
    items, wm = source.collect(wm)
    assert split(items)[1] == [] and json.loads(wm) == {}
    source.clock_.tick()
    machine.shows["%1"] = "omp_stalled_plain"
    items, wm = source.collect(wm)
    samples, events = split(items)
    assert len(events) == 1
    assert samples[0].error_key == json.loads(wm)["%1"]["error_key"]
    assert events[0].source_key != first.source_key
    assert events[0].stream_key == first.stream_key
    assert events[0].observed_at == int((T0 + 10) * 1000)


def test_onset_survives_a_restart_through_the_watermark(machine):
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_stalled_plain"}
    items, wm = ScreenSource(clock=Clock(T0), secret=SECRET).collect(None)
    onset = split(items)[1][0].observed_at
    # a new process, later, reading the stored watermark
    items, wm2 = ScreenSource(clock=Clock(T0 + 600), secret=SECRET).collect(wm)
    assert split(items)[1] == []
    assert json.loads(wm2)["%1"]["onset"] == onset


def test_a_pid_change_means_a_new_pane(machine, source):
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_stalled_plain"}
    items, wm = source.collect(None)
    first = split(items)[1][0]
    source.clock_.tick()
    machine.panes = [pane("%1", 999)]          # same pane id, a new shell
    items, wm = source.collect(wm)
    events = split(items)[1]
    assert len(events) == 1
    assert events[0].stream_key == identity.stream("%1:999", SECRET) != first.stream_key
    assert json.loads(wm)["%1"]["pane_pid"] == 999


def test_a_gone_pane_leaves_the_watermark(machine, source):
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_stalled_plain"}
    _, wm = source.collect(None)
    machine.panes = []
    items, wm = source.collect(wm)
    assert items == [] and json.loads(wm) == {}


def test_a_capture_failure_is_skipped(machine, source):
    machine.panes = [pane("%1", 101), pane("%2", 102)]
    machine.shows = {"%1": "omp_stalled_plain", "%2": "omp_busy"}
    _, wm = source.collect(None)
    machine.shows["%1"] = None
    source.clock_.tick()
    items, wm2 = source.collect(wm)
    samples, events = split(items)
    assert [s.pane for s in samples] == ["%2"] and events == []
    assert json.loads(wm2) == json.loads(wm)    # an unread pane keeps its stall
    machine.shows["%1"] = "omp_stalled_plain"
    assert split(source.collect(wm2)[0])[1] == []   # still the same occurrence


def test_a_new_error_key_while_stalled_is_a_new_occurrence(machine, source, monkeypatch):
    reads = iter([Reading("stalled", error_key="a"), Reading("stalled", error_key="b"),
                  Reading("unknown", note="no omp composer on screen"),
                  Reading("stalled", error_key="b")])
    adapter = by_name("omp")
    monkeypatch.setattr(type(adapter), "read", lambda self, plain, styled="": next(reads))
    machine.panes = [pane("%1", 101)]
    machine.shows = {"%1": "omp_idle"}
    counts = []
    wm = None
    for _ in range(4):
        items, wm = source.collect(wm)
        counts.append(len(split(items)[1]))
        source.clock_.tick()
    assert counts == [1, 1, 0, 0]    # `unknown` neither ends nor starts a stall


def test_reset_hint_is_utc_ms(machine, source, monkeypatch):
    when = dt.datetime(2026, 10, 1, 22, 29, tzinfo=dt.timezone(dt.timedelta(hours=10)))
    monkeypatch.setattr(type(by_name("claude")), "read",
                        lambda self, plain, styled="": Reading("stalled", "Opus 5.5", "k", when))
    machine.panes = [pane("%1", 101, "claude")]
    machine.shows = {"%1": "claude_stalled"}
    samples, events = split(source.collect(None)[0])
    ms = int(when.timestamp() * 1000)
    assert samples[0].reset_hint == ms and events[0].resets_at == ms
    assert samples[0].model == "Opus 5.5"


def fixture_lines():
    for path in sorted(FIXTURES.glob("*.txt")):
        for line in path.read_text().splitlines():
            if len(line.strip()) >= 20:   # shorter lines are words, not text
                yield path.stem, line.strip()


@pytest.mark.parametrize("fixture", sorted(p.stem for p in FIXTURES.glob("*.txt")))
def test_note_never_holds_screen_text(machine, source, fixture):
    harness = fixture.split("_")[0]
    machine.panes = [pane("%1", 101, harness)]
    machine.shows = {"%1": fixture}
    items, wm = source.collect(None)
    text = (FIXTURES / f"{fixture}.txt").read_text()
    for s in split(items)[0]:
        if s.note:
            assert s.note not in text
            for _, line in fixture_lines():
                assert line not in s.note
    for _, line in fixture_lines():
        assert line not in wm


# --- Through the real runtime into a store -------------------------------------------------

def test_through_the_runtime_into_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    m = Machine(tmp_path, {"%1": "omp_stalled_plain", "%2": "omp_busy", "%3": "claude_idle"})
    monkeypatch.setattr(sh, "run", m)
    monkeypatch.setattr(sh, "which", lambda name: f"/usr/bin/{name}")
    db = tmp_path / "usage.db"
    clock = Clock()
    rt = Runtime([ScreenSource(interval_s=0, clock=clock)], db_path=db,
                 lock_path=tmp_path / "run.lock", clock=clock)
    rt.start()
    try:
        rt.run_once()
        clock.tick()
        rt.run_once()                  # the same stall again
        m.screens["%1"] = "omp_busy"
        clock.tick()
        rt.run_once()                  # it clears
        m.screens["%1"] = "omp_stalled_plain"
        clock.tick()
        rt.run_once()                  # and recurs
    finally:
        rt.stop()

    conn = sqlite3.connect(db)
    try:
        samples = conn.execute(
            "select pane, observed_at, harness, state, error_key, note, source, confidence,"
            " session_key from state_samples order by observed_at, pane").fetchall()
        events = conn.execute(
            "select source, stream_key, source_key, kind, confidence, observed_at, resets_at"
            " from limit_events order by observed_at").fetchall()
        status = conn.execute(
            "select last_error from collector_status where collector = 'screen'").fetchone()
        wm = conn.execute("select path from watermarks where collector = 'screen'").fetchone()[0]
    finally:
        conn.close()

    assert status == (None,)
    assert len(samples) == 12                       # 3 agent panes x 4 passes
    assert {s[6] for s in samples} == {"screen"} and {s[7] for s in samples} == {"inferred"}
    assert {s[8] for s in samples} == {None}
    states = [(s[0], s[3]) for s in samples if s[0] == "%1"]
    assert [st for _, st in states] == ["stalled", "stalled", "busy", "stalled"]

    t = int(T0 * 1000)
    assert len(events) == 2
    secret = identity.install_secret()
    stream = identity.stream("%1:101", secret)
    key = next(s[4] for s in samples if s[0] == "%1" and s[3] == "stalled")
    assert events[0][:6] == ("screen", stream, collector.stall_id("%1:101", key, t, secret),
                             "hit", "inferred", t)
    assert events[1][1] == stream and events[1][5] == t + 15_000
    assert events[1][2] != events[0][2]
    assert json.loads(wm)["%1"]["onset"] == t + 15_000

    stored = "\n".join(str(v) for row in samples + events for v in row) + wm
    for _, line in fixture_lines():
        assert line not in stored


def test_omp_retry_after_is_pinned_to_onset():
    from usage_watch.adapters.omp import Omp
    from pathlib import Path
    text = (Path(__file__).parent / "fixtures" / "omp_stalled_plain.txt").read_text()
    reading = Omp().read(text)
    assert reading.state == "stalled" and reading.retry_after_ms == 4360000
