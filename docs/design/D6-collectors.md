# D6: Collectors and reconciliation

Status: draft, revised after third review (2026-10-01). Rests on
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
- Items go through one **write path**, in the collector runtime:
  1. check them against the source's field allowlist (D5);
  2. write the observations and other records;
  3. hand new observations to the **reconciler**, which updates canonical
     usage events;
  4. update attribution evidence and effective attributions (D2);
  5. update `collector_status`.
- **Incremental observations.** A new source record whose
  `(source, stream_key, source_request_key)` already exists updates that
  observation, by the source's counting rule (below); it is not a new
  observation. If it is a primary observation, its canonical usage event
  is then updated in place. This applies to any source whose
  representation of a request evolves, e.g. Claude's later, more complete
  streaming snapshot of the same request.
- A source that fails is reported in `collector_status` and retried. It
  never stops the others.
- **No source makes an outbound network call.** The OTLP receiver listens
  on 127.0.0.1 only.

## Liveness [F]

- The collector runtime holds the exclusive lock on `run.lock` for as long
  as it runs, and writes its `runtime` heartbeat row every **10 s**.
- Whether a collector is running comes from the lock and the heartbeat, as
  D3 sets out. `collector_status` doesn't answer that question.
- `collector_status` is about **data age**: per source, when it last
  collected, when it last produced something new, its last error, and its
  orphan count (below).

## Sources [F for the proof-of-concept set; the rest X or L]

| Source | Contract | Reads | Gives | Confidence | Counting rule |
|---|---|---|---|---|---|
| `claude.transcript` [F] | pull, tail | `~/.claude/projects/**/*.jsonl` | observations, sessions, limit events | authoritative | **Primary** for Claude Code. Within the source, collapse on `(message.id, requestId)`, keeping the **final** snapshot (largest `output_tokens`): a later, more complete snapshot advances the existing observation (incremental observations, above). Skip `<synthetic>`. Subagent files are their own sessions |
| `codex.rollout` [F] | pull, tail | `~/.codex/sessions/**/*.jsonl` | observations, sessions, **anchors** (`rate_limits`) | authoritative | **Primary** for Codex. Per session, the change in `total_token_usage`. Skip unchanged repeats. A decrease starts a new baseline. The model comes from the preceding `turn_context` |
| `omp.session` [F] | pull, tail | `~/.omp/agent/sessions/**/*.jsonl` | observations (with `auxiliary`), harness cost, sessions, `credentialId` | authoritative | **Primary** for omp. Collapse on the entry `id` |
| `omp.usage_cache` [F] | pull, 60 s | `agent.db` `cache`, `usage_cache:report:*` values only | anchors for every omp login | observed | Ignore entries by age, not by status. Hash identity on read |
| `claude.statusline` [F] | pull, 10 s | the tap's snapshot files | anchors for the Claude Code account | authoritative | Newest per window |
| `claude.cached_utilization` [F] | pull, 5 min | `~/.claude.json` `.cachedUsageUtilization` | anchors, including per-model weekly | observed | Only when `fetchedAtMs` is newer than the newest anchor |
| `omp.usage_history` [X] | pull, 15 min | `agent.db` `usage_history` | anchors, backfill | observed | Never used for nudges (D8) |
| `otlp.receiver` [L] | push | 127.0.0.1, OTLP/HTTP json and protobuf | observations, cost, sessions, identity | authoritative | **Secondary** for counting, for every harness. D4 mapping only |
| `screen` [F, exists] | pull, 5 s / 300 s | tmux panes | state samples, limit events, reset hints | inferred | Today's adapters |
| `topology` [F, exists] | pull, with `screen` | tmux, ps, git, workmux | live facts for attribution | observed | Today's scan |

**Watermarks** for tail sources are `(path, inode, byte offset)` per file.
A new inode or a shorter file starts over from 0, which is safe because
observation identity is unique. The first run backfills everything present,
with `historical` attribution evidence only (D2).

### Observation keys [F]

An observation's identity is `(source, stream_key, source_request_key)`.
`source_request_key` is scoped to its stream. **A source with no stable
per-stream key doesn't emit observations** until one is confirmed.

**`stream_key` is fixed when the observation is first stored, and never
changes.**
- **Pull sources:** the `session_key`. The session is normally known from
  the file header before any record (the Claude path, Codex
  `session_meta`, the omp session header), so the fallback, a keyed hash
  of the source file path (D5), is rare.
- **Push sources (OTLP):** the `session_key` when known, else a keyed hash
  of the trace ID (D5).

| Source | `stream_key` | `source_request_key` | Can emit? |
|---|---|---|---|
| `claude.transcript` | `session_key` (`claude:<sessionId>`); a subagent file is its own session | `message.id` + `requestId` (R2, verified; no pair spans two files) | yes |
| `codex.rollout` | `session_key` (`codex:<session id>`) | the `token_count` event's timestamp + its `total_token_usage.total_tokens`. R2 found forked files don't replay token events | yes |
| `omp.session` | `session_key` (`omp:<session uuid>`); a nested subagent file is its own session | the entry's `id` field. R2 found one duplicate entry; uniqueness per session: confirm from a recorded sample before C2 | yes, once confirmed (gates C2, not F1 or F2) |
| `otlp.receiver`, Claude Code | `session_key` from `session.id` | `request_id` on `claude_code.api_request` (R1, verified) | yes |
| `otlp.receiver`, Codex | `session_key` from `conversation.id` (logs and spans; not on metrics) | `turn.id` | yes (never links: see below) |
| `otlp.receiver`, omp | `session_key` from `gen_ai.conversation.id` (spans; logs join through their trace) | `gen_ai.response.id` (seen in R1's live capture) | yes |

The anchor, snapshot and screen sources don't emit usage observations, so
they need no observation key.

## Reconciliation [F]

**One primary source per harness.** For each harness, its session log is
primary for counting: `claude.transcript`, `codex.rollout`, `omp.session`.
OTel is a preferred telemetry source for other facts, but **secondary for
counting**. The source's own key prevents double counting within it.

**Linking is only through `provider_request_key`** (D4: a keyed hash, D5
namespace `request:<provider>`, of the provider's request ID). A secondary
observation links to a primary one only when both carry the same non-null
`provider_request_key`. Observation identity stays
`(source, stream_key, source_request_key)`.

**Event identity is stable.** One primary observation permanently
corresponds to one canonical usage event. The accounting link is
`usage_events.accounting_observation_id` (NOT NULL, UNIQUE); it is not a
row in `event_observations`. Reconciliation updates events in place and
never rebuilds or replaces a `usage_id`.

| Observation | What happens | Counts? |
|---|---|---|
| **Primary** | Creates its canonical usage event (`usage_id`) the first time it is seen, with `accounting_observation_id` pointing at it; later versions of it update that event in place | yes, exactly once per distinct primary key |
| **Linked secondary** (same `provider_request_key` as a primary observation, e.g. Claude transcript `requestId` = Claude OTel `request_id`) | An `event_observations` row with role `supporting`. Where its values differ, the event records `disagreement` | no |
| **Unlinked secondary** | Stored as evidence, with no `event_observations` row. Links later if a primary observation with the same `provider_request_key` arrives | **never** |

- **`link_state` is derived, not stored.** An observation is `primary` if
  it is some event's `accounting_observation_id`, `linked` if it has an
  `event_observations` row, and `orphan` otherwise. It is answered by
  query.
- **`event_observations.role`** is `supporting` or `metadata` only. A
  secondary observation supports at most one event.
- **Token accounting is atomic.** The event's token fields come from its
  single accounting observation, as one unit. They're never mixed with
  another observation's, and never taken from a `supporting` or `metadata`
  observation.
- **Metadata from a linked observation.** A request-level metadata field
  the accounting observation lacks may be taken from a linked observation,
  when a source has one. The event records which one, with role
  `metadata` and the field name.
- **Codex OTel never links by request:** the rollout has no request ID
  (R2), so neither side has a `provider_request_key` and every Codex OTel
  observation is an orphan. Its `auth_mode` is
  used as **session-level** `billing_route` evidence instead, linked by
  `conversation.id` = `session_id` (D2), and usage events inherit it.
- **Disagreement:** the largest per-field difference between the
  accounting observation and **all** its linked `supporting` observations. `doctor` reports how many
  disagreements appear per source pair, which is an early sign of parser
  drift.
- **Orphans:** the number of unlinked secondary observations per source is
  shown in `doctor` and in `collector_status`, counted by query.
- **No readable primary source, no count.** A harness whose primary source
  is unavailable (e.g. OTel arrives but no session log is readable) is
  **not counted**. It's reported as a **coverage gap**. A secondary source
  is never promoted automatically. Promoting one to primary is an explicit
  config choice, per harness.
- **Versioning:** reconciliation rules have a version, recorded per event
  as `usage_events.reconciled_version`. Events below the current version
  are reprocessed in place from their stored observations; their
  `usage_id` is kept.
- **What reconciliation never does:** merge observations it can't prove
  are the same request.

## Freshness [F]

Each anchor source has two ages. **Display** is how long a reading is shown
as current before being marked stale. **Control** is the oldest it may be
and still count for a nudge (D8, which is `[F5]`: settle before F5).

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

## Open questions

1. **omp link.** omp session entries carry `responseId` (608 null, R2),
   and omp OTel spans carry `gen_ai.response.id` (R1). Whether the two
   hold the same value, so the spans can link, is to confirm from a
   recorded sample. Until then neither sets `provider_request_key` (D4),
   and every omp OTel observation is an orphan.
