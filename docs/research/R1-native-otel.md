# R1: What native OTel does each harness emit, and how is it switched on?

Date: 2026-09-30. Versions: Claude Code 2.1.285, Codex CLI 0.154.0, omp
18.4.0. Evidence: official docs, strings in each installed binary, and
key-name-only greps of local config. **Verified** means seen in docs or
the binary. **Inferred** means reasoned from code or naming, not observed.

## Summary

| | Claude Code | Codex CLI | omp |
|---|---|---|---|
| Signals | metrics, logs/events; traces in beta | log events; metrics and traces in the binary | traces, logs, metrics |
| Switch | `CLAUDE_CODE_ENABLE_TELEMETRY=1` plus standard `OTEL_*` variables | `[otel]` table in `~/.codex/config.toml` | Standard `OTEL_EXPORTER_OTLP_*ENDPOINT` variables; config kill switch `telemetry.otlpExportEnabled` |
| Protocols | OTLP gRPC, http/json, http/protobuf; Prometheus; console | `otlp-http` (binary or json), `otlp-grpc` | OTLP **http/protobuf only** |
| Default | off | off | effectively off (needs an endpoint) |
| On this machine | not configured | not configured | not configured |
| Token counts | yes, including cache | yes, including cached and reasoning | yes, GenAI convention names |
| Cost | `claude_code.cost.usage` (USD) | `codex.turn.cost_microusd` (binary only) | `omp.agent.chat.cost.estimated_usd` |
| Session ID | `session.id` (on by default) | `conversation.id` | `session.id` / `gen_ai.conversation.id` on spans only |
| cwd or project | none documented | none documented | none found |

## Claude Code

- **Switch** (verified; [monitoring docs](https://code.claude.com/docs/en/monitoring-usage), and every name is present in the binary):
  - `CLAUDE_CODE_ENABLE_TELEMETRY=1` turns telemetry on.
  - `OTEL_METRICS_EXPORTER` and `OTEL_LOGS_EXPORTER` take `otlp`, `prometheus`, `console` or `none`.
  - `OTEL_EXPORTER_OTLP_PROTOCOL` is `grpc`, `http/json` or `http/protobuf`.
  - `OTEL_EXPORTER_OTLP_ENDPOINT` sets where it sends.
  - Traces also need `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` and `OTEL_TRACES_EXPORTER=otlp`.
  - Default export intervals are 60 s for metrics and 5 s for logs.
- **Metrics** (verified):
  - `claude_code.token.usage`, with `type` = `input|output|cacheRead|cacheCreation`, plus `model`, `query_source`, `speed` and `effort`.
  - `claude_code.cost.usage` (USD).
  - Also `session.count`, `active_time.total`, `lines_of_code.count` and others.
- **Events** (verified): `claude_code.api_request`, carrying `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`,
  `cost_usd`, `model`, `duration_ms` and `request_id`. Every event carries
  `prompt.id` and `event.sequence`.
- **Standard attributes** (verified): `session.id` (default on),
  `user.account_uuid`, `organization.id`, `user.email` and
  `terminal.type`. `app.version` is off by default. The binary also reads
  `OTEL_METRICS_INCLUDE_{SESSION_ID,ACCOUNT_UUID,VERSION,ENTRYPOINT,REPOSITORY}`.
  The repository option is undocumented, so what it adds is **inferred**.

## Codex CLI

- **Switch** (verified; [advanced config docs](https://learn.chatgpt.com/docs/config-file/config-advanced)):
  - An `[otel]` table with `environment` (default "dev"), `exporter` (`none|otlp-http|otlp-grpc`) and `log_user_prompt` (default off).
  - `otlp-http` takes `endpoint`, `protocol` (`binary|json`) and `headers`.
  - The binary also has `trace_exporter`, `metrics_exporter`, `span_attributes`, a `tls` block, and a fourth exporter kind, `statsig`.
- **Log events** (verified):
  - Names: `codex.conversation_starts`, `codex.api_request`, `codex.sse_event`, `codex.user_prompt`, `codex.tool_decision`, `codex.tool_result`.
  - Token fields: `input_token_count`, `output_token_count`, `cached_token_count`, `reasoning_token_count`, `tool_token_count`.
  - Attributes: `conversation.id`, `model`, `auth_mode`, `user.account_id`, `user.email`, `terminal.type`, `app.version`.
- **Binary only, not in the docs** (verified present; what each one is exported on is unverified):
  - Metrics `codex.turn.token_usage.*` (input, cached input, cache write, non-cached input, output, reasoning output, total), `codex.turn.cost_microusd`, `codex.conversation.turn.count` and `codex.tool.call`.
  - Span attributes in GenAI-convention names (`gen_ai.usage.input_tokens`, `gen_ai.usage.cache_read.input_tokens` and others).

## omp

- **Build:** a Bun-compiled JavaScript binary using the OTel JS SDK, with
  service name `oh-my-pi` (verified; `telemetry-export-otlp.ts` in the
  binary).
- **Switch** (verified):
  - A signal exports when `OTEL_EXPORTER_OTLP_{TRACES,LOGS,METRICS}_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` is set.
  - `OTEL_*_EXPORTER`, if set, must include `otlp`.
  - `OTEL_SDK_DISABLED=true` stops everything.
  - Setting `telemetry.otlpExportEnabled` (default true) is a kill switch.
  - Only http/protobuf is supported. Any other protocol disables the signal, with a warning.
- **Metrics** (verified):
  - `gen_ai.client.token.usage` (histogram), with `gen_ai.token.type` = `input|output|total|cache_read_input|cache_write_input|reasoning_output`, plus `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.service_tier`, `omp.gen_ai.agent.id` and `omp.gen_ai.agent.name`.
  - `omp.agent.chat.cost.estimated_usd`, and run, step, call, duration and error counters.
- **Spans** (verified): `invoke_agent`, chat and `execute_tool`, with
  `gen_ai.usage.*`, `gen_ai.cost.*_usd` and `gen_ai.conversation.id`. Its
  metrics carry no session ID.

## Consequences for the plan

- **Native OTel is a real top source for all three**, so C3 (a local OTLP
  receiver) is worth building. It must accept **OTLP over HTTP with
  protobuf**: omp speaks nothing else. Claude Code and Codex can send JSON,
  and protobuf decoding without a dependency is work to weigh in D6. This
  is the plan's likeliest case for a justified dependency.
- **Everything is off by default**, so usage-watch would have to switch it
  on: through `usage-watch exec` (H3), per-harness settings, or setup
  instructions. Session logs (R2) stay the source for backfill and for
  sessions started without telemetry.
- **Pane attribution:** no harness sends cwd or project. Setting
  `OTEL_RESOURCE_ATTRIBUTES` for each harness launch (e.g. pane, project) is
  the likely route. All three binaries read the variable, but whether they
  pass it into exports is **unverified**.
- **Privacy:** the exports carry `user.email` and account and org IDs. The
  receiver must drop or hash them on arrival (D5). The prompt-bearing
  options (Claude's `OTEL_LOG_*`, Codex's `log_user_prompt`, omp's
  `gen_ai.response.text`) must stay off whenever usage-watch sets
  telemetry up.

## Open

1. Codex: is `statsig` its default metrics exporter, meaning metrics go to
   OpenAI? And do `codex.turn.*` metrics go out over OTLP when `[otel]` is
   set? This needs a live capture.
2. Claude Code: what `OTEL_METRICS_INCLUDE_REPOSITORY` adds.
3. omp: whether its logs carry `session.id` or cwd.
4. Whether each harness passes `OTEL_RESOURCE_ATTRIBUTES` into its exports.
5. What gates omp's `gen_ai.response.text`.
