# D6: Collectors

Status: draft. Rests on [R1](../research/R1-native-otel.md),
[R2](../research/R2-local-token-records.md),
[R9](../research/R9-credential-free-limits.md) and
[R10](../research/R10-omp-extension-limits.md).

## The interface

A collector turns one source into D1 records. It has:
- a `name`, the `source` value its records carry;
- a `kind`: `tail` (reads files that grow), `poll` (reads a snapshot on a
  schedule) or `push` (receives data);
- a default `confidence`;
- one method, `collect(watermark) -> (records, new_watermark)`, which never
  blocks for long and never touches the network.

The runtime (F2) owns scheduling, writing, deduplication and watermarks. A
collector that raises an error is reported by `doctor` and retried on its
next turn. It never stops the others.

**No collector makes an outbound network call.** Under the no-credentials
principle, every source is local. The OTLP receiver listens on 127.0.0.1
only.

## The collectors

| Collector | Kind | Source | Gives | Confidence | Rule that matters |
|---|---|---|---|---|---|
| `claude.transcript` | tail | `~/.claude/projects/**/*.jsonl` | usage, sessions, limit hit and reset events | authoritative | **Deduplicate on `(message.id, requestId)`, keeping the largest `output_tokens`.** Skip `<synthetic>`. Subagent files are separate sessions |
| `codex.rollout` | tail | `~/.codex/sessions/**/*.jsonl` | usage, sessions, **capacity** (`rate_limits`) | authoritative | **Usage is the per-session change in `total_token_usage`.** Skip unchanged repeats, and treat a decrease as a new baseline. The model comes from the preceding `turn_context` |
| `omp.session` | tail | `~/.omp/agent/sessions/**/*.jsonl` | usage, harness cost, sessions, `credentialId` | authoritative | One duplicate seen in 70k. Deduplicate on the entry ID. Judgment and other auxiliary calls are marked `auxiliary` |
| `omp.usage_cache` | poll, 60 s | `agent.db` `cache` table, `usage_cache:report:*` values only | capacity for every omp login | observed | Hash identity metadata on read. Ignore entries by age, not by status |
| `omp.usage_history` | poll, 15 min | `agent.db` `usage_history` | capacity history, backfill | observed | Fallback only |
| `claude.statusline` | poll, 10 s | the tap's snapshot file (below) | capacity for the Claude Code account | authoritative | Newest reading per window wins |
| `claude.cached_utilization` | poll, 5 min | `~/.claude.json` `.cachedUsageUtilization` | capacity, including per-model weekly windows | observed | Used only when its `fetchedAtMs` is newer than the newest anchor |
| `otlp.receiver` | push | 127.0.0.1, OTLP over HTTP (json and protobuf) | usage, cost, sessions, account identity | authoritative | Map names per D4. Drop content (D5) |
| `screen` | poll, 5 s (dashboard) / 300 s (run) | tmux panes (today's adapters) | agent states, reset hints | inferred | Exists today; becomes a collector in F3 |
| `topology` | poll, with `screen` | tmux, ps, git, workmux | panes, projects, roles, lanes | observed | Exists today; enriches the rest |

**When a record arrives from two routes** (e.g. the Claude transcript and
Claude's OTel for the same request), deduplication uses the provider's
request ID where both carry it (Claude `request_id`). Otherwise the
higher-confidence source wins. The duplicate is dropped, and the drop is
counted.

**Watermarks:**
- **Tail collectors:** `(path, inode, byte offset)` per file. A new inode
  or a shorter file starts again from 0, which is safe thanks to the unique
  keys.
- **Backfill:** a first run reads everything present.

## The Claude status line tap

- **What it is:** `usage-watch statusline-tap -- <the user's own command>`.
- **What it does, in order:**
  1. reads stdin;
  2. writes `rate_limits`, `session_id` and a timestamp atomically to
     `$XDG_STATE_HOME/usage-watch/claude-statusline/<session>.json`;
  3. runs the user's own command with the same stdin;
  4. prints its output unchanged.
- **If it fails**, it still runs the user's command. The status line must
  never break.
- **How it's installed:** `usage-watch init --claude-statusline` shows the
  exact `settings.json` change and applies it only on confirmation, keeping
  the original to restore. `--undo` restores it.
- **Decision (open in the plan):** whether `init` offers this at all.
  Recommended: yes, explicitly and reversibly.

## Disagreement

Two capacity readings for the same account and window are both stored.
"Current" is the newest **anchor**. Where two anchors are within a minute of
each other and differ by more than 5 points, `doctor` reports it, since one
source is misread or stale. Usage totals never mix bases (D1).

## The first dependency

- **The need:** omp sends OTLP only as protobuf. Decoding it needs either
  the `protobuf` and `opentelemetry-proto` packages, or a hand-written
  decoder.
- **Decision:** make the receiver an **optional extra**,
  `usage-watch[otlp]`, depending on `opentelemetry-proto` and `protobuf`.
  The core stays standard library only, and so does the receiver's JSON
  path.
- **Why not hand-write it:** a small wire-format decoder is possible, but
  it's a parser for a spec we don't own. It would be ours to keep correct
  across OTLP versions, which is the maintenance the plan asks us to avoid.
- **The recorded justification** (plan principle): it adds a whole source
  (omp's OTel), and there is no reasonable standard-library way to decode
  protobuf. The extra keeps it out of every install that doesn't need it.

## Order of building

The proof of concept needs `claude.transcript`, `codex.rollout`,
`omp.session`, `omp.usage_cache` and `claude.statusline`, plus the existing
`screen` and `topology`. The OTLP receiver comes after (C3), because
session logs already give usage without it.
