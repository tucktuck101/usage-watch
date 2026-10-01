"""The Claude Code collectors (D6): `claude.transcript` and
`claude.cached_utilization`, as pull sources.

`ClaudeTranscriptSource` tails `~/.claude/projects/**/*.jsonl` (subagent
files included) and emits usage observations, sessions, limit events and
session attribution evidence. `ClaudeCachedUtilizationSource` reads
`~/.claude.json` `.cachedUsageUtilization` and emits capacity samples with
account evidence.

Privacy (D4, D5): every line is parsed in memory and only the fields D4 maps
are kept. Notice text is matched in memory; only `kind`, `window` and
`resets_at` survive. Account and organisation IDs are keyed-hashed on read.
From `~/.claude.json` only `cachedUsageUtilization` and
`oauthAccount.accountUuid` / `oauthAccount.organizationUuid` are read. No
network calls.

Field names below were confirmed against this machine's real files
(2026-10-01), key names and types only.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from .. import identity
from ..model import (
    AttributionEvidence, CapacitySample, LimitEvent, Session, UsageObservation, session_key,
)
from ..runtime.core import account_alias_value

__all__ = [
    "ClaudeCachedUtilizationSource", "ClaudeTranscriptSource", "LIVE_MS", "keep_larger_output",
]

TRANSCRIPT = "claude.transcript"
# One stream for every Claude transcript: `message.id + requestId` is unique
# across sessions, and a session can copy another's history (resume, fork),
# which per-session streams would count twice (assumption A11).
STREAM = "claude.transcript:all"
CACHED = "claude.cached_utilization"
HARNESS = "claude"
PROVIDER = "anthropic"
ALIAS_KIND = "anthropic.account_org"
PARSER_VERSION = "claude.transcript/1"
SYNTHETIC = "<synthetic>"
LIVE_MS = 10 * 60 * 1000  # a session last seen within this is live

# `message.usage` token fields kept in `native` (D4: numeric token fields only).
_NATIVE = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
_THINKING = "output_tokens_details.thinking_tokens"

# quotaLimits.rateLimitType -> window. Values seen: five_hour, seven_day.
_RATE_LIMIT_WINDOW = {"five_hour": "session", "seven_day": "weekly"}

# cachedUsageUtilization.utilization keys -> (window, window_seconds).
# Reading: only `seven_day_<model family>` is a per-model window. Other
# `seven_day_*` keys (`oauth_apps`, `cowork`, ...) and codename keys are not
# model windows, and are not in D4's allowlist, so they are dropped.
_MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable")
_CACHED_WINDOWS = {"five_hour": ("session", 18000), "seven_day": ("weekly", 604800)}
_CACHED_WINDOWS.update({f"seven_day_{m}": (f"weekly:{m}", 604800) for m in _MODEL_FAMILIES})

# System notices (`type:"system"`, `subtype:"informational"`). Shapes seen:
# "usage limit reached · continuing automatically at 3:30 pm · ..." and
# "usage limit reset · continuing automatically". R9's "hit your session
# limit · resets 3pm" form is matched too.
_NOTICE_HIT = re.compile(r"\blimit reached\b|\bhit your\b[^·]*\blimit\b", re.I)
_NOTICE_RESET = re.compile(r"\blimit (?:has )?reset\b", re.I)
_NOTICE_AT = re.compile(r"\b(?:at|resets)\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.I)
_NOTICE_WINDOW = (("session", re.compile(r"\bsession limit\b", re.I)),
                  ("weekly", re.compile(r"\bweekly limit\b", re.I)))


# --- Small parsers ---------------------------------------------------------------------

def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _iso_ms(v: Any) -> int | None:
    """An ISO 8601 time (with `Z` or an offset) as UTC ms; None if unparseable or naive."""
    if not isinstance(v, str):
        return None
    try:
        t = dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return int(t.timestamp() * 1000)


def _epoch_ms(v: Any) -> int | None:
    """Unix seconds (10 digits seen) or ms, as ms."""
    n = _int(v)
    if n is None or n <= 0:
        return None
    return n * 1000 if n < 100_000_000_000 else n


def keep_larger_output(old: UsageObservation, new: UsageObservation) -> UsageObservation:
    """D6's counting rule: keep the snapshot with the larger `output_tokens`.
    A tie, or an unknown new value, keeps the old snapshot."""
    o, n = old.output_tokens, new.output_tokens
    if n is not None and (o is None or n > o):
        # The request stays with the session it was first seen in: a copy of
        # its history in another session (resume, fork) must not take it over.
        return dataclasses.replace(new, session_key=old.session_key)
    return old


def account_alias(account_uuid: str, org_uuid: str, secret: bytes | None = None) -> str:
    """`account` evidence value for a Claude login: the keyed hash of
    `"<accountUuid>|<organizationUuid>"`, lowercased, as an alias value."""
    raw = f"{account_uuid}|{org_uuid}".lower()
    return account_alias_value(PROVIDER, ALIAS_KIND, identity.account(PROVIDER, raw, secret))


def _pair(a: Any, b: Any) -> tuple[str, str] | None:
    if isinstance(a, str) and a and isinstance(b, str) and b:
        return a, b
    return None


def read_state_file(path: Path) -> tuple[dict | None, tuple[str, str] | None]:
    """From `~/.claude.json`, only `cachedUsageUtilization` and the
    `oauthAccount` (accountUuid, organizationUuid) pair. Everything else is
    discarded before returning. A missing or unreadable file gives (None, None)."""
    try:
        with open(path, "rb") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    cached = data.get("cachedUsageUtilization")
    oauth = data.get("oauthAccount")
    pair = _pair(oauth.get("accountUuid"), oauth.get("organizationUuid")) if isinstance(oauth, dict) else None
    return (cached if isinstance(cached, dict) else None), pair


# --- Tail watermarks -----------------------------------------------------------------------

def _load_marks(watermark: str | None) -> dict[str, tuple[int, int]]:
    """`{path: [inode, byte_offset]}`. A missing or unreadable watermark starts empty."""
    if not watermark:
        return {}
    try:
        data = json.loads(watermark)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for path, mark in data.items():
        if (isinstance(mark, list) and len(mark) == 2
                and _int(mark[0]) is not None and _int(mark[1]) is not None):
            out[path] = (mark[0], mark[1])
    return out


def _read_new(path: str, mark: tuple[int, int] | None) -> tuple[int, int, bytes, int] | None:
    """Read the complete new lines of `path` after `mark`.

    Returns (inode, start, data, new_offset); `data` ends at the last newline.
    A new inode or a shorter file starts over at 0. An incomplete final line
    is left for the next pass. None if the file can't be read.
    """
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            start = 0
            if mark is not None and mark[0] == st.st_ino and mark[1] <= st.st_size:
                start = mark[1]
            if st.st_size <= start:
                return st.st_ino, start, b"", start
            fh.seek(start)
            data = fh.read(st.st_size - start)
    except OSError:
        return None
    end = data.rfind(b"\n")
    if end < 0:
        return st.st_ino, start, b"", start
    return st.st_ino, start, data[:end + 1], start + end + 1


# --- The transcript source -------------------------------------------------------------------

class _SessionAgg:
    __slots__ = ("session_id", "first", "last", "cwd", "main_cwd", "from_start", "branches", "owners")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.first: int | None = None
        self.last: int | None = None
        self.cwd: str | None = None
        self.main_cwd: str | None = None
        self.from_start = False   # this pass read the session's own file from byte 0
        self.branches: list[tuple[int, str]] = []
        self.owners: set[tuple[str, str]] = set()


class ClaudeTranscriptSource:
    """A `PullSource` tailing Claude Code transcripts. Primary for Claude Code.

    Reading: `session_key = "claude:" + sessionId` for every line, subagent
    files included; every observation shares one `stream_key` (STREAM), since
    request keys are unique across sessions and copied history must not count
    twice. Subagent lines carry their parent's
    `sessionId` (confirmed), so a subagent's requests land in the parent's
    stream and session; `parent_session_key` is not set.
    """

    name = TRANSCRIPT
    primary = True
    merge = staticmethod(keep_larger_output)

    def __init__(self, root: str | os.PathLike | None = None,
                 state_file: str | os.PathLike | None = None, interval_s: float = 30,
                 clock: Callable[[], float] = time.time, secret: bytes | None = None,
                 tz: dt.tzinfo | None = None):
        self.root = Path(root) if root is not None else Path.home() / ".claude" / "projects"
        self.state_file = Path(state_file) if state_file is not None else Path.home() / ".claude.json"
        self.interval_s = interval_s
        self.clock = clock
        self.secret = secret
        self.tz = tz  # for notice times; None is the machine's local zone
        # In-memory only; lost on restart, which costs at most one repeated
        # evidence row per session (identity includes valid_from).
        self._branch: dict[str, str] = {}              # session_key -> last branch emitted
        self._live: dict[str, int] = {}                # session_key -> last_seen_at, live ones
        self._last_seen: dict[str, int] = {}           # session_key -> last_seen_at emitted
        self._login: dict[str, tuple[str, int]] = {}   # session_key -> (value, first seen live)
        self._started = False

    # -- the pass --

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        now = int(self.clock() * 1000)
        backfill = watermark is None
        marks = _load_marks(watermark)
        new_marks: dict[str, list[int]] = {}
        sessions: dict[str, _SessionAgg] = {}
        observations: dict[tuple[str, str], UsageObservation] = {}
        limits: list[LimitEvent] = []

        for path in self._files():
            got = _read_new(path, marks.get(path))
            if got is None:
                if path in marks:
                    new_marks[path] = list(marks[path])
                continue
            inode, start, data, offset = got
            new_marks[path] = [inode, offset]
            if not data:
                continue
            p = Path(path)
            main_id = p.stem if p.parent.name != "subagents" else None
            for line in data.splitlines():
                self._line(line, main_id, start == 0, sessions, observations, limits)

        items: list = []
        items += self._sessions(sessions, now, backfill)
        items += observations.values()
        items += limits
        items += self._evidence(sessions, now)
        self._started = True
        return items, json.dumps(new_marks, sort_keys=True)

    def _files(self) -> list[str]:
        try:
            return sorted(str(p) for p in self.root.rglob("*.jsonl") if p.is_file())
        except OSError:
            return []

    # -- one line --

    def _line(self, raw: bytes, main_id: str | None, from_start: bool,
              sessions: dict[str, _SessionAgg], observations: dict, limits: list) -> None:
        if not raw.strip():
            return
        try:
            d = json.loads(raw)
        except ValueError:
            return  # D5: never logged with its content
        if not isinstance(d, dict):
            return
        sid = d.get("sessionId")
        if not isinstance(sid, str) or not sid:
            return
        sk = session_key(HARNESS, sid)
        ts = _iso_ms(d.get("timestamp"))
        agg = sessions.get(sk)
        if agg is None:
            agg = sessions[sk] = _SessionAgg(sid)
        own_file = main_id == sid
        if own_file and from_start:
            agg.from_start = True
        if ts is not None:
            agg.first = ts if agg.first is None else min(agg.first, ts)
            agg.last = ts if agg.last is None else max(agg.last, ts)
            branch = d.get("gitBranch")
            if isinstance(branch, str) and branch:
                agg.branches.append((ts, branch))
        cwd = d.get("cwd")
        if isinstance(cwd, str) and cwd:
            agg.cwd = cwd
            if own_file:
                agg.main_cwd = cwd

        kind = d.get("type")
        if kind == "bridge-session":
            pair = _pair(d.get("ownerAccountUuid"), d.get("ownerOrganizationUuid"))
            if pair:
                agg.owners.add(pair)
        elif kind == "assistant":
            if d.get("error") == "rate_limit" and ts is not None:
                self._rate_limit(d, sk, ts, limits)
            obs = self._observation(d, sk, ts)
            if obs is not None:
                key = (obs.stream_key, obs.source_request_key)
                old = observations.get(key)
                observations[key] = obs if old is None else keep_larger_output(old, obs)
        elif kind == "system" and d.get("subtype") == "informational" and ts is not None:
            self._notice(d, sk, ts, limits)

    def _observation(self, d: dict, sk: str, ts: int | None) -> UsageObservation | None:
        msg = d.get("message")
        if not isinstance(msg, dict) or ts is None:
            return None
        usage = msg.get("usage")
        mid = msg.get("id")
        model = msg.get("model")
        if not isinstance(usage, dict) or not isinstance(mid, str) or not mid:
            return None
        if model == SYNTHETIC:
            return None
        rid = d.get("requestId")
        rid = rid if isinstance(rid, str) and rid else None
        details = usage.get("output_tokens_details")
        thinking = _int(details.get("thinking_tokens")) if isinstance(details, dict) else None
        native = {k: usage[k] for k in _NATIVE if _int(usage.get(k)) is not None}
        if thinking is not None:
            native[_THINKING] = thinking
        return UsageObservation(
            source=TRANSCRIPT, stream_key=STREAM, source_request_key=f"{mid}+{rid or ''}",
            confidence="authoritative", observed_at=ts, parser_version=PARSER_VERSION,
            provider_request_key=identity.request(PROVIDER, rid, self.secret) if rid else None,
            harness=HARNESS, provider=PROVIDER,
            model=model if isinstance(model, str) and model else None,
            session_key=sk, native=json.dumps(native, sort_keys=True) if native else None,
            uncached_input_tokens=_int(usage.get("input_tokens")),
            cache_read_input_tokens=_int(usage.get("cache_read_input_tokens")),
            cache_write_input_tokens=_int(usage.get("cache_creation_input_tokens")),
            output_tokens=_int(usage.get("output_tokens")),
            reasoning_output_tokens=thinking,
        )

    def _rate_limit(self, d: dict, sk: str, ts: int, limits: list) -> None:
        uuid = d.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return
        q = d.get("quotaLimits")
        q = q if isinstance(q, dict) else {}
        limits.append(LimitEvent(
            source=TRANSCRIPT, stream_key=sk, source_key=uuid, kind="hit",
            confidence="authoritative", observed_at=ts,
            window=_RATE_LIMIT_WINDOW.get(q.get("rateLimitType")),
            resets_at=_epoch_ms(q.get("resetsAt")),
        ))

    def _notice(self, d: dict, sk: str, ts: int, limits: list) -> None:
        uuid = d.get("uuid")
        text = d.get("content")
        if not isinstance(uuid, str) or not uuid or not isinstance(text, str):
            return
        if _NOTICE_HIT.search(text):
            kind = "hit"
        elif _NOTICE_RESET.search(text):
            kind = "reset"
        else:
            return
        window = next((w for w, rx in _NOTICE_WINDOW if rx.search(text)), None)
        resets_at = self._notice_time(text, ts) if kind == "hit" else None
        limits.append(LimitEvent(
            source=TRANSCRIPT, stream_key=sk, source_key=uuid, kind=kind,
            confidence="authoritative", observed_at=ts, window=window, resets_at=resets_at,
        ))

    def _notice_time(self, text: str, ts: int) -> int | None:
        """The notice's "at 3:30 pm" as UTC ms: the first such wall-clock time
        at or after the notice, in the machine's zone.

        Reading: the notice is in local time. Confirmed on this machine's three
        such notices against the same session's `quotaLimits.resetsAt`.
        """
        m = _NOTICE_AT.search(text)
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
        at = dt.datetime.fromtimestamp(ts / 1000, self.tz)
        cand = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand < at.replace(second=0, microsecond=0):
            cand += dt.timedelta(days=1)
        return int(cand.timestamp() * 1000)

    # -- items built after the files are read --

    def _sessions(self, sessions: dict[str, _SessionAgg], now: int, backfill: bool) -> list:
        items: list = []
        for sk, agg in sessions.items():
            last = agg.last
            prior = self._last_seen.get(sk)
            if prior is not None and (last is None or prior > last):
                last = prior
            live = last is not None and now - last <= LIVE_MS
            # Reading: the store keeps the newer non-null started_at, so it is
            # only sent when it is the true start: the session's own file was
            # read from byte 0, or this is the first backfill of every file.
            started = agg.first if (agg.from_start or backfill) else None
            items.append(Session(
                session_key=sk, harness=HARNESS, session_id=agg.session_id,
                started_at=started, last_seen_at=last, live=live,
                cwd=agg.main_cwd or agg.cwd,
            ))
            if last is not None:
                self._last_seen[sk] = last
            if live:
                self._live[sk] = last
            else:
                self._live.pop(sk, None)
                self._login.pop(sk, None)

        # Sessions that went quiet since the last pass are no longer live.
        for sk, last in list(self._live.items()):
            if sk not in sessions and now - last > LIVE_MS:
                del self._live[sk]
                self._login.pop(sk, None)
                items.append(Session(session_key=sk, harness=HARNESS,
                                     session_id=sk.split(":", 1)[1], live=False))

        # Reading: after a restart the in-memory live set is empty, so any
        # session the store may still mark live, whose own file is quiet, is
        # cleared once, on this process's first pass.
        if not self._started and not backfill:
            for path in self._files():
                p = Path(path)
                if p.parent.name == "subagents":
                    continue
                sk = session_key(HARNESS, p.stem)
                if sk in sessions or sk in self._live:
                    continue
                try:
                    quiet = now - int(p.stat().st_mtime * 1000) > LIVE_MS
                except OSError:
                    continue
                if quiet:
                    items.append(Session(session_key=sk, harness=HARNESS,
                                         session_id=p.stem, live=False))
        return items

    def _evidence(self, sessions: dict[str, _SessionAgg], now: int) -> list:
        items: list = []
        for sk, agg in sessions.items():
            # Branch: historical, authoritative, valid from when it was first seen.
            prev = self._branch.get(sk)
            for ts, branch in sorted(agg.branches):
                if branch != prev:
                    items.append(AttributionEvidence(
                        subject_kind="session", subject_id=sk, dimension="branch", value=branch,
                        method="transcript", source=TRANSCRIPT, confidence="authoritative",
                        validity="historical", first_observed_at=now, last_confirmed_at=now,
                        valid_from=ts,
                    ))
                    prev = branch
            if prev is not None:
                self._branch[sk] = prev
            # Transcript owner: historical, authoritative.
            for acct, org in sorted(agg.owners):
                items.append(AttributionEvidence(
                    subject_kind="session", subject_id=sk, dimension="account",
                    value=account_alias(acct, org, self.secret), method="transcript_owner",
                    source=TRANSCRIPT, confidence="authoritative", validity="historical",
                    first_observed_at=now, last_confirmed_at=now,
                ))

        # The current login, for live sessions only (D2): live, inferred.
        if self._live:
            _, pair = read_state_file(self.state_file)
            if pair is not None:
                value = account_alias(*pair, self.secret)
                for sk in sorted(self._live):
                    seen = self._login.get(sk)
                    first = seen[1] if seen is not None and seen[0] == value else now
                    self._login[sk] = (value, first)
                    items.append(AttributionEvidence(
                        subject_kind="session", subject_id=sk, dimension="account", value=value,
                        method="state_file", source=TRANSCRIPT, confidence="inferred",
                        validity="live", first_observed_at=first, last_confirmed_at=now,
                        valid_from=first,
                    ))
        return items


# --- The cached utilization source -------------------------------------------------------------

def capacity_subject_id(sample: CapacitySample) -> str:
    """A capacity sample's natural key, used as its attribution `subject_id`."""
    return f"{sample.source}|{sample.stream_key}|{sample.window}|{sample.observed_at}"


class ClaudeCachedUtilizationSource:
    """A `PullSource` over `~/.claude.json` `.cachedUsageUtilization`. Not primary.

    The watermark is the last `fetchedAtMs` seen, as decimal text; a pass
    whose `fetchedAtMs` is not newer emits nothing.
    """

    name = CACHED
    primary = False
    merge = None

    def __init__(self, path: str | os.PathLike | None = None, interval_s: float = 300,
                 clock: Callable[[], float] = time.time, secret: bytes | None = None):
        self.path = Path(path) if path is not None else Path.home() / ".claude.json"
        self.interval_s = interval_s
        self.clock = clock
        self.secret = secret

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        cached, pair = read_state_file(self.path)
        if cached is None:
            return [], watermark
        fetched = _int(cached.get("fetchedAtMs"))
        if fetched is None:
            return [], watermark
        try:
            last = int(watermark) if watermark else None
        except ValueError:
            last = None
        if last is not None and fetched <= last:
            return [], watermark
        windows = cached.get("utilization")
        if not isinstance(windows, dict):
            return [], str(fetched)

        stream_key = identity.stream(str(self.path), self.secret)
        # Reading: the snapshot names the account it was fetched for
        # (`accountUuid`). If that isn't the current login, the login changed
        # after the fetch, and `oauthAccount` says nothing about the sample.
        cached_acct = cached.get("accountUuid")
        if pair is not None and isinstance(cached_acct, str) and cached_acct.lower() != pair[0].lower():
            pair = None
        value = account_alias(*pair, self.secret) if pair is not None else None
        now = int(self.clock() * 1000)

        items: list = []
        for key, (window, seconds) in _CACHED_WINDOWS.items():
            w = windows.get(key)
            if not isinstance(w, dict):
                continue
            sample = CapacitySample(
                source=CACHED, stream_key=stream_key, window=window, confidence="observed",
                observed_at=fetched, status="unknown",  # locked_reason: only null seen
                used_pct=_num(w.get("utilization")), window_seconds=seconds,
                resets_at=_iso_ms(w.get("resets_at")),
            )
            items.append(sample)
            if value is not None:
                items.append(AttributionEvidence(
                    subject_kind="capacity_sample", subject_id=capacity_subject_id(sample),
                    dimension="account", value=value, method="state_file", source=CACHED,
                    confidence="observed", validity="historical",
                    first_observed_at=now, last_confirmed_at=now,
                ))
        return items, str(fetched)
