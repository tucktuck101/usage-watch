# Plan: local observability for agentic development

Status: agreed direction, 2026-09-30. Nothing past phase 0 is built.

## Goal

usage-watch started as quota recovery: find agents stalled on a usage limit
and nudge them when the pool refills. The goal is to answer a wider question:

> Where is my AI capacity and spend going, why is it being consumed, and is
> anything unusual happening?

Quota recovery stays as one capability of the wider tool.

## Principles

- **Three measures, never interchangeable.** *Capacity* is quota pools,
  remaining share and reset times. *Usage* is tokens, requests, cache and
  reasoning tokens. *Cost* is money, actual or estimated.
- **Every fact carries its provenance:** the source it came from, and a
  confidence of `authoritative` (the harness or provider said so),
  `observed` (read from a tool such as OpenUsage) or `inferred` (deduced,
  such as a state read from a screen). When sources disagree, the record
  shows it.
- **usage-watch's own model is the core.** OpenTelemetry is the exchange
  format, translated at the edge, so changes to the GenAI semantic
  conventions don't ripple through the code. Standard GenAI attributes are
  used where they fit, and everything project-specific sits under
  `usage_watch.*`.
- **Cardinality.** Session, pane, task and commit identifiers go on traces
  and events, never on metrics.
- **Source order.** Native OTel first, then structured logs and session
  files, then provider polling for account-level facts, then tmux, process
  and screen inspection for runtime state and enrichment. No single source
  is assumed complete; joining them up is the value.
- **Accounts, not providers, are the unit.** One person often holds several
  subscriptions per provider. Each account is identified by the provider's
  own stable ID, stored hashed and shown by a label the user chooses. The
  same account found in several places is one account with several token
  sources.
- **Polling is budgeted.** Every tool polling a token shares one
  allowance. Prefer sources that cost no requests. Poll each account at most
  every few minutes, with jitter. Back off on 429 and honour `Retry-After`.
  Every capacity reading carries its age, and decisions such as a nudge use
  only readings fresh enough to trust.
- **Hooks add context, not counts.** They say why (task, PR, role, retries),
  never how many tokens.
- **Views build on what exists.** The dashboard and commands read normalised
  state. They don't talk to sources directly.
- **Safety first.** Credentials are read-only and never refreshed, logged or
  sent anywhere but their own provider. Prompts are never stored. Traffic
  interception is out of scope unless everything else proves insufficient.
- **Standard library first.** A dependency is allowed only when it clearly
  adds value, keeps things simple, and there is no reasonable standard
  library way. The justification is recorded in the Decisions log.

## Scope

**The goal is everything below. The proof of concept is phases 0 to 3, C1,
C2, V1, V2 and A1**: capacity and usage history without OTel, breakdowns,
"since I last looked", and pool-threshold alerts. Each later view builds on
what the proof of concept stores.

## Phases

Each phase depends on the ones before it unless a row says otherwise.

### Phase 0: close out current work

| # | Task | Done when | Status |
|---|---|---|---|
| 0.1 | Endpoint check: Claude and Codex usage APIs, read-only, printing no tokens | Response shapes are recorded under `docs/research/`, or the endpoints are ruled out | done: [finding](research/0.1-endpoint-check.md). Personal Claude and Codex answer directly; the team-plan token was throttled (429) |
| 0.2 | Decide what happens to `openusage`: optional backend, or removed | Decision recorded below | open: 0.1 suggests optional, not removed, until the team-plan path works |
| 0.3 | Swap the old scratchpad watcher for the installed `usage-watch run` | One watcher running | owner action |
| 0.4 | Record real Claude and Codex stall screens as fixtures | Those adapters no longer rely on made-up screens | waits for a real stall |

### Phase 1: research

Read-only. Each question produces a file under `docs/research/` with its
evidence. A question with no evidence stays open.

| # | Question | Why |
|---|---|---|
| R1 | What native OTel does each harness emit (Claude Code, Codex, omp), and how is it switched on? Unverified for all three | Decides whether the top source exists |
| R2 | Where does each harness record per-request tokens locally? Candidates: Claude `~/.claude/projects/*.jsonl`, Codex `~/.codex/sessions/**/*.jsonl`, omp's `agent.db` | The fallback when there's no OTel, and backfill for history |
| R3 | What account-level capacity sources exist per provider? Found so far: harness login APIs, omp's `usage_history`, OpenUsage | Capacity without OpenUsage |
| R4 | What is the current state of the OTel GenAI semantic conventions (stable or experimental attributes)? | Which names are safe to adopt |
| R5 | Where does pricing data come from? OpenUsage references LiteLLM's public model-price table | Cost estimates |
| R6 | How can a usage record be joined to a pane or project: session ID to process to pane, or cwd in logs? | The joins that are the product's value |
| R7 | Where does each harness keep its logins, and how can a pane's account be told? Known leads: Claude's default home and `CLAUDE_CONFIG_DIR` homes (Keychain entry suffixed by a hash of the directory), Claude Swap's saved accounts, Claude Desktop's organizations (macOS only); Codex's `CODEX_HOME`, `~/.codex`, `~/.config/codex` and sibling `-*` homes; omp's `auth_credentials` (several logins per provider, each with an identity key). Open: which login an omp pane is using at a given moment | Several subscriptions per provider, attributed correctly |

### Phase 2: design

Written under `docs/design/` and agreed before building.

| # | Task | Needs |
|---|---|---|
| D1 | Canonical model: capacity samples, usage events, cost events, agent-state samples and context events, each with source and confidence | R1–R3 |
| D2 | Correlation keys and join rules, including what a failed join looks like | R6 |
| D3 | Storage: SQLite (standard library), schema versioning, retention, "last looked" markers | D1 |
| D4 | Attribute names: standard GenAI versus `usage_watch.*`; which fields may go on metrics versus traces and events | R4 |
| D5 | Privacy: no prompts, emails and account IDs hashed or dropped, redaction, what export may send | D1 |
| D6 | Collector interface: poll or push, deduplication, a watermark per source, handling disagreement | D1, D3 |
| D7 | Account registry and identity: the stable identity per provider, hashing, user labels, removed accounts, several token sources per account, confirming a token's identity whenever it changes, choosing between valid tokens, the poll budget per account | R7, D5 |

### Phase 3: foundation

| # | Task |
|---|---|
| F1 | The store, the model types from D1 and D3, and the account registry from D7 |
| F2 | The collector runtime: scheduling, watermarks, deduplication, writing records |
| F3 | Existing logic becomes collectors: screen states (inferred), `openusage` (observed), tmux, git and workmux enrichment |
| F4 | `status` and `dashboard` read from the store |
| F5 | The watcher reads its nudge decisions from the store, with behaviour unchanged, which the existing tests confirm |

### Phase 4: collectors

| # | Task | Needs |
|---|---|---|
| C1 | Native capacity, read-only, for every account discovered (D7): Claude and Codex logins from every home, omp `usage_history`, polling within the budget | 0.1, 0.2, D7 |
| C2 | Token usage from session logs (Claude, Codex, omp), with backfill | R2 |
| C3 | Local OTLP receiver (OTLP over HTTP with JSON, standard library) | R1 |
| C4 | Enrichment: attach project, branch, worktree, role and pane to usage records | D2 |
| C5 | Pane-to-account attribution: read only `CLAUDE_CONFIG_DIR` or `CODEX_HOME` from the harness process's environment, never other variables; for omp, per R7; config as the fallback | R7, D7 |

### Phase 5: views

| # | Task |
|---|---|
| V1 | `usage-watch usage`: breakdowns by provider, account, model, harness, project, branch, role or session, over a time range |
| V2 | "Since I last looked" |
| V3 | Dashboard panels: burn rate, time until each pool is empty, trends, peak concurrency |

### Phase 6: cost

| # | Task |
|---|---|
| K1 | A pricing table with a refresh and a pinned version |
| K2 | Cost records labelled `actual` (billed API spend) or `estimated` (tokens times price), never mixed silently |

### Phase 7: alerts

| # | Task |
|---|---|
| A1 | Pool thresholds, and burn-rate alerts ("empty before reset") |
| A2 | Anomalies: an unusually expensive session or project, or a concurrency spike |
| A3 | Alert channels (desktop notification exists; others pluggable), with quiet hours and de-duplication |

### Phase 8: hooks and context

| # | Task |
|---|---|
| H1 | `usage-watch task start` / `task end`: task, issue or PR, role, kind of work |
| H2 | Hook installers for each harness that supports lifecycle hooks |
| H3 | `usage-watch exec <harness>`: start a harness with telemetry settings and initial context |
| H4 | Views by task, PR and role, including retries and handoffs |

### Phase 9: export

| # | Task |
|---|---|
| E1 | OTLP export to external backends, applying D4's metric rules and D5's privacy rules |

### Deferred

Traffic interception or proxying. Revisit only if every other source proves
insufficient, given what it means for security and maintenance.

## Decisions

| Date | Decision |
|---|---|
| 2026-09-30 | Order: phases 0–3 first, then C1 and C2; C3 waits on R1 |
| 2026-09-30 | Plan and research findings live in this repo, under `docs/` |
| 2026-09-30 | Standard library first; a dependency needs a recorded justification (see Principles) |
| 2026-09-30 | Goal is every view listed; the proof of concept is the set in Scope |
| 2026-09-30 | Credentials are read-only: never refreshed or written |
| 2026-09-30 | Provider logic may be ported from OpenUsage (MIT) with its notice kept; its name and branding are not used |
| 2026-09-30 | Several subscriptions per provider are supported: accounts are the unit, discovered read-only from every place a login lives, with a poll budget per account (R7, D7, C1, C5) |

## Open questions

- Whether the team-plan token answers the usage endpoint when not
  recently polled (see the 0.1 finding).
- 0.2: whether `openusage` stays as an optional backend or goes.
