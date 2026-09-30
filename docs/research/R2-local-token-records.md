# R2: Where does each harness record per-request tokens locally?

Date: 2026-09-30. Versions: Claude Code 2.1.285, Codex CLI 0.154.0, omp
18.4.0. Method: read-only scans that printed key names, types and aggregate
counts, never content. SQLite was opened read-only.

## Summary

| | Claude Code | Codex CLI | omp |
|---|---|---|---|
| Where | `~/.claude/projects/<slug>/<sessionId>.jsonl`; subagents in `<sessionId>/subagents/*.jsonl` | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `~/.omp/agent/sessions/<cwd-slug>/<ts>_<uuid>.jsonl`; subagents nested |
| Record | `type:"assistant"` lines, `message.usage` | `event_msg` with `payload.type:"token_count"` | `type:"message"`, `message.role:"assistant"`, `message.usage` |
| Input / output | `input_tokens`, `output_tokens` | `input_tokens`, `output_tokens` | `input`, `output` |
| Cache | `cache_read_input_tokens`, `cache_creation_input_tokens` (split into 5 m and 1 h) | `cached_input_tokens`, `cache_write_input_tokens` | `cacheRead`, `cacheWrite` (plus 1 h split) |
| Reasoning | `output_tokens_details.thinking_tokens` | `reasoning_output_tokens` | `reasoningTokens` (some records) |
| Cost | none | none | **yes**: `cost{input, output, cacheRead, cacheWrite, total}` USD |
| Model | `message.model` | not on the event; from the preceding `turn_context.payload.model` | `message.model`, `message.provider` |
| Request ID | `message.id` + `requestId` | none | `responseId` (608 null) |
| Per request or running total? | per request | **both**: `last_token_usage` and a running `total_token_usage` | per request |
| **Trap** | **Duplicates**: one line per content block, each repeating the usage | **Repeats**: re-emitted totals | negligible |
| History on this machine | from 2026-09-01 (about 30 days, likely retention) | from 2025-11-14 | from 2026-08-05 |

## Claude Code

- **Duplicates (verified).** 11,042 assistant lines held only 4,732 unique
  `(message.id, requestId)` pairs. In 735 pairs the copies differ, and
  `output_tokens` only ever grows (e.g. 2, then 12,402) as streaming
  completes.
  - **Rule:** deduplicate on `(message.id, requestId)`, and keep the largest `output_tokens`.
  - No pair spans two files.
  - Exclude the null-id records, whose model is `<synthetic>`.
- **Retention.** The oldest record is 2026-09-01, with no
  `cleanupPeriodDays` set. That suggests a default cleanup of about 30
  days (inferred). usage-watch must ingest regularly, or history is lost.
- Subagent transcripts carry `isSidechain:true` and `agentId`, and their
  own usage. They are separate requests to count, not duplicates.

## Codex CLI

- **Running totals (verified).** Of 38,618 `token_count` events:
  - 7,454 repeat the previous total unchanged;
  - 97 have `info:null`;
  - 4 show the total going down, probably at a compaction or reset.

  Summing `last_token_usage` blindly double-counts.
  - **Rule:** per session, take the change in `total_token_usage`, skip events where it didn't change, and treat a decrease as a new baseline.
- Forked rollouts (62 files with more than one `session_meta`, and 32
  session IDs in more than one file) did **not** replay token events.
- `~/.codex/state_5.sqlite`, table `threads`, has `rollout_path`, `cwd`,
  `git_branch`, `model` and `tokens_used` per thread. It's a cheap
  per-thread summary. It is WAL-mode, so open it with `immutable=1`.
- A `payload.rate_limits` object also rides on `token_count` events. That
  is a capacity reading at no cost (see [R3](R3-capacity-sources.md)).

## omp

- **`agent.db` holds no per-request tokens** (verified):
  - `usage_history` holds rate-limit fractions;
  - `client_usage` and `usage_cost_history` are empty;
  - `model_perf`, `model_usage` and `command_usage` are aggregates.
- The session files hold 70,720 assistant records, all with usage, and
  **with cost**. Only one duplicate entry was found.
- Subagent sessions (1,064 nested files) carry their own usage, some with
  `parentSession`.

## Consequences for the plan

- **C2 is viable for all three**, with backfill: weeks of history already
  exist on disk.
- Each harness needs its own counting rule (above), and those rules must be
  tested against recorded samples. Summing naively overcounts Claude by
  about 2.3x and Codex by the repeats.
- **Cached tokens:** Claude and omp report input **excluding** cache reads.
  The OTel convention's `input_tokens` **includes** them
  ([R4](R4-otel-genai-conventions.md)). D1 must pick one rule and convert
  every source to it.
- **Cost:** omp's own figure becomes a `harness_estimate`
  ([R5](R5-pricing-sources.md)). Claude and Codex need pricing (K1).
- **Ingest continuously.** Claude's retention means an occasional backfill
  would lose data.

## Open

1. Is Claude's roughly 30-day retention the default cleanup?
2. For Codex's decreasing totals: is resetting the baseline right, or is
   usage lost at compaction?
3. Are omp's 608 records with a null `responseId` failed requests that
   still used tokens?
