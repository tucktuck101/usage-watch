import re
import stat

import pytest

from usage_watch import identity
from usage_watch.errors import Problem

SECRET_A = b"a" * 32
SECRET_B = b"b" * 32


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state" / "usage-watch"


def test_secret_is_created_0600_and_reused(state_home):
    first = identity.install_secret()
    path = state_home / "install_secret"
    assert len(first) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert identity.install_secret() == first
    assert [p.name for p in state_home.iterdir()] == ["install_secret"]


def test_explicit_state_dir(tmp_path):
    secret = identity.install_secret(tmp_path / "elsewhere")
    assert (tmp_path / "elsewhere" / "install_secret").read_bytes() == secret


def test_wrong_size_secret_is_a_prompt(state_home):
    state_home.mkdir(parents=True)
    (state_home / "install_secret").write_bytes(b"short")
    with pytest.raises(Problem) as exc:
        identity.install_secret()
    text = exc.value.render()
    assert "5 bytes" in text and "32 bytes" in text
    assert "restore" in text and "backup" in text and "splits history" in text


def test_hash_is_stable_and_separated():
    h = identity.keyed_hash("stream", "trace-1", SECRET_A)
    assert h == identity.keyed_hash("stream", b"trace-1", SECRET_A)
    assert h != identity.keyed_hash("stream", "trace-1", SECRET_B)
    assert h != identity.keyed_hash("checkout", "trace-1", SECRET_A)
    assert identity.account("anthropic", "x", SECRET_A) != identity.account("openai", "x", SECRET_A)
    assert identity.account("anthropic", "x", SECRET_A) != identity.request("anthropic", "x", SECRET_A)
    assert re.fullmatch(r"[0-9a-f]{16}", h)


def test_helpers_use_the_d5_namespaces():
    s = SECRET_A
    assert identity.account("claude", "id", s) == identity.keyed_hash("account:claude", "id", s)
    assert identity.request("claude", "id", s) == identity.keyed_hash("request:claude", "id", s)
    assert identity.checkout("/r/.git", s) == identity.keyed_hash("checkout", "/r/.git", s)
    assert identity.stream("/p.jsonl", s) == identity.keyed_hash("stream", "/p.jsonl", s)
    assert identity.repository("https://h/o/r", s) == identity.keyed_hash("repository", "h/o/r", s)


def test_default_secret_is_the_install_secret():
    assert identity.stream("x") == identity.stream("x", identity.install_secret())


@pytest.mark.parametrize("namespace, raw", [("", "x"), ("stream", ""), ("stream", b""), ("stream", None)])
def test_empty_input_is_refused(namespace, raw):
    with pytest.raises(ValueError):
        identity.keyed_hash(namespace, raw, SECRET_A)


@pytest.mark.parametrize("provider", ["", "a:b"])
def test_bad_provider_is_refused(provider):
    with pytest.raises(ValueError):
        identity.account(provider, "x", SECRET_A)


def test_remote_normalisation():
    remotes = [
        "git@github.com:Org/Repo.git",
        "https://user:tok@github.com/org/repo",
        "ssh://git@github.com/org/repo.git",
        "https://GitHub.com/org/repo.git/",
    ]
    assert {identity.normalise_remote(r) for r in remotes} == {"github.com/org/repo"}
    assert len({identity.repository(r, SECRET_A) for r in remotes}) == 1
    assert identity.repository("git@github.com:org/other", SECRET_A) != identity.repository(remotes[0], SECRET_A)


@pytest.mark.parametrize("remote", ["", "  ", "/srv/repo.git", "file:///srv/repo.git", "https://github.com/"])
def test_remote_without_host_or_path_is_refused(remote):
    with pytest.raises(ValueError):
        identity.normalise_remote(remote)


SENSITIVE = "SeCrEt-Token-9f3a"


@pytest.mark.parametrize(
    "call",
    [
        lambda: identity.keyed_hash("stream", 12345, SECRET_A),
        lambda: identity.keyed_hash("stream", SENSITIVE, b""),
        lambda: identity.account("bad:" + SENSITIVE, SENSITIVE, SECRET_A),
        lambda: identity.repository(f"/home/{SENSITIVE}/repo", SECRET_A),
        lambda: identity.repository(f"https://{SENSITIVE}@/", SECRET_A),
        lambda: identity.repository(f"https://u:{SENSITIVE}@[bad/x", SECRET_A),
    ],
)
def test_exception_text_never_contains_the_raw_value(call):
    with pytest.raises((ValueError, TypeError)) as exc:
        call()
    text = str(exc.value) + repr(exc.value) + str(exc.value.__cause__ or "") + str(exc.value.__context__ or "")
    assert SENSITIVE.lower() not in text.lower()
    assert "12345" not in text


def test_problem_never_contains_the_secret(state_home):
    state_home.mkdir(parents=True)
    (state_home / "install_secret").write_bytes(SENSITIVE.encode())
    with pytest.raises(Problem) as exc:
        identity.install_secret()
    assert SENSITIVE not in exc.value.render() and SENSITIVE not in str(exc.value)
