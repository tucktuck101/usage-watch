# D4: Naming, mapping and cardinality

Status: draft, revised after review (2026-10-01). Rests on
[R4](../research/R4-otel-genai-conventions.md) and
[R1's live capture](../research/R1-native-otel.md#live-capture-2026-10-01).
Scope markers as in [D1](D1-model.md).

## Internal names are ours [F]

The store uses D1's names. OTel names appear only at the receiver's inbound
mapping (C3) and at export (E1). A convention change touches those two
mappings and nothing else.

## Inbound mapping: each source's actual fields [F]

The receiver and collectors keep **only** the fields below. Everything else
is dropped (D5).

| D1 field | Claude transcript | Claude OTel | Codex rollout | Codex OTel | omp session | omp OTel |
|---|---|---|---|---|---|---|
| `uncached_input_tokens` | `input_tokens` | `input_tokens` / `type=input` | `input_tokens − cached_input_tokens` (assumes input includes cached, per OpenAI convention: **verify**) | `non_cached_input` | `input` | `type=input` (whether it includes cache reads: **verify per version**) |
| `cache_read_input_tokens` | `cache_read_input_tokens` | `cache_read_tokens` / `cacheRead` | `cached_input_tokens` | `cached_input` | `cacheRead` | `cache_read_input` |
| `cache_write_input_tokens` | `cache_creation_input_tokens` | `cache_creation_tokens` / `cacheCreation` | `cache_write_input_tokens` | `cache_write_input` | `cacheWrite` | `cache_write_input` |
| `output_tokens` | `output_tokens` | `output_tokens` | `output_tokens` | `output` | `output` | `output` |
| `reasoning_output_tokens` | `output_tokens_details.thinking_tokens` | none | `reasoning_output_tokens` | `reasoning_output` | `reasoningTokens` | `reasoning_output` |
| `session_id` | `sessionId` | `session.id` | `session_meta.payload.id` | `conversation.id` | session `id` | `gen_ai.conversation.id` |
| request key | `message.id` + `requestId` | `request_id` | turn and total position | turn ID | entry ID | `gen_ai.response.id` |
| harness cost | none | `cost_usd_micros` | none | `codex.turn.cost_microusd` | `cost.total` | `…cost.estimated_usd` |
| billing route | unknown | unknown | unknown | `auth_mode` | unknown | unknown |

- Every cell is checked against a recorded, redacted sample before it's
  trusted. The cells marked "verify" are open.
- Whether each source's reasoning figure is a subset of its output, or
  separate, is checked per source (D1's subset rule).

## Outbound: export [L, with the defaults fixed now]

- Follow the GenAI conventions in force when export is built, pinned to a
  schema URL. Today that means the counters
  `gen_ai.client.inference.usage.*` with `input` **including** cache
  reads and writes, which is D1's `total_input_tokens`.
- The older `gen_ai.client.token.usage` histogram is available as an
  option, off by default.
- usage-watch's own names where no convention exists:
  `usage_watch.capacity.used_ratio`, `usage_watch.cost.usd_micros` with
  `usage_watch.cost.basis`, and `usage_watch.*` attributes.

## Cardinality [F: the rule; L: export]

**Default exported metric dimensions** are only the ones that are
genuinely low-cardinality: provider, model, harness, role, window, token
category, cost basis.

- **Project and account on exported metrics are opt-in**, from a fixed
  list the user names in config. There is never a moving "top N". Values
  not on the list are **not exported on metrics at all**, rather than
  folded into a changing `other`. They remain in the local store, and on
  events and traces where export allows (D5).
- **Never on metrics:** session, pane, request key, branch, worktree, task,
  issue, PR, commit, cwd.
- The local store has no cardinality limit, since views query it
  directly.
