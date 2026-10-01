"""The `codex.rollout` collector (D6): Codex CLI session files as a tail source.

Reads `<CODEX_HOME or ~/.codex>/sessions/**/rollout-*.jsonl`, only the bytes
added since the last pass, and emits:

- a `Session` per rollout file's own session, plus `branch` evidence from
  `session_meta.payload.git.branch` (`historical`, `authoritative`, D2);
- a primary `UsageObservation` per `token_count` event whose
  `info.total_token_usage` grew, carrying the growth (the D6 delta rule);
- `CapacitySample` anchors from `payload.rate_limits`, when their values
  change.

No account evidence: without OTel, Codex's account isn't known (D2).

Field facts checked against a real history (693 files, 38,618 `token_count`
events), counts only:

- **The file's own session is its first `session_meta`.** Every file starts
  with one. A forked file's first `session_meta` carries `forked_from_id`,
  and its second line is the parent's `session_meta` (copied history, id =
  that `forked_from_id`); a resumed file repeats its own `session_meta`
  later. Parent `session_meta` lines are ignored. No `token_count` key
  appears in two sessions, so forks don't replay token events.
- **Totals restart per file.** In every file the first event's
  `total_token_usage` equals its `last_token_usage`, including the two
  sessions that continue in a second file. So the delta state is kept per
  file, starting from zero; per-session state would read a continuation
  file's first event as a decrease and drop it.
- **Input includes cached:** `input_tokens >= cached_input_tokens` and
  `total_tokens == input_tokens + output_tokens` on every event, so
  `uncached = Δinput − Δcached`. **Reasoning is a subset of output:**
  `reasoning_output_tokens <= output_tokens` on every event and total
  excludes it separately.
- `rate_limits.{primary,secondary}` hold `used_percent` (0-100),
  `window_minutes` and `resets_at` (epoch seconds). `primary` is 300 or
  10080 minutes, so windows are named by length, not by slot (D1). No
  status field exists: status stays `unknown`.

Nothing else is read (D4, D5): prompts, responses and tool output are never
parsed beyond a substring test, and nothing is stored but mapped fields.

Readings of the design that this module leaves open are marked "Reading:".
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from .. import identity
from ..model import (
    UNBOUNDED_START, AttributionEvidence, CapacitySample, Session, UsageObservation, session_key,
)

__all__ = ["CodexRolloutSource", "LIVE_MS", "PARSER_VERSION", "SOURCE", "window_name"]

SOURCE = "codex.rollout"
HARNESS = "codex"
PROVIDER = "openai"
PARSER_VERSION = "codex.rollout/1"
LIVE_MS = 10 * 60 * 1000            # live if seen within 10 minutes
CHUNK = 8 * 1024 * 1024
WINDOW_NAMES = {18000: "session", 604800: "weekly"}

# The `total_token_usage` fields read, in a fixed order.
_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens", "total_tokens")
_IN, _CACHED, _WRITE, _OUT, _REASON, _TOTAL = range(len(_FIELDS))

# Lines worth parsing. Anything else only gives its timestamp, read from
# the line's prefix without parsing the body.
_WANTED = (b'"session_meta"', b'"turn_context"', b'"token_count"')
_TS = re.compile(rb'^\{(?:"ordinal":\d+,)?"timestamp":"([^"]{10,40})"')


def window_name(seconds: int) -> str:
    """D1: Codex windows map by length; anything else is `other:<seconds>s`."""
    return WINDOW_NAMES.get(seconds, f"other:{seconds}s")


def _ms(ts) -> int | None:
    """An ISO-8601 UTC timestamp (`...Z`) as UTC ms, or None."""
    if not isinstance(ts, str):
        return None
    try:
        t = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (t - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)) // dt.timedelta(milliseconds=1)


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _new_state() -> dict:
    """Per-file state, kept in the watermark.
    sk: own session_key; sid: its id; model: from the last turn_context;
    prev: the last total_token_usage baseline (None: zero); rl: last emitted
    anchor per window; branch: last branch emitted; last: last seen (ms);
    live: whether the session was last written as live."""
    return {"sk": None, "sid": None, "model": None, "prev": None, "rl": {},
            "branch": None, "last": None, "live": False}


def _load(watermark: str | None) -> tuple[dict, dict]:
    """(files, state). A missing or unreadable watermark starts over."""
    if not watermark:
        return {}, {}
    try:
        data = json.loads(watermark)
    except ValueError:
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    files, state = {}, {}
    for path, pos in (data.get("files") or {}).items():
        if (isinstance(pos, list) and len(pos) == 2
                and all(_int(x) is not None and x >= 0 for x in pos)):
            files[path] = pos
            st = (data.get("state") or {}).get(path)
            state[path] = {**_new_state(), **st} if isinstance(st, dict) else _new_state()
    return files, state


class CodexRolloutSource:
    """A `PullSource` over Codex rollout files. Primary for Codex (D6)."""

    name = SOURCE
    primary = True
    merge = None

    def __init__(self, root: str | os.PathLike | None = None, interval_s: float = 30,
                 clock: Callable[[], float] = time.time, secret: bytes | None = None):
        if root is None:
            root = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions"
        self.root = Path(root)
        self.interval_s = interval_s
        self.clock = clock
        self.secret = secret
        self.stats: dict[str, int] = {}

    # -- the contract --

    def collect(self, watermark: str | None) -> tuple[list, str | None]:
        files, state = _load(watermark)
        now = int(self.clock() * 1000)
        self.stats = {k: 0 for k in ("files_read", "bytes", "token_count", "info_null",
                                      "repeats", "decreases", "observations", "anchors",
                                      "bad_lines")}
        items: list = []
        seen: set[str] = set()
        for path in sorted(self.root.rglob("rollout-*.jsonl")) if self.root.is_dir() else []:
            key = str(path)
            seen.add(key)
            pos = files.get(key)
            st = state.get(key)
            try:
                with open(path, "rb") as fh:
                    info = os.fstat(fh.fileno())
                    if pos is None or pos[0] != info.st_ino or info.st_size < pos[1]:
                        pos, st = [info.st_ino, 0], _new_state()  # new or replaced: from 0
                    if info.st_size > pos[1]:
                        offset = self._tail(fh, pos[1], key, st, items)
                        self.stats["files_read"] += 1
                        if offset > pos[1] and st["sk"] is not None:
                            items.append(self._session(st, now))
                        pos[1] = offset
                    elif st["live"] and st["sk"] is not None and not self._live(st, now):
                        items.append(self._session(st, now))  # gone quiet: no longer live
            except OSError:
                continue  # unreadable now; its watermark is kept and it's retried
            files[key], state[key] = pos, st
        files = {k: v for k, v in files.items() if k in seen}
        state = {k: v for k, v in state.items() if k in seen}
        return items, json.dumps({"files": files, "state": state}, separators=(",", ":"))

    # -- reading --

    def _tail(self, fh, offset: int, key: str, st: dict, items: list) -> int:
        """Read complete lines from `offset`; return the offset after the last one."""
        fh.seek(offset)
        rest = b""
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            buf = rest + chunk
            end = buf.rfind(b"\n")
            if end < 0:
                rest = buf
                continue
            for line in buf[:end].split(b"\n"):
                self._line(line, key, st, items)
            offset += end + 1
            self.stats["bytes"] += end + 1
            rest = buf[end + 1:]
        return offset  # an incomplete final line waits for the next pass

    def _line(self, line: bytes, key: str, st: dict, items: list) -> None:
        m = _TS.match(line)
        if m is not None:
            t = _ms(m.group(1).decode("ascii", "replace"))
            if t is not None and (st["last"] is None or t > st["last"]):
                st["last"] = t
        if not any(w in line for w in _WANTED):
            return
        try:
            rec = json.loads(line)
        except ValueError:
            self.stats["bad_lines"] += 1
            return
        if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
            return
        kind, payload, ts = rec.get("type"), rec["payload"], rec.get("timestamp")
        at = _ms(ts)
        if at is None:
            return
        if st["last"] is None or at > st["last"]:
            st["last"] = at
        if kind == "session_meta":
            self._meta(payload, at, st, items)
        elif kind == "turn_context":
            if isinstance(payload.get("model"), str):
                st["model"] = payload["model"]
        elif kind == "event_msg" and payload.get("type") == "token_count":
            self.stats["token_count"] += 1
            self._usage(payload.get("info"), ts, at, key, st, items)
            self._anchors(payload.get("rate_limits"), at, key, st, items)

    def _meta(self, p: dict, at: int, st: dict, items: list) -> None:
        sid = p.get("id")
        if not isinstance(sid, str) or not sid:
            return
        if st["sk"] is None:
            # The file's own session is its first session_meta (module doc).
            st["sk"], st["sid"] = session_key(HARNESS, sid), sid
            items.append(Session(
                session_key=st["sk"], harness=HARNESS, session_id=sid,
                started_at=_ms(p.get("timestamp")) or at,
                cwd=p["cwd"] if isinstance(p.get("cwd"), str) else None))
        elif sid != st["sid"]:
            return  # a fork parent's copied session_meta
        git = p.get("git")
        branch = git.get("branch") if isinstance(git, dict) else None
        if isinstance(branch, str) and branch and branch != st["branch"]:
            # Reading: the first branch holds from the session's start; a
            # changed branch on a later own session_meta (a resume) holds from then.
            items.append(AttributionEvidence(
                subject_kind="session", subject_id=st["sk"], dimension="branch", value=branch,
                method="record", source=SOURCE, confidence="authoritative",
                validity="historical", first_observed_at=at, last_confirmed_at=at,
                valid_from=UNBOUNDED_START if st["branch"] is None else at))
            st["branch"] = branch

    def _stream(self, key: str, st: dict) -> str:
        """D6: the session_key; a keyed hash of the path only if no session_meta came first."""
        return st["sk"] if st["sk"] is not None else identity.stream(key, self.secret)

    def _usage(self, info, ts: str, at: int, key: str, st: dict, items: list) -> None:
        if not isinstance(info, dict):
            self.stats["info_null"] += 1  # D4: not a zero, no observation
            return
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        cur = [_int(total.get(f)) for f in _FIELDS]
        write_absent = "cache_write_input_tokens" not in total
        if write_absent:
            # Omission means zero here (D1 null arithmetic; assumption A12):
            # older Codex versions don't write the field, every one of the
            # 19,403 events on this machine that does carries 0, and OpenAI's
            # prompt caching has no separate cache-write step.
            cur[_WRITE] = 0
        if cur[_TOTAL] is None:
            return
        prev = st["prev"] or [0 if c is not None else None for c in cur]
        if cur == prev:
            self.stats["repeats"] += 1
            return
        st["prev"] = cur
        if any(c is not None and p is not None and c < p for c, p in zip(cur, prev)):
            self.stats["decreases"] += 1  # a new baseline; nothing emitted
            return
        d = [c - p if c is not None and p is not None else None for c, p in zip(cur, prev)]
        uncached = d[_IN] - d[_CACHED] if d[_IN] is not None and d[_CACHED] is not None else None
        # `native` holds only what the source reported: an inferred 0 stays out.
        native = {f: v for f, v in zip(_FIELDS, d)
                  if v is not None and not (write_absent and f == "cache_write_input_tokens")}
        items.append(UsageObservation(
            source=SOURCE, stream_key=self._stream(key, st),
            source_request_key=f"{ts}|{cur[_TOTAL]}", confidence="authoritative",
            observed_at=at, parser_version=PARSER_VERSION, harness=HARNESS, provider=PROVIDER,
            model=st["model"], session_key=st["sk"],
            uncached_input_tokens=uncached, cache_read_input_tokens=d[_CACHED],
            cache_write_input_tokens=d[_WRITE], output_tokens=d[_OUT],
            reasoning_output_tokens=d[_REASON],
            native=json.dumps(native, sort_keys=True, separators=(",", ":"))))
        self.stats["observations"] += 1

    def _anchors(self, rl, at: int, key: str, st: dict, items: list) -> None:
        if not isinstance(rl, dict):
            return
        for slot in ("primary", "secondary"):
            w = rl.get(slot)
            if not isinstance(w, dict):
                continue
            minutes = _int(w.get("window_minutes"))
            if minutes is None or minutes <= 0:
                continue
            seconds = minutes * 60
            used = _num(w.get("used_percent"))
            resets = _num(w.get("resets_at"))
            resets_ms = int(resets * 1000) if resets is not None else None
            name = window_name(seconds)
            values = [used, seconds, resets_ms]
            if st["rl"].get(name) == values:
                continue  # unchanged for this session
            st["rl"][name] = values
            items.append(CapacitySample(
                source=SOURCE, stream_key=self._stream(key, st), window=name,
                confidence="authoritative", observed_at=at, status="unknown",
                used_pct=used, window_seconds=seconds, resets_at=resets_ms))
            self.stats["anchors"] += 1

    # -- sessions --

    def _live(self, st: dict, now: int) -> bool:
        return st["last"] is not None and now - st["last"] <= LIVE_MS

    def _session(self, st: dict, now: int) -> Session:
        st["live"] = self._live(st, now)
        return Session(session_key=st["sk"], harness=HARNESS, session_id=st["sid"],
                       last_seen_at=st["last"], live=st["live"])
