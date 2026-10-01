"""The omp collectors: `omp.session` (tail) and `omp.usage_cache` (snapshot).

Fixtures under tests/fixtures/omp/ are synthetic, shaped like real omp 18.x
records (key names and types checked against a live install; no content
copied). `@ROOT@` in a fixture stands for the sessions root, since omp's
`parentSession` is an absolute file path."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from usage_watch import identity
from usage_watch.collectors import omp
from usage_watch.model import AttributionEvidence, CapacitySample, Session, UsageObservation
from usage_watch.runtime.core import Runtime, account_alias_value

FIXTURES = Path(__file__).parent / "fixtures" / "omp"
SECRET = b"s" * 32
PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
PARENT_FILE = f"-proj-demo/2026-10-01T00-00-00-000Z_{PARENT}.jsonl"
CHILD_FILE = f"-proj-demo/2026-10-01T00-00-00-000Z_{PARENT}/0-Task.jsonl"
LATE = 1_790_900_000_000 / 1000  # a clock well after the fixture's records
ANTHROPIC_KEY = "anthropic:synthetic-identity-1"
CODEX_KEY = "openai-codex:synthetic-identity-2"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_db(path: Path, report: dict | None = None) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY, provider TEXT, credential_type TEXT,"
        " data TEXT, disabled_cause TEXT, identity_key TEXT, created_at INT, updated_at INT);"
        "CREATE TABLE cache (key TEXT PRIMARY KEY, value TEXT, expires_at INT);")
    conn.executemany(
        "INSERT INTO auth_credentials (id, provider, credential_type, data, identity_key)"
        " VALUES (?, ?, 'oauth', 'SECRET-NEVER-READ', ?)",
        [(1, "anthropic", ANTHROPIC_KEY), (2, "openai-codex", CODEX_KEY), (3, "openrouter", None)])
    if report is None:
        report = json.loads((FIXTURES / "usage_cache_report.json").read_text())
    conn.execute("INSERT INTO cache VALUES (?, ?, 0)",
                 ("usage_cache:report:anthropic:synthetic", json.dumps(report)))
    conn.execute("INSERT INTO cache VALUES ('usage_cache:other', '{}', 0)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def root(tmp_path):
    dest = tmp_path / "sessions"
    shutil.copytree(FIXTURES / "sessions", dest)
    for p in dest.glob("**/*.jsonl"):
        p.write_text(p.read_text().replace("@ROOT@", str(dest)))
    return dest


@pytest.fixture
def db(tmp_path):
    return make_db(tmp_path / "agent.db")


def split(items):
    out = {Session: [], UsageObservation: [], AttributionEvidence: [], CapacitySample: []}
    for i in items:
        out.setdefault(type(i), []).append(i)
    return out


def session_source(root, db, t=LATE):
    return omp.OmpSessionSource(root=root, db=db, interval_s=0, clock=Clock(t), secret=SECRET)


# --- omp.session -------------------------------------------------------------------


def test_session_source_contract(root, db):
    s = session_source(root, db)
    assert (s.name, s.primary, s.merge) == ("omp.session", True, None)
    assert omp.OmpSessionSource().interval_s == 30


def test_sessions_from_headers(root, db):
    items, _ = session_source(root, db).collect(None)
    sessions = {s.session_key: s for s in split(items)[Session]}
    parent, child = sessions[f"omp:{PARENT}"], sessions[f"omp:{CHILD}"]
    assert parent.parent_session_key is None
    assert child.parent_session_key == f"omp:{PARENT}"  # from the parent file's header
    assert parent.cwd == "/tmp/proj-demo"
    assert parent.started_at == 1_790_812_800_000
    assert parent.last_seen_at == 1_790_812_815_000
    assert not parent.live
    # Sessions lead the batch, before anything that names them.
    assert isinstance(items[0], Session) and isinstance(items[1], Session)


def test_observations_map_d4_fields(root, db):
    all_obs = split(session_source(root, db).collect(None)[0])[UsageObservation]
    obs = {o.source_request_key: o for o in all_obs if not o.auxiliary}
    assert set(obs) == {"a0000004", "a0000008", "b0000002"}  # not user or toolResult
    # omp's own side calls (model_usage) count too, marked auxiliary (A10)
    assert {o.source_request_key for o in all_obs if o.auxiliary} and all(
        o.source_request_key.startswith("aux:") for o in all_obs if o.auxiliary)
    a = obs["a0000004"]
    assert (a.source, a.stream_key, a.session_key) == ("omp.session", f"omp:{PARENT}", f"omp:{PARENT}")
    assert (a.harness, a.provider, a.model) == ("omp", "anthropic", "claude-opus-5-5")
    assert (a.uncached_input_tokens, a.cache_read_input_tokens, a.cache_write_input_tokens,
            a.output_tokens, a.reasoning_output_tokens) == (10, 3000, 400, 200, None)
    assert a.observed_at == 1_790_812_802_000
    assert a.provider_request_key is None and a.auxiliary is False
    assert a.confidence == "authoritative"
    native = json.loads(a.native)
    assert native["totalTokens"] == 3610 and native["cost.total"] == 0.0091
    assert "cttl" not in native and all(isinstance(v, (int, float)) for v in native.values())
    b = obs["a0000008"]
    assert b.provider == "openai" and b.reasoning_output_tokens == 40
    assert obs["b0000002"].provider == "openrouter"
    assert obs["b0000002"].stream_key == f"omp:{CHILD}"


def test_no_content_reaches_an_item(root, db):
    items, wm = session_source(root, db).collect(None)
    text = repr(items) + wm
    for secret_text in ("synthetic prompt", "synthetic reply", "synthetic tool output",
                        "systemPrompt", ANTHROPIC_KEY, "SECRET-NEVER-READ", "msg_synthetic_1"):
        assert secret_text not in text


def test_account_evidence_per_credential(root, db):
    ev = split(session_source(root, db).collect(None)[0])[AttributionEvidence]
    expected = {
        account_alias_value("anthropic", "omp.identity_key", identity.account("anthropic", ANTHROPIC_KEY, SECRET)),
        account_alias_value("openai", "omp.identity_key", identity.account("openai", CODEX_KEY, SECRET)),
    }
    assert {e.value for e in ev} == expected
    for e in ev:
        assert (e.subject_kind, e.subject_id, e.dimension) == ("session", f"omp:{PARENT}", "account")
        assert (e.method, e.source, e.confidence, e.validity) == (
            "credential_id", "omp.session", "authoritative", "historical")
        # valid from the request that switched to this login (A19)
        assert e.valid_from == e.first_observed_at > 0


def test_only_named_columns_are_selected(root, db, monkeypatch):
    statements = []
    real = omp._connect_ro

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(omp, "_connect_ro", traced)
    session_source(root, db).collect(None)
    omp.OmpUsageCacheSource(db=db, secret=SECRET).collect(None)
    assert statements == [
        "SELECT id, provider, identity_key FROM auth_credentials",
        "SELECT key, value FROM cache WHERE key GLOB 'usage_cache:report:*'",
    ]


def test_database_is_opened_read_only(db):
    conn = omp._connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM cache")
    conn.close()


def test_a_second_pass_reads_nothing_new(root, db):
    s = session_source(root, db)
    _, wm = s.collect(None)
    items, wm2 = s.collect(wm)
    assert items == [] and json.loads(wm2)["files"] == json.loads(wm)["files"]


def append(path: Path, text: str):
    with open(path, "a") as fh:
        fh.write(text)


def assistant(entry_id, t, out=5, cred=None):
    msg = {"role": "assistant", "content": [], "provider": "anthropic", "model": "m",
           "usage": {"input": 1, "output": out, "cacheRead": 0, "cacheWrite": 0,
                     "totalTokens": 1 + out, "cost": {"total": 0}}, "timestamp": t}
    if cred is not None:
        msg["credentialId"] = cred
    return json.dumps({"type": "message", "id": entry_id, "parentId": "x",
                       "timestamp": "2026-10-01T01:00:00.000Z", "message": msg}) + "\n"


def test_tail_reads_only_new_complete_lines(root, db):
    s = session_source(root, db)
    _, wm = s.collect(None)
    path = root / PARENT_FILE
    line = assistant("c0000001", 1_790_816_400_000)
    append(path, line[:40])  # an incomplete final line
    items, wm = s.collect(wm)
    assert split(items)[UsageObservation] == []
    offset = json.loads(wm)["files"][str(path)][1]
    assert offset == path.stat().st_size - 40
    append(path, line[40:])
    items, wm = s.collect(wm)
    new = split(items)
    assert [o.source_request_key for o in new[UsageObservation]] == ["c0000001"]
    assert new[UsageObservation][0].stream_key == f"omp:{PARENT}"  # header state carried over
    assert new[AttributionEvidence] == []  # credentials already reported for this session
    assert new[Session][0].last_seen_at == 1_790_816_400_000


def test_a_shorter_file_or_new_inode_restarts(root, db):
    s = session_source(root, db)
    _, wm = s.collect(None)
    path = root / CHILD_FILE
    lines = path.read_text().splitlines(keepends=True)
    path.unlink()
    path.write_text("".join(lines[:2]) + assistant("d0000001", 1_790_812_900_000))
    items, _ = s.collect(wm)
    obs = split(items)[UsageObservation]
    assert [o.source_request_key for o in obs] == ["d0000001"]
    assert obs[0].stream_key == f"omp:{CHILD}"


def test_live_within_ten_minutes_then_cleared(root, db):
    last = 1_790_812_815_000
    s = session_source(root, db, t=(last + 60_000) / 1000)
    items, wm = s.collect(None)
    live = {x.session_key: x.live for x in split(items)[Session]}
    assert live == {f"omp:{PARENT}": True, f"omp:{CHILD}": True}
    s.clock = Clock((last + 11 * 60_000) / 1000)
    items, _ = s.collect(wm)
    live = {x.session_key: x.live for x in split(items)[Session]}
    assert live == {f"omp:{PARENT}": False, f"omp:{CHILD}": False}


def test_a_new_credential_later_adds_evidence(root, db):
    s = session_source(root, db)
    _, wm = s.collect(None)
    append(root / CHILD_FILE, assistant("e0000001", 1_790_816_400_000, cred=1))
    ev = split(s.collect(wm)[0])[AttributionEvidence]
    assert [(e.subject_id, e.first_observed_at) for e in ev] == [(f"omp:{CHILD}", 1_790_816_400_000)]


def test_missing_root_or_db(tmp_path):
    s = omp.OmpSessionSource(root=tmp_path / "none", db=tmp_path / "none.db", secret=SECRET)
    assert s.collect(None)[0] == []
    c = omp.OmpUsageCacheSource(db=tmp_path / "none.db", secret=SECRET)
    assert c.collect(None) == ([], None)


# --- omp.usage_cache ---------------------------------------------------------------


def test_usage_cache_contract():
    c = omp.OmpUsageCacheSource()
    assert (c.name, c.primary, c.merge, c.interval_s) == ("omp.usage_cache", False, None, 60)


def test_capacity_samples_per_limit(db):
    items, _ = omp.OmpUsageCacheSource(db=db, secret=SECRET).collect(None)
    samples = {s.window: s for s in split(items)[CapacitySample]}
    assert set(samples) == {"session", "weekly", "weekly:fable"}
    stream = identity.stream("usage_cache:report:anthropic:synthetic", SECRET)
    s = samples["session"]
    assert (s.source, s.stream_key, s.confidence, s.observed_at) == (
        "omp.usage_cache", stream, "observed", 1_790_812_900_000)
    assert (s.used_pct, s.window_seconds, s.resets_at, s.status) == (42.0, 18000, 1_790_820_000_000, "ok")
    assert samples["weekly"].status == "warning" and samples["weekly"].window_seconds == 604800
    assert samples["weekly:fable"].used_pct == 100.0 and samples["weekly:fable"].status == "exhausted"


def test_capacity_account_evidence(db):
    items, _ = omp.OmpUsageCacheSource(db=db, secret=SECRET).collect(None)
    ev = split(items)[AttributionEvidence]
    stream = identity.stream("usage_cache:report:anthropic:synthetic", SECRET)
    raw = "aaaaaaaa-0000-4000-8000-000000000001|bbbbbbbb-0000-4000-8000-000000000002"
    value = account_alias_value("anthropic", "omp.report_account", identity.account("anthropic", raw, SECRET))
    assert {e.subject_id for e in ev} == {
        f"omp.usage_cache|{stream}|{w}|1790812900000" for w in ("session", "weekly", "weekly:fable")}
    for e in ev:
        assert (e.subject_kind, e.dimension, e.value, e.method, e.confidence, e.validity) == (
            "capacity_sample", "account", value, "usage_cache", "observed", "historical")
    assert "someone@example.invalid" not in repr(items)


def test_usage_cache_emits_only_new_readings(tmp_path):
    report = json.loads((FIXTURES / "usage_cache_report.json").read_text())
    path = make_db(tmp_path / "agent.db", report)
    c = omp.OmpUsageCacheSource(db=path, secret=SECRET)
    items, wm = c.collect(None)
    assert json.loads(wm) == {"usage_cache:report:anthropic:synthetic": 1_790_812_900_000}
    assert c.collect(wm)[0] == []
    report["value"]["fetchedAt"] += 60_000
    conn = sqlite3.connect(path)
    conn.execute("UPDATE cache SET value = ? WHERE key = 'usage_cache:report:anthropic:synthetic'",
                 (json.dumps(report),))
    conn.commit()
    conn.close()
    items, _ = c.collect(wm)
    assert {s.observed_at for s in split(items)[CapacitySample]} == {1_790_812_960_000}


def test_unknown_window_and_status():
    name, seconds = omp._window({"window": {"id": "1d", "durationMs": 86_400_000}})
    assert (name, seconds) == ("other:1d", 86400)
    assert omp._window({}) == ("other:unstated", None)


# --- Through the real runtime --------------------------------------------------------


def test_through_the_runtime_into_the_store(root, db, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store_db = tmp_path / "usage.db"
    sources = [omp.OmpSessionSource(root=root, db=db, interval_s=0, clock=Clock(LATE)),
               omp.OmpUsageCacheSource(db=db, interval_s=0)]
    rt = Runtime(sources, db_path=store_db, lock_path=tmp_path / "run.lock", clock=Clock(LATE))
    rt.start()
    try:
        rt.run_once()
        rt.run_once()  # a second pass adds nothing
    finally:
        rt.stop()
    conn = sqlite3.connect(store_db)
    try:
        errors = conn.execute("select collector, last_error from collector_status").fetchall()
        assert all(e is None for _, e in errors), errors
        assert conn.execute("select count(*), sum(output_tokens) from usage_events").fetchone() == (4, 301)
        assert conn.execute("select count(*) from usage_events where auxiliary = 1").fetchone() == (1,)
        assert conn.execute("select count(*) from sessions").fetchone() == (2,)
        assert conn.execute("select parent_session_key from sessions where session_id = ?",
                            (CHILD,)).fetchone() == (f"omp:{PARENT}",)
        assert conn.execute("select count(*) from capacity_samples").fetchone() == (3,)
        states = dict(conn.execute(
            "select subject_kind || '/' || subject_id, state from effective_attributions"
            " where dimension = 'account'").fetchall())
        # Two credentials on one session: ambiguous, by design (D2).
        assert states.pop(f"session/omp:{PARENT}") == "ambiguous"
        assert set(states.values()) == {"attributed"} and len(states) == 3
        # 3 reported aliases, plus the identity key the usage report co-reports (AliasLink)
        assert conn.execute("select count(*) from account_aliases").fetchone() == (4,)
    finally:
        conn.close()


def test_side_calls_are_auxiliary_observations(tmp_path):
    import json as _json
    from usage_watch.collectors.omp import OmpSessionSource
    root = tmp_path / "sessions" / "p"
    root.mkdir(parents=True)
    f = root / "s.jsonl"
    lines = [
        {"type": "title", "title": "x"},
        {"type": "session", "id": "sid-1", "version": 1, "timestamp": "2026-10-01T00:00:00Z", "cwd": "/w"},
        {"type": "model_usage", "id": "a1", "parentId": "p", "provider": "openrouter", "model": "m",
         "purpose": "judge", "role": "assistant", "stopReason": "stop", "api": "x",
         "timestamp": "2026-10-01T00:00:05Z",
         "usage": {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 12}},
    ]
    f.write_text("\n".join(_json.dumps(l) for l in lines) + "\n")
    src = OmpSessionSource(root=tmp_path / "sessions", db=tmp_path / "none.db", secret=b"s" * 32)
    items, _ = src.collect(None)
    aux = [i for i in items if getattr(i, "auxiliary", False)]
    assert len(aux) == 1
    assert aux[0].source_request_key == "aux:a1" and aux[0].session_key == "omp:sid-1"
    assert aux[0].uncached_input_tokens == 10 and aux[0].output_tokens == 2
