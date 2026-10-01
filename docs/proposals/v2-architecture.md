# Proposal: usage-watch v2 architecture

Status: **proposal for review**, rewritten 2026-10-01 for the final product
direction. Not yet normative. The active design in `docs/design/` is still
the prototype's until this is approved and the old design is archived (§25).
Assumptions made where the direction was underspecified are marked
**[A]** and collected at the end.

---

## 1. Product identity

> **usage-watch is a lightweight, local-first observability runtime for
> development, built on OpenTelemetry, SQLite, SQL, tmux and terminal
> visualisation.**

Point local telemetry at it. It retains the data locally, lets you query
it with SQL, builds terminal dashboards from it, lets agents manage and
investigate it through the CLI, optionally reacts to it with scripts, and
optionally forwards it to larger observability systems.

It is the tool you want **before** a Grafana stack is worth the effort. It
is not a production observability backend.

**Coding agents are the first bundled use case** (a *pack*, §22–23). The
architecture test for every core feature: *if it only makes sense for
Claude, Codex, omp or Workmux, it belongs in the coding-agents pack, not
the core.*

## 2. Non-goals

- production-scale or distributed observability storage;
- a Grafana replacement;
- a cloud or SaaS backend, or a browser dashboard;
- coding-agent orchestration, or built-in nudging;
- a workflow engine (triggers run one executable and stop there);
- a large plugin framework;
- a new telemetry protocol;
- arbitrary SQL writes by anyone;
- coding-agent semantics in core storage.

## 3. Generic system architecture

```
  OTel producers (your app · coding agents · host metrics · …)
          │ OTLP grpc/http           non-OTLP: `usage-watch ingest` (§10)
          ▼                                   │
 ┌──────── one launchd/systemd user agent: `usage-watch serve` ──────────┐
 │                                                                         │
 │  supervises ─► otelcol (child process, generated config)               │
 │                 receivers ← one per *source* (§14)                     │
 │                 processors: memory_limiter → source tagging            │
 │                             → privacy profile (§12) → batch            │
 │                 exporters:  otlphttp(json) → sink                      │
 │                             + optional destinations (§12)              │
 │                                                                         │
 │  sink        OTLP/HTTP JSON  127.0.0.1:4329   (+ the ingest API)       │
 │  writer      the only DB writer; validates, interns, commits, then acks │
 │  scheduler   retention · rollups · trigger evaluation · pack enrichers │
 │  control     unix socket: reload config, regenerate and restart otelcol │
 └─────────────────────────────────┬───────────────────────────────────────┘
                                   │ SQLite (WAL)
     ┌─────────────────────────────┼──────────────────────────────┐
     ▼                             ▼                              ▼
 CLI (humans and agents)     panels → tmux dashboards       triggers → exec(file)
 observe · CRUD · introspect (`usage-watch panel run …`)    (run inside serve)
```

**One daemon, not two.** `serve` supervises otelcol as a child: it
generates otelcol's config from the declared sources, profiles and
destinations, and restarts the child on change. Users run one service.

**One writer.** Only `serve` writes the database. Every query, panel and
trigger reads through the **safe query layer** (§9). Configuration changes
go through validated CLI commands (§13), which write declarative files
and ask `serve` to reload.

## 4. otelcol's responsibilities

- OTLP receivers (gRPC, HTTP, protobuf, JSON), **one per source**, each on
  its own endpoint;
- tagging every record with the source: a `resource` processor inserts
  `usage_watch.source.id`;
- privacy profiles as processors (§12): `transform`/`redaction`, and
  `filter`;
- `memory_limiter`, `batch`, retry, and the persistent `sending_queue`
  (`file_storage`), so nothing is lost while `serve` restarts;
- export to the sink as **OTLP/HTTP JSON**, so the Python side needs no
  protobuf;
- optional external destinations with standard exporters;
- optional extra receivers where they remove code, e.g. `hostmetrics` for
  local system metrics.

usage-watch **generates** otelcol's configuration. It never asks a user or
agent to edit collector YAML (an expert override file is allowed **[A1]**).

## 5. usage-watch's responsibilities

- the **storage contract** (§6–8) and its invariants;
- the **query contract** (§9), and the safe query layer every feature
  shares;
- the **control plane:** sources, named queries, panels, dashboards,
  triggers, destinations, packs, config, all through the CLI, with an audit
  trail;
- the panel runtime and tmux composition;
- the trigger runtime;
- retention and rollups;
- pack installation and pack enrichers (code only in built-in packs, §22).

## 6. Canonical OpenTelemetry SQLite schema (the storage contract)

**Design rule:** model OTLP faithfully, as defined in
`opentelemetry-proto` (`trace/v1`, `logs/v1`, `metrics/v1`, `common/v1`,
`resource/v1`). Interpretation happens in views, never in storage.

**Encodings:**
- **Time:** INTEGER Unix nanoseconds (`*_ns`). 0 means unset, per OTLP.
- **IDs:** `trace_id` (32 hex characters), `span_id` (16 hex), lowercase TEXT,
  as OTLP JSON encodes them. An empty ID is NULL.
- **Attributes and AnyValue:** a JSON object or value with natural JSON
  types. SQLite's JSON keeps int64 integers exact and distinguishes 1 from
  1.0.
  - `bytes` values become `{"$bytes": "<base64>"}`;
  - `kvlist` becomes an object and `array` an array;
  - `string_value_strindex` (profiles only) is not used.
- **Unsigned 64-bit counts** (`count`, bucket counts) are stored as SQLite
  INTEGER (signed 64-bit). A value above 2⁶³−1 is rejected, and the
  rejection is counted **[A2]**.
- **Dropped counts** (`dropped_*_count`) are always kept, since they're part
  of the data's meaning.
- Every row carries `source_id` and `received_ns`.

**Interned entities**

| Table | Columns | Identity |
|---|---|---|
| `resources` | `resource_id` PK, `fingerprint` UNIQUE, `schema_url`, `attributes` JSON, `dropped_attributes_count`, `service_name` (generated from `attributes.service.name`), `first_seen_ns`, `last_seen_ns` | hash of the canonical attributes + `schema_url` |
| `scopes` | `scope_id` PK, `fingerprint` UNIQUE, `name`, `version`, `schema_url`, `attributes`, `dropped_attributes_count` | hash of name, version, attributes, `schema_url` |
| `attribute_sets` | `attrset_id` PK, `fingerprint` UNIQUE, `attributes` | hash of the canonical attributes. Used by metric points and exemplars, where the same set repeats thousands of times |

**Traces**

| Table | Columns |
|---|---|
| `spans` | `span_row` PK, `trace_id`, `span_id`, `parent_span_id`, `trace_state`, `flags`, `name`, `kind` (0–5), `start_ns`, `end_ns`, `attributes`, `dropped_attributes_count`, `dropped_events_count`, `dropped_links_count`, `status_code` (0 unset, 1 ok, 2 error), `status_message`, `resource_id`, `scope_id`, `source_id`, `received_ns`. UNIQUE (`trace_id`, `span_id`) |
| `span_events` | `span_row` FK, `seq`, `time_ns`, `name`, `attributes`, `dropped_attributes_count`. PK (`span_row`, `seq`) |
| `span_links` | `span_row` FK, `seq`, `linked_trace_id`, `linked_span_id`, `trace_state`, `flags`, `attributes`, `dropped_attributes_count` |

**Logs**

| Table | Columns |
|---|---|
| `logs` | `log_row` PK, `time_ns`, `observed_ns`, `severity_number` (0–24), `severity_text`, `body` JSON (AnyValue), `event_name`, `attributes`, `dropped_attributes_count`, `flags`, `trace_id`, `span_id`, `resource_id`, `scope_id`, `source_id`, `received_ns` |

**Metrics**

A **metric stream** follows the OTel data model's identity: resource,
scope, name, unit, type, and for sums and histograms the temporality, plus
monotonicity for sums.

| Table | Columns |
|---|---|
| `metric_streams` | `stream_id` PK, `resource_id`, `scope_id`, `name`, `description`, `unit`, `type` (`gauge`/`sum`/`histogram`/`exp_histogram`/`summary`), `temporality` (0 unspecified, 1 delta, 2 cumulative; NULL for gauge and summary), `is_monotonic` (sum only), `metadata` JSON. UNIQUE on the identity |
| `number_points` | `point_row` PK, `stream_id`, `attrset_id`, `start_ns`, `time_ns`, `value_int`, `value_double` (exactly one set, unless the no-recorded-value flag), `flags`, `source_id`, `received_ns`. For gauge and sum |
| `histogram_points` | `point_row`, `stream_id`, `attrset_id`, `start_ns`, `time_ns`, `count`, `sum` (nullable), `min`, `max` (nullable), `explicit_bounds` JSON array, `bucket_counts` JSON array (length = bounds + 1), `flags`, … |
| `exp_histogram_points` | `point_row`, `stream_id`, `attrset_id`, `start_ns`, `time_ns`, `count`, `sum`, `min`, `max`, `scale`, `zero_count`, `zero_threshold`, `positive_offset`, `positive_counts` JSON, `negative_offset`, `negative_counts` JSON, `flags`, … |
| `summary_points` | `point_row`, `stream_id`, `attrset_id`, `start_ns`, `time_ns`, `count`, `sum`, `quantiles` JSON array of [quantile, value], `flags`, … |
| `exemplars` | `point_table`, `point_row`, `seq`, `time_ns`, `value_int`, `value_double`, `trace_id`, `span_id`, `filtered_attrset_id` |

**Buckets as JSON arrays** **[A3]**: a point's buckets are one row's data,
written once and read together. The query views explode them (`json_each`)
into `otel_histogram_buckets` and `otel_exp_histogram_buckets` with
computed bounds. That's smaller and faster to write than a bucket-per-row
table, and gives the same query surface.

**Ingest bookkeeping:** `ingest_batches` (`batch_id`, `source_id`, signal,
received time, records accepted and rejected by kind, dropped-by-profile
counts). It backs `source test` and `status`. It records **counts and
keys, never values**.

**Indexes** cover what the views and time windows need:
- spans by (`start_ns`), (`trace_id`), (`resource_id`, `start_ns`) and
  (`status_code`, `start_ns`);
- logs by (`time_ns`), (`trace_id`) and (`severity_number`, `time_ns`);
- points by (`stream_id`, `attrset_id`, `time_ns`).

## 7. Trace, log and metric correctness

- **Spans** keep their parentage, links and events. A span seen twice (a
  retry) is idempotent on (`trace_id`, `span_id`), and the later complete
  version wins. A missing parent is allowed: a trace may be partial.
- **Logs:** `time_ns` may be 0. Views use `COALESCE(time_ns, observed_ns)`
  as the event time, per the spec's guidance. The body stays AnyValue, so
  structured logs survive.
- **Metrics:**
  - **Gauges** are instantaneous.
  - **Sums carry temporality.** Views compute per-interval increases
    correctly: delta points are used as they are. Cumulative points are
    differenced per series (stream + attribute set), and a **reset** (a new
    `start_ns`, or a decrease in a monotonic sum) restarts the series
    instead of producing a negative.
  - **Non-monotonic sums** are never turned into rates.
  - **Histograms:** a `sum` can be absent (it's optional, e.g. with negative
    measurements). Min and max are optional. Quantiles are estimated from
    buckets by a registered SQL function, `hist_quantile(bounds, counts, q)`,
    and exponential histograms by `exp_hist_quantile(...)`, with the
    estimation method documented.
  - **Summaries** are stored and exposed, never re-aggregated across points.
  - **The no-recorded-value flag** produces a point with no value, never a
    zero.
- **Exemplars** link points to traces, so a latency outlier leads straight
  to its trace.

Conformance is proven by fixtures covering every type and edge case (§26).

## 8. Storage contract

The physical schema in §6 is documented in `docs/storage.md` and versioned
(`storage_version`), with forward-only migrations run by `serve`. It is
**stable and boring** by design. Ingestion code must preserve its
invariants (§10). Readers *may* query it, but are told to prefer views.

## 9. Query contract, and the safe query layer

**One query engine powers everything:**

```
                       safe query layer (SQL)
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
     CLI query / sql        panel             trigger
      → JSON / table      → renderer        → condition → exec(file)
                           → tmux
```

**Safe query layer** (the same code for every caller):
- a `mode=ro` connection;
- a `set_authorizer` allowing `SELECT` and `READ` only, and denying
  `ATTACH`, `DETACH`, PRAGMA writes, `load_extension`, temp-table writes and
  every write opcode;
- `enable_load_extension(False)`;
- a progress handler with a time budget (default 2 s, configurable per
  call);
- a row cap (default 10,000);
- registered read-only helper functions: `attr()`, `ns_to_iso()`,
  `hist_quantile()`, `exp_hist_quantile()`, a `quantile()` aggregate,
  `rate()`.

**Core views** (contract `views v1`, documented in `docs/views.md` and
exposed through introspection):

| View | One row per | Notes |
|---|---|---|
| `otel_resources`, `otel_services` | resource / service.name | first and last seen, signals seen |
| `otel_spans` | span | `service_name`, `kind_name`, `status_name`, `duration_ms`, `is_root`, `attributes` |
| `otel_span_events`, `otel_span_links` | event / link | |
| `otel_traces` | trace | root span, services, start, duration, span count, has error |
| `otel_logs` | log | `ts_ns` (event time), `severity_name`, `body_text` (if a string), `service_name` |
| `otel_errors` | error | spans with error status ∪ logs with severity ≥ 17 |
| `otel_metric_streams` | stream | |
| `otel_gauge_points`, `otel_sum_points` | point | sums with `increase` and `rate_per_s`, reset-aware |
| `otel_histogram_points` (+ `_buckets`), `otel_exp_histogram_points` (+ `_buckets`), `otel_summary_points`, `otel_exemplars` | point / bucket | |

**Packs add namespaced views** (`genai_*`, `coding_agent_*`, `http_*`).
Every view, core or pack, is registered in a `view_catalog` with its
columns, types, units, description, owner and contract version. That
catalog is what introspection reads.

## 10. Non-OTLP ingestion

Everything enters through one validating path, the **ingest API** on
`serve`:
- **OTLP** through otelcol (the main path);
- **`usage-watch ingest --format otlp-json <file|->`:** OTLP JSON from a file
  or stdin. Importers, scripts and tests use it;
- **pack importers** (e.g. coding-agent history) produce OTLP-JSON-shaped
  records and submit them the same way;
- **host metrics** via otelcol's `hostmetrics` receiver, as a source type,
  with no custom code.

Nothing writes SQLite directly. The schema is documented for reading;
writing goes through the API, so interning, idempotency, source tagging
and the privacy profile are always applied.

## 11. Retention

Retention can be set **per source** and as a global default. It suits
throwaway debugging:

```
usage-watch source add debug-today --port 4319 --retention 24h
```

| Class | What | Default |
|---|---|---|
| detail | spans, logs, points, exemplars, ingest batches | 7 days (per source) |
| rollups | optional per-minute or per-hour aggregates (§26/M8) | 90 days |
| control | sources, queries, panels, dashboards, triggers, audit | until deleted |

`serve` deletes expired detail rows in small batches, every few minutes,
then removes orphaned interned rows. A database size cap (`max_db_mb`) is a
second safeguard, deleting the oldest detail first **[A4]**.

## 12. Privacy profiles and forwarding policy

Privacy is a **per-source profile**, applied in otelcol and again in the
writer.

| Profile | Intent | Behaviour |
|---|---|---|
| `open` | your own app in development | store what you emit; redact well-known secret carriers (authorization, cookie and set-cookie headers, keys matching `*password*`, `*secret*`, `*token*`, `*api_key*`) |
| `standard` (default) | general use | `open`, plus a value-pattern redaction for credential shapes (bearer tokens, `sk-…`, private key blocks) |
| `strict` | coding agents; anything sensitive | **fail-closed allowlist** of attribute keys per signal, plus drop all log bodies and span event attributes unless allowlisted. Packs ship their own strict allowlists |

- **Drops and redactions are counted by key**, never logging values.
- **Destinations** (`usage-watch destination add …`) receive data **after**
  the source's profile, and can require a stricter one: a destination
  declares `min_profile`, so a `strict` source is never forwarded with less.
  There's no raw branch by default.
- **Packs can force a profile** for their sources. The coding-agents pack
  forces `strict`, and keeps harness content capture disabled (§23).

## 13. The CLI as control plane

- **Trust model:** whoever can run `usage-watch` can manage it. There's no
  approval system. Every mutation is **validated** and **audited** (§21).
- **Grammar:** `usage-watch <object> <verb> [name] [flags]`, with
  observation commands at top level.

| Object | Verbs |
|---|---|
| `source` | `add`, `list`, `show`, `update`, `remove`, `test`, `enable`, `disable` |
| `query` (named) | `create`, `list`, `show`, `update`, `delete`, `run` |
| `panel` | `create`, `list`, `show`, `update`, `delete`, `run`, `check` |
| `dashboard` | `create`, `list`, `show`, `update`, `delete`, `add-panel`, `remove-panel`, `launch`, `close` |
| `trigger` | `create`, `list`, `show`, `update`, `delete`, `enable`, `disable`, `test`, `history` |
| `destination` | `add`, `list`, `show`, `update`, `remove`, `test` |
| `pack` | `install`, `list`, `show`, `remove` |
| `config` | `get`, `set` (retention, defaults, ports, budgets) |
| `service` | `install`, `uninstall`, `start`, `stop`, `status`, `logs` |

- **Objects are stored** as TOML files under `~/.config/usage-watch/<object>/`
  (portable, diffable). The CLI is the supported way to change them. It
  validates, writes atomically, records the audit entry, then tells `serve`
  to reload over the control socket. Hand edits are picked up and validated
  on reload **[A5]**.
- **Output:** every command takes `--json`. Follow modes take `--jsonl`.
  - Envelope: `{"ok": true, "<object>": {...}, ...}`, or
    `{"ok": false, "error": {"code": "PORT_IN_USE", "message": "...", "fix": "..."}}`.
  - Error codes are stable identifiers.
  - **Exit codes:** 0 ok; 1 the operation failed; 2 invalid usage or
    validation failure; 3 not found; 4 conflict (exists, port in use);
    5 the service isn't running.
- **Lifecycle:** `serve` installs as a launchd agent on macOS or a systemd
  user unit on Linux, through `usage-watch service install`.

## 14. Source CRUD

A **source** is everything needed to get telemetry in: its type, endpoint,
profile, retention and labels.

| Type | Creates |
|---|---|
| `otlp` | an otelcol OTLP receiver on the given port(s) and protocols, a pipeline with source tagging and the profile |
| `hostmetrics` | otelcol's `hostmetrics` receiver at an interval |
| `ingest` | no listener; accepts `usage-watch ingest --source <id>` |
| pack types (`coding-agent:claude`, …) | defined by the pack (§23) |

`source add` validates the port is free, regenerates otelcol's config,
restarts the child, waits for the receiver to listen, and returns:

```json
{"ok": true, "source": {"id": "my-api", "type": "otlp", "endpoints":
 {"grpc": "127.0.0.1:4319", "http": "127.0.0.1:4320"}, "profile": "open",
 "retention": "7d"}, "collector_reloaded": true, "status": "waiting_for_telemetry"}
```

`source test` reports listening, the last batch per signal, record counts,
the services and resources seen, the top span and metric names, and drops.
It's everything an agent needs to confirm telemetry is arriving.

## 15. Query and introspection CLI

**Observation**, with `--since`, `--until`, `--service`, `--source`,
`--limit` and `--json`:

| Command | Answers |
|---|---|
| `services` | what is sending, and which signals |
| `traces` | recent traces: root, duration, errors |
| `trace show <id>` | the span tree with timings, events, links and related logs |
| `logs [--severity --grep]` | log records |
| `metrics [--name]` | streams and recent values (rates for sums, quantiles for histograms) |
| `errors` | error spans and logs |
| `sql '<query>'` | anything; read-only through the safe layer |
| `investigate --service S --since 30m` | **a deterministic evidence bundle, no LLM:** error groups, the slowest spans and their traces, logs around errors, metric changes against the previous equal window. It's built from named queries, so it's cheap and inspectable **[A6]** |

**Introspection:**
- `schema` (storage tables);
- `views`;
- `describe <view>` (columns, types, units, semantics);
- `queries`;
- `panels`;
- `dashboards`;
- `triggers`;
- `sources`;
- `destinations`;
- `packs`;
- `render-types` (each type's required and optional field mappings);
- `capabilities` (the CLI's own versions and contract versions).

All are JSON. An agent needs nothing preloaded.

## 16. Named-query CRUD

A named query is TOML: `name`, `description`, `sql`, typed `params`
(`since`, `service`, …, with defaults) and `columns` (documented output).
It can be referenced by panels, triggers and `investigate`.

```
usage-watch query create checkout-errors --sql '…' --param service=api --description '…'
usage-watch query run checkout-errors --since 30m --json
```

**Parameters bind as SQL parameters**, never by string interpolation.
Packs may add **parameter resolvers**: e.g. the coding-agents pack's
`--self` resolves `$TMUX_PANE` to session IDs. That's a pack feature, not
core.

## 17. Panel CRUD and format

```toml
[panel]
name = "request-rate"
title = "Request rate"
refresh = "2s"

[query]                  # either inline SQL or a named query
sql = "SELECT … FROM otel_spans WHERE …"
# named = "request-rate"   params = { service = "api" }

[render]
type = "line"
x = "minute"
y = ["requests"]
series = "route"         # optional
unit = "req/s"
```

- **CRUD:**
  - `panel create <name> --type line --sql … --x … --y …`, or
    `--from-file panel.toml`;
  - `panel update` (with flags that patch);
  - `panel check` (validates the schema, runs `EXPLAIN` through the safe
    layer, and checks that the mappings name result columns);
  - `panel run <name>` (draws in the current terminal).
- **The runtime:**
  - redraws on `refresh`, but **skips the query when nothing changed**,
    checked with `PRAGMA data_version`;
  - sizes the plot to the terminal on resize;
  - shows the query's age and its error, without crashing.

## 18. Rendering

**Render types are templates:** SQL result + type + field mappings. There's
no custom plotting code.

| Type | Engine | Mappings |
|---|---|---|
| `table` | ANSI | columns, column formats, max rows |
| `stat` | ANSI | value, unit, thresholds (colour), optional delta |
| `list` / `timeline` | ANSI | time, text, level |
| `sparkline` | ANSI block characters | y, optional x |
| `line` | Plotext `plot` (date x via `date_form`) | x, y[], series |
| `bar` | Plotext `bar`, `multiple_bar`, `stacked_bar` (vertical or horizontal) | x, y[], orientation, stacked |
| `scatter` | Plotext `scatter` | x, y, series |
| `histogram` | Plotext `hist` (raw values), or a `bar` over pre-bucketed rows (OTel histograms) | values, or bounds + counts |

Plotext is an optional extra (`usage-watch[dashboard]`). Without it, the
Plotext types fall back to a table, with a note. Exact Plotext behaviour
(time axes, colours, resizing) is verified in M0 against the installed
version.

## 19. Dashboard CRUD and tmux composition

```toml
[dashboard]
name = "api-dev"
title = "API dev"

[[rows]]
height = "40%"
cells = [ { panel = "request-rate" }, { panel = "latency" } ]

[[rows]]
height = "30%"
cells = [ { panel = "errors" }, { panel = "slow-spans" } ]

[[rows]]
cells = [ { panel = "recent-traces" } ]
```

- **CRUD:** `create`, `add-panel <dash> <panel> --row N [--width 50%]`,
  `remove-panel`, `update`, `show`, `delete`.
- **`launch`** creates a tmux window (or a session, if you're outside tmux)
  named after the dashboard. It splits it per the grid, each pane running
  `usage-watch panel run <name>`.
- **`close`** kills that window.
- **tmux keeps** layout, resize, attach and detach, and remote use.
  usage-watch only creates the panes.
- **Cross-panel focus**, optional later: a tmux window option
  (`@uw_focus`) that panels may read as a query parameter.

## 20. Trigger CRUD and runtime

```toml
[trigger]
name = "high-error-rate"
every = "10s"
for = "30s"          # condition must hold this long
cooldown = "10m"
timeout = "30s"
enabled = true

[query]
named = "error-rate"     # or sql = "…"
params = { service = "api" }

[condition]            # on the first row's column, or on row count
column = "error_rate"
operator = ">"         # > >= < <= == != ; or `rows > 0`
value = 5

[action]
exec = "~/.config/usage-watch/actions/notify-me"
```

- **Evaluated inside `serve`** through the safe query layer.
- **Firing:** when the condition has held for `for`, outside the cooldown,
  with at most one execution running per trigger.
- **Execution:**
  - the action is a **path to an executable**: no shell, no
    interpolation;
  - it must be an absolute path once `~` is expanded, owned by the user, and
    not group- or world-writable;
  - matched rows go on **stdin as JSON**
    (`{"trigger", "fired_at", "rows", "query"}`);
  - the environment is minimal (`PATH`, `HOME`, `USAGE_WATCH_TRIGGER`);
  - it's killed at the timeout.
- **Recorded in `trigger_runs`:** time, duration, exit code, rows matched,
  and whether it timed out. **Stdout and stderr are not stored** unless the
  trigger sets `capture_output = true`, and even then only up to 4 KB,
  redacted by the `standard` patterns.
- `trigger test <name>` evaluates once and shows what would run, without
  executing.

Nudging an agent is now simply a user's trigger script.

## 21. Audit trail

Every mutating CLI operation appends one JSON line to
`~/.local/state/usage-watch/audit.jsonl`:

```json
{"ts": "…", "op": "panel.create", "object": "panel", "id": "request-rate",
 "result": "ok", "error_code": null, "cwd": "…", "tmux_pane": "%14",
 "project": "my-api", "cli_version": "…"}
```

- Changed field names are included, values are not. SQL text is stored
  hashed, plus its length **[A7]**.
- `usage-watch audit --since … --json` reads it.
- It's an operational record, not proof of who acted.

## 22. Packs: the domain model

A pack is **data first**:

```
packs/<name>/
├── pack.toml          name, version, requires (core contract versions),
│                      provides, forced profile for its sources
├── views.sql          CREATE VIEW <prefix>_… (registered in view_catalog)
├── queries/*.toml
├── panels/*.toml
├── dashboards/*.toml
├── triggers/*.toml    (installed disabled)
└── sources/*.toml     source templates
```

- `pack install <path|builtin>` validates every object (views compile
  through the safe layer; panels and queries are checked), then registers
  them with a pack owner. `pack remove` removes exactly what it installed.
- **Code:** third-party packs are declarative only in v2. **Built-in packs
  may ship Python** for collection, enrichment and import, as `serve`
  plugins behind a narrow interface. Their output is always OTel records
  through the ingest API (§10), so pack data is ordinary telemetry **[A8]**.

## 23. The coding-agents pack (built-in)

**It reuses** the prototype's research (R1–R10), topology, pane joins,
identity and import code.

- **Sources:** `coding-agent:claude`, `:codex` and `:omp`.
  - Each **prints, or applies with consent and a backup,** that harness's
    own OTel configuration, pointing at a pack-owned OTLP receiver.
  - Content capture stays off:
    - **Claude:** prompt, tool and raw-body logging disabled;
    - **Codex:** every exporter set explicitly local or `none` (metrics
      would otherwise default to Statsig), and `log_user_prompt`,
      `log_agent_responses` and `log_guardian_assessments` all false;
    - **omp:** `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`.
  - **The profile is forced to `strict`,** with the pack's per-harness
    allowlists.
- **Enricher (Python):** every ~5 s it emits
  `coding_agent.context` **log records** (event name) carrying the pane,
  tmux session and window, harness session ID, cwd, a keyed hash of the
  repo, worktree, branch and Workmux state. These are the prototype's
  topology, `panes.py` and Workmux logic, now emitting telemetry instead of
  writing their own tables.
- **Importer:** history from session logs, emitted as OTel log records via
  ingest. It's for history from before OTel was enabled, and explicit gap
  repair (`usage-watch pack run coding-agents import --since …`). It's
  reconciled by request IDs and never runs continuously.
- **Views:**
  - `genai_model_calls`: from Claude `claude_code.api_request` events,
    Codex turn spans or logs, and omp `chat` spans;
  - `genai_token_usage`, `genai_tool_calls`, `coding_agent_sessions`;
  - `active_coding_agents`, whose state carries its basis: Workmux
    explicit, or `active`/`quiet`/`gone` from telemetry.

  All are joined to context by session ID and time.
- **Queries:** `what-have-i-done` (`--self`), `token-burn`, `slow-tools`,
  `compare-session`.
- **Dashboard:** `agents` (the Workmux monitoring replacement).
- **Optional:** a capacity source (Codex `rate_limits`, omp usage cache,
  Claude status line tap), only where cheap.

## 24. Generic local-app example (acceptance scenario 1)

A FastAPI app with the OTel SDK sends OTLP to `:4319`. The agent uses only
the CLI:

```
usage-watch capabilities --json                         # 1 discover
usage-watch source add my-api --port 4319 --profile open --retention 24h --json   # 2 configure
usage-watch source test my-api --json                   # 3 verify: signals, services, span names
usage-watch services --json ; usage-watch views --json ; usage-watch describe otel_spans --json   # 4 inspect
usage-watch sql "SELECT name, count(*), avg(duration_ms) FROM otel_spans
                 WHERE service_name='my-api' AND start_ns > …" --json      # 5 query
usage-watch query create api-latency --sql "… hist_quantile(…) …" --param service=my-api   # 6 named queries
usage-watch panel create request-rate --type line --query api-request-rate --x minute --y requests
usage-watch panel create latency --type line --query api-latency --x minute --y p50,p95,p99
usage-watch panel create errors --type table --query recent-errors
usage-watch panel create slow-spans --type table --query slow-spans
usage-watch panel create recent-traces --type list --query recent-traces           # 7 panels
usage-watch dashboard create api-dev --grid '[["request-rate","latency"],["errors","slow-spans"],["recent-traces"]]'   # 8 compose
usage-watch dashboard launch api-dev                    # 9 observe while the user reproduces
usage-watch investigate --service my-api --since 15m --json ;  usage-watch trace show <id> --json   # 10–11
usage-watch panel create checkout-db --type bar … ; usage-watch dashboard add-panel api-dev checkout-db   # modify
usage-watch dashboard delete api-dev ; usage-watch source remove my-api                  # 12 clean up
```

Generic queries for HTTP and database spans (`http_*`) ship in a small
built-in **`otel-basics` pack**, using the OTel HTTP and DB semantic
conventions. That keeps them out of core too.

**Scenario 2:** `usage-watch pack install coding-agents`, then
`usage-watch source add coding-agent:claude` (and Codex, omp), then
`usage-watch dashboard launch agents`. It's the same core, with a
different pack.

## 25. Migration from the prototype

| Prototype | Fate |
|---|---|
| `topology.py`, `collectors/panes.py`, Workmux handling | **Move into the coding-agents pack** enricher |
| `identity.py` (keyed hashing), `errors.py` (prompt-style errors), `sh.py`, `notify.py` | **Core utilities** (the hashing is generic) |
| `collectors/claude.py`, `codex.py`, `omp.py` | **Pack importer**, rewritten to emit OTel log records |
| `collectors/statusline.py` + capacity sources, `capacity.py`, `alerts.py` | **Pack optional capacity source**. Alerts become triggers |
| `collectors/screen.py`, `adapters/` | **Retire** (a pane-capture diagnostic may stay in the pack's `doctor`) |
| `runtime/` (reconcile, attribution, accounts, core), `store/` v1, `queries.py`, `dashboard.py` (curses), current `cli.py` | **Retire** as v2 lands. Liveness becomes `service status` |
| `docs/design/` D1–D8 | **Archive** to `docs/design/archive/prototype-v1/` with a banner. A new active design index for v2 |
| `docs/research/` | **Keep**. Mark superseded conclusions |
| The prototype store `usage.db` | Left in place, read-only. **Open decision 2** |

## 26. Testing

| Layer | Tests |
|---|---|
| **Storage conformance** | OTLP JSON fixtures for every signal and metric type: gauge; delta and cumulative sums (monotonic and not, with resets); histograms with and without sum and min/max; exponential histograms (positive, negative, zero bucket, scale); summaries; exemplars; links; events; empty and odd IDs; log time 0. Each is ingested, then checked: rows, the views' values, and **a round-trip back to OTLP JSON** with no semantic loss |
| **Correctness properties** | rates never negative across resets; non-monotonic sums never rated; histogram quantiles stay within bucket bounds; retries are idempotent |
| **Privacy** | per profile: fixtures carrying prompts, model text, tool I/O, credentials and emails; assert what may reach SQLite, query output and the forwarded stream; drops counted, values never logged |
| **Safe SQL** | writes, `ATTACH`, PRAGMA writes, extension loading, recursive or expensive CTEs, huge results: all must fail or be capped |
| **CLI contract** | golden JSON for every command; stable error codes and exit codes; audit lines for every mutation |
| **Control plane** | source CRUD regenerates valid otelcol config (validated with `otelcol validate`); reload; port conflicts |
| **Panels and dashboards** | render snapshots from fixture databases, at fixed terminal sizes; layout generation (tmux commands asserted, not executed) |
| **Triggers** | the `for`/cooldown state machine; timeouts; refusal of unsafe actions; stdin payload; no output stored by default |
| **Packs** | install and remove are exact; views compile; coding-agents fixtures (recorded, redacted OTLP per harness) |
| **Live, manual (not CI)** | a runbook: a tiny real request through each harness and a sample FastAPI app, into a real otelcol |
| **Footprint** | idle CPU and memory measured for `serve` + otelcol, with a budget recorded per release |

## 27. Milestones (each leaves something runnable)

| M | Delivers | You can |
|---|---|---|
| **M0 Contracts** | `docs/storage.md`, `docs/views.md`, `docs/cli.md` (object model, JSON envelope, codes); conformance and harness fixtures; otelcol component check (the processors and exporters we rely on, `encoding: json`); Plotext check | review the contracts |
| **M1 Pipeline** | `serve` (sink, writer, retention, otelcol supervision with **one default OTLP source**), the §6 schema and core views, `service install`, observation commands, `sql`, introspection | **point any app at it and query traces, logs and metrics** |
| **M2 Control plane** | source and destination CRUD with `test`, config, JSON envelope and exit codes, audit, profiles | **an agent can wire up a new app by itself** |
| **M3 Panels** | named-query CRUD, panel CRUD and runtime, all render types | terminal panels from SQL |
| **M4 Dashboards** | dashboard CRUD, tmux launch, the `otel-basics` pack, `investigate`, `trace show` | **acceptance scenario 1 end to end** |
| **M5 Coding-agents pack** | harness sources, strict profiles, enricher, views, queries, the `agents` dashboard, `--self` | **acceptance scenario 2: the Workmux monitor replacement** |
| **M6 Triggers** | trigger CRUD and runtime, `trigger_runs` | react to telemetry with scripts |
| **M7 Refinements** | rollups, history import, third-party declarative packs, footprint tuning | longer history, shareable packs |

---

## Open decisions (these genuinely block)

1. **How otelcol is obtained.** The design relies on contrib components
   (`transform`/`redaction`, `filter`, `file_storage`, `hostmetrics`).
   Options:
   - **(a)** download the official `otelcol-contrib` release binary in
     `service install` (simple; about 250 MB);
   - **(b)** ship a **custom build** with only the needed components,
     using the OpenTelemetry Collector Builder (`ocb`). That means a
     small binary and a fast start, but we'd build and publish binaries;
   - **(c)** Homebrew, if a contrib formula is available.

   This affects the footprint goal (§32 of the brief) and the install
   story. **Lean: (b)**, falling back to (a).
2. **The prototype store's history.** Import its usage history into the v2
   store as pack telemetry (it holds Claude history no longer on disk), or
   start clean.

## Assumptions made for this proposal

| # | Assumption |
|---|---|
| A1 | No one is asked to edit otelcol YAML. An optional expert override file is merged last, and `validate`d |
| A2 | Unsigned 64-bit counts above 2⁶³−1 are rejected and counted, rather than stored lossily |
| A3 | Histogram and exponential-histogram buckets are stored as JSON arrays on the point row, and exploded by views |
| A4 | A database size cap (`max_db_mb`) backs up time-based retention |
| A5 | TOML files are the persisted form of control objects. The CLI is the supported writer. Hand edits are validated on reload, and an invalid file is reported and skipped, never applied |
| A6 | `investigate` is a fixed composition of named queries (no LLM), versioned with the views |
| A7 | The audit trail records field names and SQL hashes, not values or SQL text |
| A8 | Third-party packs are declarative only in v2. Built-in packs may include Python, which only produces OTel records through the ingest API |
| A9 | Default ports: the sink on `127.0.0.1:4329`; the default OTLP source on 4317 (gRPC) and 4318 (HTTP); everything bound to localhost |
| A10 | Default detail retention is 7 days for generic sources. The coding-agents pack sets 14 |
