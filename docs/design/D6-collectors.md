# D6: Collectors and reconciliation

Status: draft, revised after review (2026-10-01). Rests on
[R1](../research/R1-native-otel.md), [R2](../research/R2-local-token-records.md),
[R9](../research/R9-credential-free-limits.md),
[R10](../research/R10-omp-extension-limits.md). Scope markers as in
[D1](D1-model.md).

## Two contracts, one write path [F]

| Contract | Shape | For |
|---|---|---|
| **Pull source** | `collect(watermark) -> (items, new_watermark)`. The runtime calls it on a schedule. It never blocks for long | file tails and snapshot reads |
| **Push source** | `start(sink)` / `stop()`. The source hands items to `sink` whenever they arrive | the OTLP receiver, taps [X] |

- Both produce the same **items**: observations, anchors, limit events,
  state samples and context events, each with its provenance.
- Items go through one **write path**:
  1. check them against the source's field allowlist (D5);
  2. write the observations and other records;
  3. hand new observations to the **reconciler**, which updates canonical
     usage events;
  4. update attributions (D2);
  5. update `collector_status`.
- A source that fails is reported in `collector_status` and retried. It
  never stops the others.
- **No source makes an outbound network call.** The OTLP receiver listens
  on 127.0.0.1 only.

## Sources [F for the proof-of-concept set; the rest X or L]

| Source | Contract | Reads | Gives | Confidence | Counting rule |
|---|---|---|---|---|---|
| `claude.transcript` [F] | pull, tail | `~/.claude/projects/**/*.jsonl` | observations, sessions, limit events | authoritative | Within the source, collapse on `(message.id, requestId)`, keeping the **final** snapshot (largest `output_tokens`). Skip `<synthetic>`. Subagent files are their own sessions |
| `codex.rollout` [F] | pull, tail | `~/.codex/sessions/**/*.jsonl` | observations, sessions, **anchors** (`rate_limits`) | authoritative | Per session, the change in `total_token_usage`. Skip unchanged repeats. A decrease starts a new baseline. The model comes from the preceding `turn_context` |
| `omp.session` [F] | pull, tail | `~/.omp/agent/sessions/**/*.jsonl` | observations (with `auxiliary`), harness cost, sessions, `credentialId` | authoritative | Collapse on the entry ID |
| `omp.usage_cache` [F] | pull, 60 s | `agent.db` `cache`, `usage_cache:report:*` values only | anchors for every omp login | observed | Ignore entries by age, not by status. Hash identity on read |
| `claude.statusline` [F] | pull, 10 s | the tap's snapshot files | anchors for the Claude Code account | authoritative | Newest per window |
| `claude.cached_utilization` [F] | pull, 5 min | `~/.claude.json` `.cachedUsageUtilization` | anchors, including per-model weekly | observed | Only when `fetchedAtMs` is newer than the newest anchor |
| `omp.usage_history` [X] | pull, 15 min | `agent.db` `usage_history` | anchors, backfill | observed | Never used for nudges (D8) |
| `otlp.receiver` [L] | push | 127.0.0.1, OTLP/HTTP json and protobuf | observations, cost, sessions, identity | authoritative | D4 mapping only |
| `screen` [F, exists] | pull, 5 s / 300 s | tmux panes | state samples, limit events, reset hints | inferred | Today's adapters |
| `topology` [F, exists] | pull, with `screen` | tmux, ps, git, workmux | live facts for attribution | observed | Today's scan |

**Watermarks** for tail sources are `(path, inode, byte offset)` per file.
A new inode or a shorter file starts over from 0, which is safe because
observation keys are unique. The first run backfills everything present,
with `historical` attributions only (D2).

## Reconciliation [F]

- **Grouping:** observations of the same request are grouped by a shared
  provider request ID where the sources carry one (Claude transcript
  `requestId` = Claude OTel `request_id`). Otherwise each source's
  observation stands as its own event, and the source's own key prevents
  double counting within it.
- **Choosing values:** per group, the canonical event takes the values of
  the highest-confidence observation, then the most complete one.
- **Disagreement:** where observations differ, the event records
  `disagreement` (the largest per-field difference) and keeps pointing at
  every observation. `doctor` reports how many disagreements appear per
  source pair, which is an early sign of parser drift.
- **Versioning:** reconciliation rules have a version. Re-running them over
  stored observations rebuilds events.
- **What reconciliation never does:** merge observations it can't prove are
  the same request. Two unlinkable sources describing the same work would
  double count. So for any one harness, **only one usage source is enabled
  as primary** until a shared request ID links them. The session log is
  primary. OTel is secondary, used only for requests it can link.

## Freshness [F]

Each anchor source has two ages. **Display** is how long a reading is shown
as current before being marked stale. **Control** is the oldest it may be
and still count for a nudge (D8).

| Source | Display | Control |
|---|---|---|
| `claude.statusline` | 15 min | 15 min |
| `codex.rollout` `rate_limits` | 15 min | 15 min |
| `omp.usage_cache` | 15 min | 10 min |
| `claude.cached_utilization` | 60 min | 15 min |
| `omp.usage_history` | 2 h | never |
| screen state | 30 s | 30 s, and re-read immediately before acting |

Older data is still shown, with its age and a stale mark. It's never
silently treated as current.

## The Claude status line tap [F: shape; installing it is an open decision]

- **What it is:** `usage-watch statusline-tap -- <the user's own command>`.
- **What it does, in order:**
  1. reads stdin;
  2. writes `rate_limits`, `session_id` and a timestamp atomically to
     `$XDG_STATE_HOME/usage-watch/claude-statusline/<session>.json`;
  3. runs the user's own command with the same stdin;
  4. prints its output unchanged.
- **If it fails**, the user's command still runs. The status line must
  never break.
- **Installing it:** `usage-watch init --claude-statusline` shows the exact
  `settings.json` change, applies it only on confirmation, keeps the
  original, and `--undo` restores it.

## The first dependency [L]

- **What:** the OTLP receiver's protobuf path, as an **optional extra**,
  `usage-watch[otlp]`, using `opentelemetry-proto` and `protobuf`. The core
  stays standard library only, and so does the receiver's JSON path.
- **Why not hand-write a decoder:** it would be a parser for a spec we
  don't own, and ours to keep correct across OTLP versions.
- **The recorded justification:** it adds a whole source (omp's OTel), with
  no reasonable standard-library way to decode protobuf, and it's kept out
  of installs that don't need it.
