# Proposal: usage-watch v2 architecture

Status: **proposal for review**, 2026-10-01. Not yet normative: the active
design in `docs/design/` is still the prototype's until this is approved
and the old design is archived (§9). Built from the owner's clarification
answers (Q1–Q24) and the repository at `1d288c2`.

> **usage-watch** is a local-first OpenTelemetry observability hub for coding
> agents. See what every coding agent on your machine is doing, from one
> terminal. Keep Workmux for orchestration; use usage-watch for visibility.

---

## 1. System, containers, components

```
                     coding-agent harnesses
              Claude Code · Codex CLI · omp · (future)
                              │ OTLP (each harness's own config)
                              ▼
 ┌──────────────── always on (launchd user agents) ─────────────────┐
 │                                                                   │
 │  otelcol-contrib  127.0.0.1:4317 grpc / :4318 http                │
 │    receive → memory_limiter → privacy allowlist → batch          │
 │      ├─ otlphttp (encoding: json) → usage-watch sink              │
 │      └─ optional otlp/otlphttp → external destinations            │
 │                                                                   │
 │  usage-watch serve  (one process, the only DB writer)             │
 │    sink    : OTLP/HTTP JSON on 127.0.0.1:4329                     │
 │    guard   : allowlist again (defence in depth)                   │
 │    store   : L1 sanitized OTel → L2 domain records → L3 rollups   │
 │    context : tmux · panes ↔ sessions · git · Workmux (every ~5 s) │
 │    upkeep  : retention, rollups, self-telemetry                   │
 └───────────────────────────────┬──────────────────────────────────┘
                                 │ SQLite (WAL), read-only to everyone else
          ┌──────────────────────┼─────────────────────────┐
          ▼                      ▼                         ▼
  usage-watch dashboard   usage-watch recent/usage/   coding agents
  → tmux window of        activity/sql (--json)       (same CLI)
    `usage-watch panel …`
```

| Container | Runs | Owns |
|---|---|---|
| **otelcol-contrib** | launchd, always | OTLP reception (gRPC, HTTP, protobuf, JSON), the first privacy stage, batching, retry and queues, fan-out to external destinations |
| **usage-watch serve** | launchd, always | the sink, the second privacy stage, enrichment, the SQLite store, derivation, retention |
| **usage-watch CLI** | on demand | panels, dashboards, named queries, SQL, setup, doctor, history import |

**One writer.** `serve` is the only process that writes the database. Every
CLI and panel connection is read-only. That removes the prototype's
per-table ownership rules and its lock dance.

## 2. Data flow

1. **Harness → otelcol.** Each harness is configured, by its own supported
   mechanism, to send to `127.0.0.1:4317/4318` (§6). Content capture is off.
2. **otelcol processing:** `memory_limiter`, then the **privacy stage**
   (an attribute allowlist per signal, §6), then `batch`. Two pipelines fan
   out from the sanitized stream:
   - the `otlphttp/usage-watch` exporter, with `encoding: json` and a
     `sending_queue` backed by `file_storage`, so telemetry survives
     usage-watch restarts;
   - optional external exporters, **after** the privacy stage only.
3. **Sink.** `POST /v1/{traces,logs,metrics}` with OTLP JSON. It parses
   in memory, applies the guard (the same allowlist), and enqueues to the
   writer thread. It answers 200 only after the batch commits, so
   otelcol's retry is real.
4. **Writer.** In one transaction per batch:
   - L1 rows for resources, spans, span events, logs and metric points;
   - the **harness mapper** derives L2 records (model calls, tool calls,
     sessions, errors), stamping each with the context valid at that
     record's time;
   - dropped-field counters are updated.
5. **Context loop** (every ~5 s). It snapshots tmux, panes ↔ sessions,
   git and Workmux into time-bounded context records (§5).
6. **Upkeep** (every few minutes). It updates the L3 rollups and enforces
   retention per class.
7. **Readers.** Panels and queries read the **view contract** (§7).

## 3. otelcol ↔ usage-watch responsibilities

| Concern | otelcol | usage-watch |
|---|---|---|
| Protocols, protobuf, gRPC | ✓ | JSON over HTTP only |
| Privacy allowlist | ✓ first stage | ✓ second stage (independent) |
| Batching, retry, persistent queue | ✓ | commit-then-ack |
| External forwarding | ✓ (sanitized only) | none |
| Harness semantics (what is a "model call") | no | ✓ harness mappers |
| Local context (tmux, git, Workmux) | no | ✓ |
| Storage, retention, views | no | ✓ |
| Config for otelcol itself | — | **generated** by `usage-watch setup collector` from usage-watch's allowlist, so the two stages can't drift |

**Single source of truth for privacy:** `allowlist.toml` in the package
lists the allowed attribute keys per signal, harness and record type.
usage-watch's guard reads it, and `setup collector` renders it into the
collector's processor config. A fixture test proves the two stages agree.

## 4. Local SQLite storage

One database, `~/.local/state/usage-watch/telemetry.db`, separate from the
prototype's `usage.db` (§9).

**L1, sanitized OTel.** Close enough to OTel to reinterpret later. Only
allowlisted attributes, stored as a JSON object.

| Table | Key columns |
|---|---|
| `resources` | `resource_id` (hash of the sorted allowlisted attributes), `harness`, `service_name`, `service_version`, `attrs` |
| `spans` | `trace_id`, `span_id`, `parent_span_id`, `resource_id`, `name`, `kind`, `start_ns`, `end_ns`, `status_code`, `attrs` |
| `span_events` | `trace_id`, `span_id`, `seq`, `name`, `time_ns`, `attrs` |
| `log_records` | `record_id`, `resource_id`, `time_ns`, `event_name`, `severity`, `trace_id`, `span_id`, `attrs` (the body is dropped unless allowlisted) |
| `metric_points` | `resource_id`, `name`, `kind`, `time_ns`, `start_ns`, `value`, `attrs` |
| `drops` | per minute: signal, harness, attribute key, count. **Keys only, never values** |

**L2, domain records** (derived by harness mappers; re-derivable from L1
within L1's retention):

| Table | Rows | Notable columns |
|---|---|---|
| `sessions` | one per harness session or conversation | `session_key` (`harness:id`), first and last seen, the derived parent session |
| `model_calls` | one per provider request | `session_key`, `harness`, `provider`, `model`, `started_at`, `duration_ms`, token components (D1-style: uncached, cache read, cache write, output, reasoning-of-output), `request_key` (hashed), `status`, `error_type`, `cost_usd_micros` + `cost_basis` (`harness_estimate`/`billed`), `source` (`otel`/`import`), and context stamped at that time (`project`, `repo_id`, `branch`, `pane`) |
| `tool_calls` | one per tool invocation | `session_key`, `tool`, `started_at`, `duration_ms`, `ok`, `error_type`, stamped context. **Never arguments or output** |
| `errors` | API and tool errors | `session_key`, time, kind, code |

**L3, rollups:** `usage_minute` and `usage_day`, by harness, provider,
model, project and repo. Sums of tokens and calls, and an unknown count.

**Context records** (§5): `pane_snapshots` and `session_context`.

**Retention classes** (one simple config, `retention_days = 14` and
`history_days = 400`):

| Class | Tables | Default |
|---|---|---|
| detail | L1, L2, context | `retention_days` (14) |
| history | L3 rollups | `history_days` (400) |

**Kept from the prototype's invariants:**
- one provider request is one `model_calls` row, with uniqueness on
  `(harness, request_key)` where a key exists;
- unknown stays null, and sums report their unknown count;
- rollups equal their source rows;
- auxiliary calls are inside totals, flagged.

## 5. Enrichment model

Context is **time-bounded evidence**, not a guess stamped forever.

| Source | Gives | How |
|---|---|---|
| **OTel resource attributes** | pane, project, if the launcher set them (an optional wrapper or Workmux, later) | authoritative when present |
| **Panes ↔ sessions** (reuse `collectors/panes.py`) | pane → harness session | Claude `~/.claude/sessions/<pid>.json`; omp terminal-session or open session file; Codex open rollout file. Read-only metadata, no content |
| **tmux** (reuse `topology.py`) | session, window, pane, process tree, cwd | `list-panes`, `ps` |
| **git** (reuse) | repo id (keyed hash of the normalised remote), checkout, worktree, branch | from cwd |
| **Workmux** (optional) | worktree or lane name, **explicit state** working/waiting/done | `workmux status --json`, `list --json` |

`session_context(session_key, pane, project, repo_id, branch, workmux_state,
valid_from, valid_to)` is updated by the context loop. A model or tool
call is stamped with the context valid at its own time. History never
inherits present-day context (the prototype's D2 rule, kept).

**Agent state** (Q8), shown with its basis:

| Shown | Basis |
|---|---|
| `working` / `waiting` / `done` | Workmux, explicit |
| `active` | a model or tool call within the last ~30 s (OTel) |
| `quiet Ns` | no telemetry for N seconds, process alive |
| `gone` | the process has exited |
| `unknown` | no basis |

Screen scraping is retired as a state source.

## 6. Privacy and filter boundary

**Three layers. The first is a courtesy, the second and third are
guarantees.**
1. **Harness configuration** keeps content off:
   - **Claude:** prompt, tool and raw-body logging switches off;
   - **Codex:** `log_user_prompt`, `log_agent_responses` and
     `log_guardian_assessments` set false, and **every** exporter
     explicitly local or `none` (metrics would otherwise default to
     Statsig);
   - **omp:** `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false`.

   `usage-watch setup harness <name>` prints the exact snippet, and applies
   it only with consent and a backup (the status line tap's pattern).
2. **The otelcol privacy stage:** fail-closed allowlist per signal (keys
   from `allowlist.toml`). Implementation candidates are in §10.
3. **The usage-watch guard:** the same allowlist, independently enforced.
   - Identity attributes (emails, account and org IDs) are **dropped**, or
     **keyed-hashed** where a view needs them (`identity.py`, kept).
   - Bodies of log records are dropped unless allowlisted.
   - Drops are counted by key, never by value.

**Forwarding is sanitized only**, as a pipeline after layer 2. There is no
raw branch in the default config.

## 7. Query and view contract

Readers use **versioned views**, never tables. The contract is
`docs/views.md`: names, columns, units and semantics, at `contract v1`.

| View | One row per |
|---|---|
| `agents` | live agent (pane or session) right now: harness, model, project, branch, state + basis, elapsed, tokens, tokens/min, last activity, errors |
| `model_calls` | provider request (L2, with context) |
| `tool_calls` | tool invocation |
| `sessions` | session, with totals and first/last activity |
| `usage_minute`, `usage_day` | rollup bucket |

**Interfaces:**
- **Named:**
  - `usage-watch agents` / `recent [--self] [--since]` / `activity --session`;
  - `usage [--since --by]`;
  - all with `--json`. `--self` resolves through `$TMUX_PANE` → the pane's
    session.
- **SQL:** `usage-watch sql '<query>' [--json]`, over the views only.

**SQL safety** (shared with panels):
- a `mode=ro` connection;
- a `set_authorizer` that permits only `SELECT` on contract views and
  read-only functions, and denies `ATTACH`, `PRAGMA` writes and
  `load_extension`;
- `enable_load_extension(False)`;
- a progress handler with a time budget;
- a row cap.

## 8. Panels and dashboards

- **A panel** is a TOML file (`name`, `title`, `refresh_seconds`, `query`,
  `[render]`), or a built-in using the same render types.
  - **Render types:** `stat`, `table`, `bar`, `sparkline`, `line`, `list`
    (timelines). `table`, `stat` and `list` are plain ANSI. `bar`, `line`
    and `sparkline` use Plotext if installed (the `usage-watch[dashboard]`
    extra), else an ANSI fallback.
  - **Run** with `usage-watch panel <name|file>`: it redraws every
    `refresh_seconds` and resizes on `SIGWINCH`.
  - **Check** with `usage-watch panel check <file>`: it validates the
    schema, the query (it runs `EXPLAIN` under the authorizer) and the
    render fields.
  - **Locations:** built-ins ship in the package; user and agent panels go
    in `~/.config/usage-watch/panels/`.
- **A dashboard** is a TOML file naming panels and a tmux layout.
  `usage-watch dashboard <name>` creates (or re-attaches to) a tmux window
  and splits it into panes, each running `usage-watch panel …`.
  - It ships with `monitor`: the Workmux monitoring replacement.
- **Selection** (optional, small): the agents panel can write the focused
  session to a tmux window option, `@uw_session`, and the detail panel
  reads it. That's not a v1 requirement.

## 9. Migration from the prototype

| Prototype piece | Fate |
|---|---|
| `topology.py`, `collectors/panes.py`, `identity.py`, `sh.py`, `errors.py`, `notify.py` | **Keep**: they become the context loop and utilities |
| `collectors/claude.py`, `codex.py`, `omp.py` (logs) | **Become `usage-watch import`**: history before OTel, plus explicit gap repair, writing `model_calls` with `source=import` and reconciled by `request_key`. omp's side-call parsing kept only if OTel lacks those calls (verify in M0) |
| `collectors/statusline.py`, capacity sources, `capacity.py`, `alerts.py` | **Park** as an optional capacity module (Q3). Not in the first milestones |
| `collectors/screen.py`, `adapters/` | **Retire** as a state source. A pane-capture diagnostic may stay in `doctor` |
| `runtime/` (core, reconcile, attribution, accounts, liveness), `store/` v1, `queries.py`, `dashboard.py` | **Retire** once their v2 replacements land. Liveness becomes a `serve` health check |
| `docs/design/D1–D8` | **Archive** to `docs/design/archive/prototype-v1/`, with a banner. New `docs/design/` index for v2 |
| `docs/research/` | **Keep**. Mark superseded conclusions (e.g. "session logs primary") |
| The prototype store (`usage.db`) | **Kept read-only**, with an optional one-time import of its `usage_events` into `model_calls` (`source=prototype`), since Claude transcripts older than ~30 days no longer exist on disk |

## 10. Milestones: smallest path to a Workmux monitoring replacement

| M | Delivers | Useful because |
|---|---|---|
| **M0 Contracts** | redacted OTLP fixtures from all three harnesses (manual live capture); `allowlist.toml` v1; `docs/views.md` v1; verification of the open items below | everything after is tested against real shapes |
| **M1 Pipeline** | `serve` (sink, guard, L1, `model_calls`, `tool_calls`, `sessions`); `setup collector` (generated otelcol config) and `setup harness`; launchd plists; `doctor` checks the chain | telemetry flows and persists, always on |
| **M2 Context** | context loop: panes ↔ sessions, tmux, git, Workmux state; `session_context`; stamping | records know their project, branch and pane |
| **M3 Monitor** ← **the replacement** | `agents` view; panels `agents`, `activity`, `token-rate`; dashboard `monitor` | **replaces the Workmux monitoring view** |
| **M4 Agents query** | `recent --self`, `activity`, `sql` with the safe connection; agent primer updated | agents can query their own history |
| **M5 Usage history** | rollups, retention classes, `usage` views and panels; `usage-watch import` (logs, prototype store) | long-term trends, history before OTel |
| **M6 Custom panels** | TOML panel schema, `panel check`, `dashboard` definitions | user and agent panels |
| **M7 Extras** | forwarding recipes (sanitized), the optional capacity panel, performance and trace views | integration and deeper analysis |

M3 is reachable without M4–M7. Each milestone retires the prototype
pieces it replaces.

---

## Open items

**Blocking (owner decision):**
1. **otelcol distribution and installation.** The privacy stage and the
   persistent queue need **contrib** components (`transform` or
   `redaction` processor, `file_storage`). Options: Homebrew, if a contrib
   formula exists; the official release binary installed by
   `usage-watch setup collector`; or a custom `ocb` build containing only
   the needed components. This decides the setup UX and the update story.
2. **The prototype store's history.** Import it once (preserving Claude
   history that is no longer on disk), or start clean.

**To verify in M0 (not decisions, facts):**
- whether Claude Code's **events** (`api_request`, `tool_result`,
  `tool_decision`) are enough without its beta traces. If yes, v2 doesn't
  require the beta flag;
- whether omp's OTel includes its auxiliary (judge, auto-thinking) calls.
  If not, the import collector keeps that narrow role;
- the exact otelcol processor for a fail-closed **allowlist** across all
  three signals (`transform` with OTTL `keep_keys`, or `redaction` with
  `allowed_keys`), and whether `otlphttp` with `encoding: json` emits
  valid OTLP JSON for logs and metrics, as well as traces;
- the stable request identifiers per harness, for `model_calls`
  uniqueness and import reconciliation (Claude `request_id`; Codex turn
  ID; omp `gen_ai.response.id`).
