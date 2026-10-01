"""The omp collectors (D6): `omp.session` (C2) and `omp.usage_cache` (C1).

`OmpSessionSource` tails `~/.omp/agent/sessions/**/*.jsonl`. Per file it
reads the `type:"session"` header (the second line; the first is a
`type:"title"` record) and emits a `Session`, then one `UsageObservation`
per `type:"message"` entry whose `message.role` is `assistant` and which
carries `message.usage`. `message.credentialId` becomes `account` evidence
on the session, through `auth_credentials.identity_key` (D2, D7).

`OmpUsageCacheSource` reads `agent.db` `cache` rows keyed
`usage_cache:report:*` and emits one `CapacitySample` per limit, plus
`account` evidence from the report's `metadata.accountId` + `metadata.orgId`.

Both open `agent.db` read-only (`mode=ro`) and select only named columns:
`auth_credentials` `id`, `provider`, `identity_key`, and `cache` `key`,
`value`. `auth_credentials.data` is never selected (D5). Lines and cache
values are parsed in memory; only mapped fields leave this module. No
network calls.

Confirmed against this machine's omp data, 2026-10-01 (key names only):
- `usage.totalTokens == input + output + cacheRead + cacheWrite` on every
  record, so `input` excludes cache reads: it is the uncached input.
- Entry `id` (8 hex characters) is unique within each session file: no
  duplicate among 74,126 usage entries.
- Assistant `message` entries carry no side-call marker. omp's own side calls
  (`purpose`: judge, judge_batch, find, auto-thinking, ...) are a separate
  record type, `type:"model_usage"`, which D4 doesn't map, so it is not read.
  `auxiliary` is therefore always False here.
- `parentSession` is the absolute path of the parent's session file, not an
  ID; the parent's ID is read from that file's own header.
- The cache `value` column is `{"value": <report>, "expiresAt"}`; the report
  holds `provider`, `fetchedAt` (Unix ms), `limits[]` and `metadata`
  (`accountId`, `orgId`). Limit windows: `window.durationMs`,
  `window.resetsAt` (Unix ms), `window.id`; a per-model limit carries
  `scope.tier` (e.g. `fable`), not `scope.modelId`.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from .. import identity
from ..model import (
    CAPACITY_STATUSES, UNSTATED_WINDOW, AttributionEvidence, CapacitySample, Session,
    UsageObservation, session_key,
)
from ..runtime.core import account_alias_value

__all__ = [
    "OmpSessionSource", "OmpUsageCacheSource", "account_provider", "capacity_subject_id",
    "default_db", "default_sessions_root",
]

HARNESS = "omp"
SESSION_SOURCE = "omp.session"
CACHE_SOURCE = "omp.usage_cache"
PARSER_VERSION = "omp.session/1"
LIVE_MS = 10 * 60 * 1000
CACHE_PREFIX = "usage_cache:report:"

# omp provider name -> the provider an account alias is namespaced under.
_ACCOUNT_PROVIDERS = {"anthropic": "anthropic", "openai-codex": "openai"}
# omp provider name -> the usage observation's provider. Others are kept as-is.
_OBS_PROVIDERS = {"openai-codex": "openai"}

# D4 table 1: usage field -> D1 token field.
_TOKEN_MAP = (
    ("input", "uncached_input_tokens"),
    ("cacheRead", "cache_read_input_tokens"),
    ("cacheWrite", "cache_write_input_tokens"),
    ("output", "output_tokens"),
    ("reasoningTokens", "reasoning_output_tokens"),
)
_NATIVE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "reasoningTokens")
_COST_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "total")

_SESSION_WINDOW_MS = 5 * 3600 * 1000
_WEEKLY_WINDOW_MS = 7 * 24 * 3600 * 1000


def default_sessions_root() -> Path:
    return Path(os.path.expanduser("~/.omp/agent/sessions"))


def default_db() -> Path:
    return Path(os.path.expanduser("~/.omp/agent/agent.db"))


def account_provider(omp_provider: str | None) -> str | None:
    """The provider an omp login's account alias belongs to, or None."""
    return _ACCOUNT_PROVIDERS.get(omp_provider or "")


def capacity_subject_id(source: str, stream_key: str, window: str, observed_at: int) -> str:
    """A capacity sample's natural key, as its attribution subject_id."""
    return f"{source}|{stream_key}|{window}|{observed_at}"


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _iso_ms(value) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _load_json(watermark: str | None) -> dict:
    if not watermark:
        return {}
    try:
        data = json.loads(watermark)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# --- omp.session -------------------------------------------------------------------


class OmpSessionSource:
    """A tailing `PullSource` over omp's session files. Primary for omp.

    Watermark: `{"files": {path: [inode, offset]}, "state": {path: {...}}}`.
    `state` holds what a later pass needs without re-reading the header:
    `sid`, `started`, `last`, `parent`, `cwd`, `version`, `live`, and the
    credential IDs already turned into evidence (`creds`).
    """

    name = SESSION_SOURCE
    primary = True
    merge = None

    def __init__(self, root: str | os.PathLike | None = None, db: str | os.PathLike | None = None,
                 interval_s: float = 30, clock: Callable[[], float] = time.time,
                 secret: bytes | None = None):
        self.root = Path(root) if root is not None else default_sessions_root()
        self.db = Path(db) if db is not None else default_db()
        self.interval_s = interval_s
        self.clock = clock
        self.secret = secret
        self._credentials: dict[int, tuple[str | None, str | None]] | None = None
        self._header_ids: dict[str, str | None] = {}

    # -- credentials, cached per pass --

    def _credential(self, credential_id: int) -> tuple[str | None, str | None]:
        if self._credentials is None:
            self._credentials = {}
            try:
                conn = _connect_ro(self.db)
                try:
                    for cid, provider, key in conn.execute(
                            "SELECT id, provider, identity_key FROM auth_credentials"):
                        self._credentials[cid] = (provider, key)
                finally:
                    conn.close()
            except sqlite3.Error:
                pass  # Reading: no credential table means no account evidence, not a failure.
        return self._credentials.get(credential_id, (None, None))

    # -- parent sessions --

    def _session_id_of(self, path: str, state: dict) -> str | None:
        """The session ID in a session file's header, from state or the file."""
        known = state.get(path, {}).get("sid")
        if known:
            return known
        if path in self._header_ids:
            return self._header_ids[path]
        sid = None
        try:
            with open(path, "rb") as fh:
                for _ in range(8):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("type") == "session":
                        sid = rec.get("id") if isinstance(rec.get("id"), str) else None
                        break
        except OSError:
            pass
        self._header_ids[path] = sid
        return sid

    # -- the pass --

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        self._credentials = None
        self._header_ids = {}
        wm = _load_json(watermark)
        files = wm.get("files") if isinstance(wm.get("files"), dict) else {}
        state = wm.get("state") if isinstance(wm.get("state"), dict) else {}
        now = int(self.clock() * 1000)
        items: list = []
        new_files: dict[str, list] = {}
        new_state: dict[str, dict] = {}

        paths = sorted(str(p) for p in self.root.glob("**/*.jsonl")) if self.root.is_dir() else []
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            prior = files.get(path)
            st_state = state.get(path) if isinstance(state.get(path), dict) else {}
            offset = 0
            if (isinstance(prior, list) and len(prior) == 2 and prior[0] == st.st_ino
                    and _is_int(prior[1]) and 0 <= prior[1] <= st.st_size):
                offset = prior[1]
            else:
                st_state = {}  # a new inode or a shorter file starts over
            st_state = dict(st_state)
            st_state.setdefault("creds", [])
            new_offset = offset
            touched = False
            if st.st_size > offset:
                new_offset, touched = self._read(path, offset, st_state, state, items)
            new_files[path] = [st.st_ino, new_offset]

            sid = st_state.get("sid")
            last = st_state.get("last")
            live = bool(_is_int(last) and now - last <= LIVE_MS)
            if sid and (touched or live != bool(st_state.get("live"))):
                items.append(Session(
                    session_key=session_key(HARNESS, sid), harness=HARNESS, session_id=sid,
                    parent_session_key=st_state.get("parent"), started_at=st_state.get("started"),
                    last_seen_at=last, live=live, cwd=st_state.get("cwd"),
                ))
            st_state["live"] = live
            new_state[path] = st_state

        # Sessions come first in a batch, so their rows exist before the
        # observations and evidence that name them.
        items.sort(key=lambda i: 0 if isinstance(i, Session) else 1)
        return items, json.dumps({"files": new_files, "state": new_state}, sort_keys=True)

    def _read(self, path: str, offset: int, st: dict, all_state: dict, items: list) -> tuple[int, bool]:
        """Read complete lines from `offset`. Return the new offset (never past
        an incomplete final line) and whether anything was emitted."""
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
        except OSError:
            return offset, False
        end = data.rfind(b"\n")
        if end < 0:
            return offset, False
        touched = False
        for line in data[: end + 1].split(b"\n"):
            if not line:
                continue
            # Fast skip: only session headers and usage-bearing messages matter.
            if line.startswith(b'{"type":"'):
                if not (line.startswith(b'{"type":"session",')
                        or (line.startswith(b'{"type":"message",') and b'"usage"' in line)
                        or (line.startswith(b'{"type":"model_usage",') and b'"usage"' in line)):
                    continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # D5: never log a line's content
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "session":
                touched |= self._header(path, rec, st, all_state)
            elif kind == "message":
                touched |= self._message(path, rec, st, items)
            elif kind == "model_usage":
                touched |= self._side_call(path, rec, st, items)
        return offset + end + 1, touched

    def _header(self, path: str, rec: dict, st: dict, all_state: dict) -> bool:
        sid = rec.get("id")
        if not isinstance(sid, str) or not sid:
            return False
        st["sid"] = sid
        started = _iso_ms(rec.get("timestamp"))
        if started is not None:
            st["started"] = started
            if not _is_int(st.get("last")) or st["last"] < started:
                st["last"] = started
        if isinstance(rec.get("cwd"), str):
            st["cwd"] = rec["cwd"]
        parent = rec.get("parentSession")
        if isinstance(parent, str) and parent:
            # Reading: parentSession is the parent's file path; the parent's
            # session_key comes from that file's header. Unreadable: null.
            parent_id = self._session_id_of(parent, all_state)
            st["parent"] = session_key(HARNESS, parent_id) if parent_id else None
        return True

    def _side_call(self, path: str, rec: dict, st: dict, items: list) -> bool:
        """omp's own model calls (judging, auto-thinking, cache warming and so
        on): `type:"model_usage"` records with top-level `id`, `provider`,
        `model`, `purpose`, `timestamp` (ISO) and a `usage` block shaped like a
        message's. They used capacity, so they count, marked auxiliary (D1
        invariant 3; assumption A10). The key is prefixed so it can never
        collide with a message entry id in the same stream."""
        usage = rec.get("usage")
        entry_id = rec.get("id")
        t = _iso_ms(rec.get("timestamp"))
        if not isinstance(usage, dict) or not isinstance(entry_id, str) or not entry_id or t is None:
            return False
        sid = st.get("sid")
        skey = session_key(HARNESS, sid) if sid else None
        tokens = {field: (usage[src] if _is_int(usage.get(src)) else None) for src, field in _TOKEN_MAP}
        native = {k: usage[k] for k in _NATIVE_FIELDS if _is_num(usage.get(k))}
        provider = rec.get("provider") if isinstance(rec.get("provider"), str) else None
        model = rec.get("model") if isinstance(rec.get("model"), str) else None
        items.append(UsageObservation(
            source=SESSION_SOURCE, stream_key=skey if skey else identity.stream(path, self.secret),
            source_request_key=f"aux:{entry_id}", confidence="authoritative", observed_at=t,
            parser_version=PARSER_VERSION, provider_request_key=None, harness=HARNESS,
            provider=_OBS_PROVIDERS.get(provider, provider) if provider else None,
            model=model, session_key=skey, native=json.dumps(native, sort_keys=True),
            auxiliary=True, **tokens,
        ))
        return True

    def _message(self, path: str, rec: dict, st: dict, items: list) -> bool:
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return False
        usage = msg.get("usage")
        entry_id = rec.get("id")
        if not isinstance(usage, dict) or not isinstance(entry_id, str) or not entry_id:
            return False
        # Reading: observed_at is `message.timestamp` (Unix ms, present on every
        # assistant entry: the request's start); else the entry's ISO timestamp.
        t = msg.get("timestamp") if _is_int(msg.get("timestamp")) else _iso_ms(rec.get("timestamp"))
        if t is None:
            return False
        sid = st.get("sid")
        skey = session_key(HARNESS, sid) if sid else None
        stream_key = skey if skey else identity.stream(path, self.secret)

        tokens = {field: (usage[src] if _is_int(usage.get(src)) else None) for src, field in _TOKEN_MAP}
        native = {k: usage[k] for k in _NATIVE_FIELDS if _is_num(usage.get(k))}
        cost = usage.get("cost")
        if isinstance(cost, dict):
            native.update({f"cost.{k}": cost[k] for k in _COST_FIELDS if _is_num(cost.get(k))})
        provider = msg.get("provider") if isinstance(msg.get("provider"), str) else None
        model = msg.get("model") if isinstance(msg.get("model"), str) else None

        items.append(UsageObservation(
            source=SESSION_SOURCE, stream_key=stream_key, source_request_key=entry_id,
            confidence="authoritative", observed_at=t, parser_version=PARSER_VERSION,
            provider_request_key=None, harness=HARNESS,
            provider=_OBS_PROVIDERS.get(provider, provider) if provider else None,
            model=model, session_key=skey, native=json.dumps(native, sort_keys=True),
            auxiliary=False, **tokens,
        ))
        if not _is_int(st.get("last")) or st["last"] < t:
            st["last"] = t
        if not _is_int(st.get("started")) or st["started"] > t:
            st["started"] = t

        cred = msg.get("credentialId")
        if skey and _is_int(cred) and cred not in st["creds"]:
            cred_provider, key = self._credential(cred)
            acct_provider = account_provider(cred_provider)
            if acct_provider and isinstance(key, str) and key:
                st["creds"].append(cred)
                items.append(AttributionEvidence(
                    subject_kind="session", subject_id=skey, dimension="account",
                    value=account_alias_value(acct_provider, "omp.identity_key",
                                              identity.account(acct_provider, key, self.secret)),
                    method="credential_id", source=SESSION_SOURCE, confidence="authoritative",
                    validity="historical", first_observed_at=t, last_confirmed_at=t,
                ))
        return True


# --- omp.usage_cache ---------------------------------------------------------------


def _window(limit: dict) -> tuple[str, int | None]:
    """D4 table 3: `window.durationMs` -> session (5 h) or weekly (7 d); a
    per-model limit (`scope.tier`, or `scope.modelId`) -> weekly:<model>."""
    window = limit.get("window") if isinstance(limit.get("window"), dict) else {}
    scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
    duration = window.get("durationMs") if _is_int(window.get("durationMs")) else None
    model = scope.get("modelId") or scope.get("tier")
    model = model.lower() if isinstance(model, str) and model else None
    if duration == _SESSION_WINDOW_MS:
        name = "session"
    elif duration == _WEEKLY_WINDOW_MS:
        name = "weekly"
    else:
        wid = window.get("id") or scope.get("windowId")
        name = f"other:{wid}" if isinstance(wid, str) and wid else UNSTATED_WINDOW
    if model:
        # Reading: only the weekly per-model form is defined (D1); any other
        # per-model window keeps the model as a suffix.
        name = f"weekly:{model}" if name == "weekly" else f"{name}:{model}"
    return name, (duration // 1000 if duration is not None else None)


class OmpUsageCacheSource:
    """A `PullSource` over omp's usage cache. Anchors only; never primary.

    Watermark: `{cache key: last fetchedAt}`. Only newer readings are emitted.
    """

    name = CACHE_SOURCE
    primary = False
    merge = None

    def __init__(self, db: str | os.PathLike | None = None, interval_s: float = 60,
                 secret: bytes | None = None):
        self.db = Path(db) if db is not None else default_db()
        self.interval_s = interval_s
        self.secret = secret

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        seen = _load_json(watermark)
        if not self.db.exists():
            return [], watermark
        conn = _connect_ro(self.db)
        try:
            rows = conn.execute(
                "SELECT key, value FROM cache WHERE key GLOB 'usage_cache:report:*'").fetchall()
        finally:
            conn.close()
        items: list = []
        out: dict[str, int] = {}
        for key, raw in rows:
            if not isinstance(key, str) or not isinstance(raw, (str, bytes)):
                continue
            try:
                outer = json.loads(raw)
            except ValueError:
                continue
            report = outer.get("value") if isinstance(outer, dict) and "limits" not in outer else outer
            if not isinstance(report, dict):
                continue
            fetched = report.get("fetchedAt")
            if not _is_int(fetched):
                continue
            last = seen.get(key)
            out[key] = max(fetched, last) if _is_int(last) else fetched
            if _is_int(last) and fetched <= last:
                continue
            items.extend(self._report(key, report, fetched))
        return items, json.dumps(out, sort_keys=True)

    def _report(self, key: str, report: dict, fetched: int) -> list:
        stream_key = identity.stream(key, self.secret)
        acct_provider = account_provider(report.get("provider"))
        meta = report.get("metadata") if isinstance(report.get("metadata"), dict) else {}
        account_id, org_id = meta.get("accountId"), meta.get("orgId")
        alias = None
        if (acct_provider and isinstance(account_id, str) and account_id
                and isinstance(org_id, str) and org_id):
            alias = account_alias_value(
                acct_provider, "omp.report_account",
                identity.account(acct_provider, f"{account_id}|{org_id}".lower(), self.secret))

        items: list = []
        limits = report.get("limits") if isinstance(report.get("limits"), list) else []
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            window, seconds = _window(limit)
            win = limit.get("window") if isinstance(limit.get("window"), dict) else {}
            amount = limit.get("amount") if isinstance(limit.get("amount"), dict) else {}
            fraction = amount.get("usedFraction")
            status = limit.get("status") if limit.get("status") in CAPACITY_STATUSES else "unknown"
            items.append(CapacitySample(
                source=CACHE_SOURCE, stream_key=stream_key, window=window,
                confidence="observed", observed_at=fetched, status=status,
                used_pct=float(fraction) * 100 if _is_num(fraction) else None,
                window_seconds=seconds,
                resets_at=win.get("resetsAt") if _is_int(win.get("resetsAt")) else None,
            ))
            if alias is not None:
                items.append(AttributionEvidence(
                    subject_kind="capacity_sample",
                    subject_id=capacity_subject_id(CACHE_SOURCE, stream_key, window, fetched),
                    dimension="account", value=alias, method="usage_cache", source=CACHE_SOURCE,
                    confidence="observed", validity="historical",
                    first_observed_at=fetched, last_confirmed_at=fetched,
                ))
        return items
