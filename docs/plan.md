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
- **Extensible by the user, at the user's risk.** Support for an unknown
  harness, multiplexer or source can be added without forking, from a local
  extensions folder (`~/.config/usage-watch/extensions/`). Extensions are
  **experimental and observe-only**: they show states and data, labelled
  experimental, but never nudge a pane unless the user explicitly allows it
  for that extension. Upstream support stays unaffected.
- **Views build on what exists.** The dashboard and commands read normalised
  state. They don't talk to sources directly.
- **No credentials of any kind.** usage-watch never reads a token, a key
  or a credential file (`auth.json`, `.credentials.json`, the Keychain, the
  secret columns of omp's `auth_credentials`). Non-secret identity fields may
  be read where they sit apart from any secret: Claude's `~/.claude.json`
  `oauthAccount`, OTel attributes, or only the `identity_key` column in
  omp's table. Limits come only from what the harnesses hand out themselves.
  Prompts are never stored. Traffic interception is out of scope.
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
| 0.2 | Decide what happens to `openusage`: optional backend, or removed | Decision recorded below | done: removed (see Decisions) |
| 0.3 | Swap the old scratchpad watcher for the installed `usage-watch run` | One watcher running | owner action |
| 0.4 | Record real Claude and Codex stall screens as fixtures | Those adapters no longer rely on made-up screens | waits for a real stall |

### Phase 1: research

Read-only. Each question produces a file under `docs/research/` with its
evidence. A question with no evidence stays open.

**R1 to R8 answered 2026-09-30:** [R1](research/R1-native-otel.md),
[R2](research/R2-local-token-records.md), [R3](research/R3-capacity-sources.md),
[R4](research/R4-otel-genai-conventions.md), [R5](research/R5-pricing-sources.md),
[R6](research/R6-joins.md), [R7](research/R7-logins-and-accounts.md),
[R8](research/R8-omp-authentication.md), and [R9](research/R9-credential-free-limits.md).
R8 found a terms-of-service constraint on Claude credentials. R9 found a
credential-free way around it: Claude Code's own status line data.
[R10](research/R10-omp-extension-limits.md) found omp keeps each login's
latest limits in its database cache, about a minute fresh, readable
without any credential. R1 now includes a live capture of all three
harnesses' OTel.

| # | Question | Why |
|---|---|---|
| R1 | What native OTel does each harness emit (Claude Code, Codex, omp), and how is it switched on? Unverified for all three | Decides whether the top source exists |
| R2 | Where does each harness record per-request tokens locally? Candidates: Claude `~/.claude/projects/*.jsonl`, Codex `~/.codex/sessions/**/*.jsonl`, omp's `agent.db` | The fallback when there's no OTel, and backfill for history |
| R3 | What account-level capacity sources exist per provider? Found so far: harness login APIs, omp's `usage_history`, OpenUsage | Capacity without OpenUsage |
| R4 | What is the current state of the OTel GenAI semantic conventions (stable or experimental attributes)? | Which names are safe to adopt |
| R5 | Where does pricing data come from? OpenUsage references LiteLLM's public model-price table | Cost estimates |
| R6 | How can a usage record be joined to a pane or project: session ID to process to pane, or cwd in logs? | The joins that are the product's value |
| R7 | Where does each harness keep its logins, and how can a pane's account be told? Known leads: Claude's default home and `CLAUDE_CONFIG_DIR` homes (Keychain entry suffixed by a hash of the directory), Claude Swap's saved accounts, Claude Desktop's organizations (macOS only); Codex's `CODEX_HOME`, `~/.codex`, `~/.config/codex` and sibling `-*` homes; omp's `auth_credentials` (several logins per provider, each with an identity key). Open: which login an omp pane is using at a given moment | Several subscriptions per provider, attributed correctly |
| R8 | How does omp authenticate its accounts, and would usage-watch holding its own logins (the way omp does) be easier or more reliable than discovering the harnesses' logins? | May replace discovery in D7 and C1 |
| R9 | Does Claude Code expose its own plan limits locally, without a credential (status line input, a local cache, an OTel signal)? | Claude capacity without touching a Claude credential (R8) |
| R10 | Can an omp extension read omp's own usage and limit state at each turn, without triggering a fetch, and write a snapshot? | Per-turn readings for every account omp holds, closing the hourly gap for team plans |

### Phase 2: design

Written under `docs/design/` and agreed before building.

**D1 to D8 drafted and revised after an external review, 2026-10-01:** see
[the design index](design/README.md). Each document marks what must be
settled before the foundation build ([F]), what is only an extension point
([X]), and what is later ([L]).

| # | Task | Needs |
|---|---|---|
| D1 | Canonical model: capacity samples, usage events, cost events, agent-state samples and context events, each with source and confidence. Capacity samples are either **anchors** (a real reading from a harness: `authoritative` or `observed`) or **estimates** (interpolated from the usage stream, `estimated`), never mixed. One counting rule for cached tokens across sources | R1–R3 |
| D2 | Correlation keys and join rules, including what a failed join looks like | R6 |
| D3 | Storage: SQLite (standard library), schema versioning, retention, "last looked" markers | D1 |
| D4 | Attribute names: standard GenAI versus `usage_watch.*`; which fields may go on metrics versus traces and events | R4 |
| D5 | Privacy: no prompts, emails and account IDs hashed or dropped, redaction, what export may send | D1 |
| D6 | Collector interface: poll or push, deduplication, a watermark per source, handling disagreement | D1, D3 |
| D7 | Account registry and identity: the stable identity per provider, hashing, user labels, removed accounts, several token sources per account, confirming a token's identity whenever it changes, choosing between valid tokens, the poll budget per account | R7, D5 |
| D8 | Nudge policy: the exact evidence a nudge needs (fresh stall, account certainty, the blocking window clear by a fresh anchor or a recovery signal, no other window known to block) | D1, D2, D6, D7 |

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
| C1 | Capacity for every account discovered (D7), reading no credential of any kind (R9): a Claude **status line tap** (opt-in, reversible wrap of the user's `statusLine` command that records `rate_limits`); Claude Code's `cachedUsageUtilization`, honouring its age; transcript limit hit and reset events; omp's `usage_cache` entries for accounts omp holds (R10, about a minute fresh; `usage_history` as the hourly fallback); Codex's `rate_limits` from its session files; on-screen reset hints. Registers them in `pool.SOURCES`, which is empty until then | 0.1, 0.2, D7, R8, R9 |
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
| V4 | Calibrated capacity estimate: between anchors, estimate each pool from the usage stream, using a per-account rate learned from pairs of anchors ("the pool moved 4% while this much cost-weighted usage happened"). Shown as an estimate with its last anchor's age. A new anchor that jumps more than local usage explains flags usage elsewhere (web, phone, other machines) |

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

### Phase 10: extension by agents

Needs the adapter, collector and tap interfaces settled (D6, phase 3), so the
brief never describes an interface about to change.

| # | Task |
|---|---|
| X1 | `usage-watch extend [harness\|multiplexer\|source] [name]`: prints a self-contained brief an agent follows to add support in the user's own environment, marked experimental and the user's responsibility. Generated from the code so it can't drift. Contents: the guarantees not to break (the primer's agent section); the interface to implement; the evidence required first (recorded screens including a real stall, cited sources); the hard rules (no credentials, no prompt content, redaction, provenance, opt-in and reversible taps); the tests and the command that proves them; where files go and how to share upstream; a done checklist that `doctor` verifies |
| X2 | The local extensions folder: load at startup, label as experimental in `status` and `doctor`, observe-only unless allowed per extension |
| X3 | Discovery: when `doctor` or `status` meets something it can't handle (an agent-like process it doesn't recognise, no tmux), it says so and names the matching `extend` command |

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
| 2026-09-30 | ~~Credentials are read-only: never refreshed or written~~ Superseded below: no credentials at all |
| 2026-09-30 | Provider logic may be ported from OpenUsage (MIT) with its notice kept; its name and branding are not used |
| 2026-09-30 | `openusage` is removed as a dependency, for maintainability, now rather than with C1. Until C1's readers land, usage-watch has no capacity source: it still finds and shows stalled panes, but they wait instead of being nudged |
| 2026-09-30 | **usage-watch reads no credential of any kind** (owner decision, to stay within Anthropic's terms: R8, R9). See the principle. It also rules out an opt-in "direct read" mode |
| 2026-10-01 | Design review adopted (all 20 points; refinements on #2, #6, #9). Key outcomes: per-dimension attributions with validity classes; source observations reconciled into canonical usage events, disagreement kept; explicit token field names with a derived total input; auxiliary calls inside totals; three kinds of capacity evidence (anchor, recovery signal, estimate), estimates computed at query time and never used for nudges; per-source freshness for display and control; collector runtime separate from nudging and views; cost bases plus billing route, only `actual_billed` called spend; allowlists for ingestion and export; developer context export opt-in; `checkout_id` versus `repository_id`; reversible account aliases; no moving top-N on metrics; "last looked" with opened, last-seen and closed times; invariants as tests; nudge conditions in D8 |
| 2026-10-01 | Only one usage source per harness counts as primary (the session log) until a shared request ID links it to another, so unlinkable sources can't double count |
| 2026-10-01 | Users' agents can extend usage-watch through `usage-watch extend` and a local extensions folder. Extensions are experimental, the user's responsibility, and observe-only unless allowed (X1–X3) |
| 2026-09-30 | Capacity is anchors plus estimates: real readings from the harnesses, with the usage stream filling the gaps between them (D1, V4) |
| 2026-09-30 | Several subscriptions per provider are supported: accounts are the unit, discovered read-only from every place a login lives, with a poll budget per account (R7, D7, C1, C5) |

## Open questions

- **Billing route per request** for Claude and omp: nothing seen so far
  marks whether a request used a subscription or an API key (Codex OTel has
  `auth_mode`). Until found, those costs are `billing route unknown` and
  never shown as spend (D1).
- **Token semantics to verify per source before trusting a mapping** (D4):
  whether omp's OTel `input` includes cache reads, whether Codex's
  rollout `input_tokens` includes cached input, and whether each source's
  reasoning figure is a subset of its output.

- Codex account identity without a credential: its OTel `user.account_id`
  (seen in the R1 live capture) works when telemetry is on. Without
  telemetry it's still open; Codex writes `account_id` otherwise only in
  `auth.json`, which holds its tokens.
- **Decision required (R9):** whether `init` may wrap the user's Claude
  Code `statusLine` command. Recommended: yes, only when the user asks,
  with a preview of the change and an undo.
- Whether Team and Enterprise plans get `rate_limits` in the status line
  (R9; the docs say Pro and Max only). Until confirmed, Team plans rely on
  omp's record.

- Whether the team-plan token answers the usage endpoint when not
  recently polled (see the 0.1 finding).
