# R4: What is the current state of the OTel GenAI semantic conventions?

Date: 2026-09-30. Sources are linked inline. Everything was read from
`main` of the new conventions repo on this date.

## Where they live, and how stable they are

- **They moved.** Since core semconv v1.42.0, the GenAI conventions live in
  a separate repo,
  [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai).
  The old pages under opentelemetry.io/docs/specs/semconv/gen-ai/ now only
  say they moved, and core v1.44.0 marks every `gen_ai.*` attribute
  "Deprecated: moved".
- **Version:** the new repo's
  [`model/manifest.yaml`](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/model/manifest.yaml)
  gives schema `gen-ai-dev/1.42.0-dev`, `stability: development`, and
  depends on core v1.44.0. The repo has **no releases** (created
  2026-05-05; its CHANGELOG has only "Unreleased").
- **Every document and attribute is Development**, so all of it may still
  change.
- **Opt-in:** core v1.37.0 defined
  `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`.
  Instrumentations without it keep emitting v1.36-era names. Whether the
  new repo still honours this variable is **unconfirmed**.

## Attributes (current registry)

[Registry](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/registry/attributes/gen-ai.md):

| Concept | Attribute |
|---|---|
| Provider | `gen_ai.provider.name` (was `gen_ai.system` before v1.37.0) |
| Model | `gen_ai.request.model`, `gen_ai.response.model` |
| Operation | `gen_ai.operation.name` (`chat`, `execute_tool`, …) |
| Input tokens | `gen_ai.usage.input_tokens` (**includes** cached tokens) |
| Output tokens | `gen_ai.usage.output_tokens` |
| Cache read / write | `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` (core v1.44.0 had `cache_creation`) |
| Reasoning tokens | `gen_ai.usage.reasoning.output_tokens` |
| Conversation | `gen_ai.conversation.id` |
| Agent | `gen_ai.agent.id`, `.name`, `.version`, `.description` |
| Tool | `gen_ai.tool.name`, `.type`, `.call.id` |
| Workflow | `gen_ai.workflow.name` (must be low cardinality) |

`prompt_tokens` and `completion_tokens` became `input_tokens` and
`output_tokens` back in v1.27.0.

## Metrics: a breaking change

`gen_ai.client.token.usage` (a histogram split by `gen_ai.token.type`) is
**not in the new repo**, and core v1.44.0 deprecates `gen_ai.token.type`.
[Its replacements](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-token-metrics.md)
are:

- **Counters, one per token category**, unit `{token}`, each with a
  required `gen_ai.token.modality`:
  `gen_ai.client.inference.usage.input_tokens`, `.output_tokens`,
  `.cache_read.input_tokens`, `.cache_write.input_tokens`,
  `.reasoning.output_tokens`. The docs call them the proxy for cost
  approximation.
- **Histograms** `gen_ai.client.inference.operation.input_tokens` and
  `.output_tokens`, for percentiles only. The docs say they "should not be
  used for total usage or cost calculations".
- **Other metrics:** `gen_ai.client.operation.duration` (seconds),
  `…time_to_first_chunk`, `…time_per_output_chunk`, and
  `gen_ai.invoke_workflow.duration`.

## Spans and events

- **Spans:**
  - Inference: `{operation} {model}`, CLIENT.
  - `create_agent {name}`: CLIENT.
  - `invoke_agent {name}`: CLIENT, or INTERNAL for in-process agents.
  - `invoke_workflow {name}`, `plan`, `execute_tool {tool}`: INTERNAL.
- **Events:** `gen_ai.client.inference.operation.details` (opt-in) and
  `gen_ai.evaluation.result`.
- **Message content** goes in opt-in attributes (`gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.system_instructions`).

## Cost and cardinality

- **There is no standard cost attribute or metric.** The token counters are
  the stated proxy.
- Span names exclude high-cardinality IDs, content is opt-in, and the token
  histograms deliberately leave out modality.

## Consequences for the plan

- **What the harnesses emit today doesn't match the current conventions.**
  omp emits the older `gen_ai.client.token.usage` with `gen_ai.token.type`
  ([R1](R1-native-otel.md)). Codex's spans use `cache_write`/`cache_read`
  names. Claude Code uses its own `claude_code.*` names. The receiver (C3)
  must therefore read **each harness's actual names** and map them into
  usage-watch's own model. That is exactly why the model sits in the
  middle (plan principles).
- **Export (E1)** should follow the convention in force when it's built,
  pinned to a schema URL, and not before: nothing is stable yet. D4 decides
  whether to also emit the older histogram, since existing backends
  understand it.
- **Cost:** usage-watch names it itself (for example
  `usage_watch.cost.usd`, with `usage_watch.cost.basis` =
  `actual|estimated`). No standard name exists to follow.
- **Input tokens include cached tokens** in the convention. Harnesses that
  report cached tokens separately must be normalised to one rule before
  adding anything up, or cached tokens get counted twice (D1).

## Open

1. Whether `OTEL_SEMCONV_STABILITY_OPT_IN` still applies in the new repo.
2. When `gen_ai.client.token.usage` was removed, and whether backends
   (Grafana, Honeycomb and others) already support the new counters.
