# D1: The model

Status: draft. Rests on [R1](../research/R1-native-otel.md),
[R2](../research/R2-local-token-records.md),
[R3](../research/R3-capacity-sources.md),
[R9](../research/R9-credential-free-limits.md),
[R10](../research/R10-omp-extension-limits.md).

## Rules for every record

- **What was measured, who said so, and how sure we are.** Every record
  carries:
  - `observed_at`: when the fact was true;
  - `recorded_at`: when usage-watch stored it;
  - `source`: a dotted name for where it came from, such as
    `claude.transcript`, `claude.statusline`, `omp.usage_cache`,
    `codex.rollout` or `omp.otel`;
  - `confidence`: one of
    - `authoritative`: the harness or provider stated it;
    - `observed`: read from a copy another tool kept;
    - `estimated`: computed by usage-watch from other records;
    - `inferred`: deduced, such as a state read from a screen.
- **Capacity, usage and cost are separate record types.** They are never
  added together or converted into each other silently.
- **Units:**
  - times are UTC milliseconds as integers;
  - money is USD in micro-units as integers (`cost_usd_micros`, as Claude
    Code's own OTel already uses);
  - percentages are 0–100 as floats;
  - token counts are integers.
- **Identities are hashed keys** (D5, D7). Raw emails and account IDs are
  never stored.
- **Absent is not zero.** An unknown value is null, never 0.

## Record types

### Capacity sample

One reading of one quota window for one account.

| Field | Meaning |
|---|---|
| `account` | hashed account key (D7) |
| `window` | normalised window: `session` (5 h), `weekly` (7 d), `weekly:<model>` (e.g. `weekly:fable`), or `other:<name>` |
| `window_seconds` | the window's length, when known |
| `used_pct` | 0–100 |
| `resets_at` | when the window resets |
| `status` | `ok`, `warning`, `exhausted` or `unknown` |
| `kind` | **`anchor`** (a real reading from a harness) or **`estimate`** (computed by V4 from the usage stream) |

Anchors and estimates are both stored, and never overwrite each other. The
"current" value of a pool is the newest anchor, or the newest estimate if
it's newer and the view asks for estimates.

Codex's `primary`/`secondary` windows map to `session`/`weekly` by
`window_seconds` (18000 and 604800), not by name.

### Usage event

One model request.

| Field | Meaning |
|---|---|
| `request_key` | the deduplication key, per source (D6) |
| `harness`, `provider`, `model` | e.g. `claude`, `anthropic`, `claude-opus-5-5` |
| `account` | hashed, when known (D2) |
| `session_id` | the harness's own session or conversation ID |
| `parent_session_id` | set for subagent sessions |
| `input_tokens` | **uncached** input only |
| `cache_read_tokens`, `cache_write_tokens` | cache components, kept separate |
| `output_tokens` | output, including reasoning where the harness includes it |
| `reasoning_tokens` | reasoning, when reported separately (a subset of output, never added again) |
| `project`, `repository`, `branch`, `worktree`, `cwd`, `role`, `pane` | enrichment (D2); `pane` only while live |
| `auxiliary` | true for a harness's own side calls, such as omp's "judgment" model; excluded from totals by default |

**The cached-token rule:**
- Sources disagree about whether "input" includes cached tokens.
  - Claude and omp report input *excluding* cache reads.
  - The OTel convention's `input_tokens` *includes* them.
  - Codex reports `input_tokens` alongside `cached_input_tokens` and
    `non_cached_input_tokens`.
- usage-watch always stores the components separately, with
  `input_tokens` meaning uncached.
- The convention's total is computed at export (D4).
- Each collector's mapping is tested against a recorded sample.

### Cost event

| Field | Meaning |
|---|---|
| `usage_ref` or `session_id` | what the cost is for |
| `cost_usd_micros` | the amount |
| `basis` | `actual` (billed), `estimated` (tokens × a price table), or `harness_estimate` (the harness's own figure) |
| `price_source` | for `estimated`: the price table's version and entry, so old estimates are never silently repriced |

Costs of different bases are never summed together. A view picks one basis
and says which.

### Agent-state sample

| Field | Meaning |
|---|---|
| `pane`, `harness`, `session_id` | where |
| `state` | `busy`, `idle`, `stalled`, `resuming`, `typing` or `unknown` |
| `model`, `reset_hint` | as read from the screen |

Always `inferred`. Kept short-term only (D3).

### Context event

The "why" from harness hooks and `usage-watch task` (plan phase 8).

| Field | Meaning |
|---|---|
| `kind` | `task_start`, `task_end`, `role`, `handoff`, `retry` |
| `session_id`, `pane` | where |
| `task`, `issue`, `pr`, `role`, `work_kind` | the context; free text, set by the user or their hooks |

Defined now so later phases add rows, not a new model.

## Entities

- **Account:** the registry (D7).
- **Session:** `session_id`, `harness`, `account`, `cwd`, `project`,
  `branch`, `started_at`, `last_seen_at`, `parent_session_id`, and `pane`
  while live.
- **Pane:** not stored as an entity. It's a live tmux fact recorded on
  sessions and events.

## Not in the model

Prompt and response content, tool inputs and outputs, credentials of any
kind (D5).
