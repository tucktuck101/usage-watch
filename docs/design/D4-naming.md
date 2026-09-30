# D4: Naming and cardinality

Status: draft. Rests on [R4](../research/R4-otel-genai-conventions.md).

## Internal names are ours

The store (D1) uses usage-watch's own field names. OTel names appear only in
two places: the receiver (C3), which maps each harness's actual names
**in**, and export (E1), which maps **out**. A convention change touches
those two mappings and nothing else.

## Inbound: each harness's actual names

| usage-watch field | Claude Code | Codex CLI | omp |
|---|---|---|---|
| `input_tokens` (uncached) | `input_tokens` / `type=input` | `non_cached_input_tokens` | `type=input` minus `cache_read_input` if included (test per version) |
| `cache_read_tokens` | `cache_read_tokens` / `cacheRead` | `cached_input` | `cache_read_input` |
| `cache_write_tokens` | `cache_creation_tokens` / `cacheCreation` | `cache_write_input` | `cache_write_input` |
| `output_tokens` | `output_tokens` | `output` | `output` |
| `reasoning_tokens` | not separate on OTel | `reasoning_output` | `reasoning_output` |
| `session_id` | `session.id` | `conversation.id` | `gen_ai.conversation.id` |
| cost, harness estimate | `cost_usd_micros` | `codex.turn.cost_microusd` (when emitted) | `…cost.estimated_usd` |

Each row gets a test built from the R1 live capture, redacted.

## Outbound: export

- **Follow the GenAI conventions in force when export is built**, pinned to
  a schema URL. Today that means the per-category counters
  `gen_ai.client.inference.usage.{input,output,cache_read.input,cache_write.input,reasoning.output}_tokens`,
  where `input` **includes** cached tokens.
- **Decision:** also emit the older `gen_ai.client.token.usage` histogram
  with `gen_ai.token.type`, off by default, for backends that don't yet
  understand the new counters.
- **usage-watch's own names**, where no convention exists:
  - `usage_watch.capacity.used_ratio` (gauge, 0–1), with
    `usage_watch.window`;
  - `usage_watch.cost.usd` with `usage_watch.cost.basis`
    (`actual|estimated|harness_estimate`);
  - attributes `usage_watch.project`, `.repository`, `.harness`, `.role`,
    `.account` (hashed), `.worktree`, `.branch`, `.task`, `.issue`.

## Cardinality

**Allowed on metrics** (bounded): `gen_ai.provider.name`,
`gen_ai.request.model`, token type, `usage_watch.harness`, `.project`,
`.role`, `.account`, `.window`, `.cost.basis`.

**Never on metrics** (unbounded), only on traces and events:
`session_id`, `pane`, `request_key`, `branch`, `worktree`, `task`,
`issue`, `pr`, commit, `cwd`.

Projects and accounts are bounded in practice, but not guaranteed to be, so
export caps each at a configurable number of values (default 50) and folds
the rest into `other`.
