# D4: Naming, mapping and cardinality

Status: draft, revised after third review (2026-10-01). Rests on
[R4](../research/R4-otel-genai-conventions.md),
[R1 and its live capture](../research/R1-native-otel.md#live-capture-2026-10-01),
[R2](../research/R2-local-token-records.md), [R6](../research/R6-joins.md),
[R7](../research/R7-logins-and-accounts.md),
[R9](../research/R9-credential-free-limits.md),
[R10](../research/R10-omp-extension-limits.md).
Scope markers as in [D1](D1-model.md).

## Internal names are ours [F]

The store uses D1's names. OTel names appear only at the receiver's inbound
mapping (C3) and at export (E1). A convention change touches those two
mappings and nothing else.

## Inbound mapping: each source's actual fields [F]

This is the **ingestion allowlist** (D5). It is complete per record type:
usage observations, sessions, capacity anchors, limit events, identity and
attribution evidence, and state samples. A source field not listed in one
of the tables below is dropped on arrival. A D1 or D2 field with no listed
source field stays null.

**The export-side allowlist is separate** (D5). A field being allowed in
here says nothing about whether it may leave the machine.

Conventions for every table:
- A field name is written exactly as the research found it. **"To confirm
  from a recorded sample"** means the research doesn't name the field:
  the collector may not read it until a recorded, redacted sample names it
  and this table is updated.
- **"Verify"** marks a mapping whose field is known but whose meaning is
  not yet checked.
- Every cell is checked against a recorded, redacted sample before it's
  trusted.
- The OTel columns apply when the OTLP receiver is built [L]. OTel is
  **secondary for counting**: its observations count only when linked to a
  primary one (D6).
- A source file's path is read only as the input to a keyed hash
  (`stream_key`, D5) and for watermarks. It isn't stored as a field.

### 1. Usage observations

Identity: `(source, stream_key, source_request_key)`. `stream_key` is
fixed when the observation is first stored. It is the `session_key`
(`"<harness>:<session_id>"`) when the source knows the session, which a
session log normally does from its file header; else a keyed hash (D5) of
the source file path (pull) or the trace ID (push, OTLP).

| D1 field | Claude transcript | Claude OTel | Codex rollout | Codex OTel | omp session | omp OTel |
|---|---|---|---|---|---|---|
| record | `type:"assistant"` lines, `message.usage` | event `claude_code.api_request` only | `event_msg` with `payload.type:"token_count"` | log `codex.sse_event` or span `session_task.turn` with `turn.id` (which one: to confirm from a recorded sample) | `type:"message"`, `message.role:"assistant"`, `message.usage` | span `chat <model>` with `gen_ai.response.id` only |
| `uncached_input_tokens` | `input_tokens` | `input_tokens` / `type=input` | `input_tokens − cached_input_tokens` (assumes input includes cached, per OpenAI convention: **verify**) | `non_cached_input` | `input` | `type=input` (whether it includes cache reads: **verify per version**) |
| `cache_read_input_tokens` | `cache_read_input_tokens` | `cache_read_tokens` / `cacheRead` | `cached_input_tokens` | `cached_input` | `cacheRead` | `cache_read_input` |
| `cache_write_input_tokens` | `cache_creation_input_tokens` | `cache_creation_tokens` / `cacheCreation` | `cache_write_input_tokens` | `cache_write_input` | `cacheWrite` | `cache_write_input` |
| `output_tokens` | `output_tokens` | `output_tokens` | `output_tokens` | `output` | `output` | `output` |
| `reasoning_output_tokens` | `output_tokens_details.thinking_tokens` | none | `reasoning_output_tokens` | `reasoning_output` | `reasoningTokens` | `reasoning_output` |
| `session_id` (feeds `session_key`) | `sessionId` | `session.id` | `session_meta.payload.id` | `conversation.id` (logs and spans; **not on metrics**) | session header `id` | `gen_ai.conversation.id` (spans; logs join through their trace ID) |
| `source_request_key` | `message.id` + `requestId` | `request_id` | the `token_count` event's timestamp + its `total_token_usage.total_tokens` (D6) | `turn.id` | the entry's `id` (uniqueness per session: confirm from a recorded sample before C2) | `gen_ai.response.id` (seen in R1's live capture) |
| `provider_request_key` (keyed hash, D5 namespace `request:<provider>`; the only cross-source link, D6) | `requestId` | `request_id` | none | none: Codex can't link by request (D6) | `responseId` (null on 608 records, R2), **only after equality with omp OTel `gen_ai.response.id` is verified** | `gen_ai.response.id`, **only after equality with omp session `responseId` is verified** |
| `model` | `message.model` (`<synthetic>` is skipped) | `model` | the preceding `turn_context.payload.model` | `model` | `message.model` | `gen_ai.request.model` |
| `provider` | no field: to confirm from a recorded sample | no field: to confirm from a recorded sample | no field: to confirm from a recorded sample | no field: to confirm from a recorded sample | `message.provider` | `gen_ai.provider.name` |
| `auxiliary` | to confirm from a recorded sample | to confirm from a recorded sample (`query_source` is a candidate, unverified) | to confirm from a recorded sample | to confirm from a recorded sample | to confirm from a recorded sample | to confirm from a recorded sample (`omp.gen_ai.agent.id` / `omp.gen_ai.agent.name` are candidates, unverified; judgment-model spans exist, R1) |
| `observed_at` | to confirm from a recorded sample | the OTLP record's time: to confirm from a recorded sample | to confirm from a recorded sample | the OTLP record's time: to confirm from a recorded sample | to confirm from a recorded sample | the OTLP record's time: to confirm from a recorded sample |
| harness cost (D1 cost event, `harness_estimate`) | none | `cost_usd` on `api_request`, plus undocumented `cost_usd_micros` | none | `codex.turn.cost_microusd` (not emitted on a ChatGPT-login run) | `cost.total` (also `cost{input, output, cacheRead, cacheWrite}`, kept in `native` only) | span `gen_ai.cost.*_usd` (0 on a subscription run). Cost **metrics** (`claude_code.cost.usage`, `omp.agent.chat.cost.estimated_usd`) have no request or session identity, so, like token metrics, they never become cost events |
| billing-route evidence | none: `unknown` | none: `unknown` | none: `unknown` | `auth_mode`: session-level evidence (table 5), not per request | none: `unknown` | none: `unknown` |

- **OTel metrics are not observations [needed before C3].** An OTLP datum
  emits a usage observation only if it yields both a `stream_key` and a
  `source_request_key`. The aggregate token metrics
  `claude_code.token.usage`, `codex.turn.token_usage` and
  `gen_ai.client.token.usage` carry no request identity, so they never
  become usage observations. They may be kept later as aggregate
  telemetry; until a table for that is added, they are dropped on arrival.
  The per-column token-field names above that come from those metrics
  (`type=input`, `token_type` values) apply only where the same names
  appear on the keyed event or span; where they don't, the keyed record's
  own field names are to confirm from a recorded sample.
- `provider_request_key` is null where the source has no provider request
  ID, or where the table says the link is not yet verified.
- `native` (D1) keeps the numeric token fields named in this table for the
  source, and nothing else.
- Claude's cache-write figure is split into 5 m and 1 h, and omp's has a
  1 h split (R2). The split's field names are to confirm from a recorded
  sample. Until then only the totals above are read.
- The Codex OTel `token_type` values above are written from R1's
  description ("non-cached input", "cached input" and so on). Their exact
  spelling is to confirm from a recorded sample. The `codex.api_request`
  and `codex.sse_event` logs also carry `input_token_count`,
  `cached_token_count`, `output_token_count`, `reasoning_token_count` and
  `tool_token_count` (R1). Whether `input_token_count` includes cached
  tokens is **verify**, so those fields feed `native` only until checked.
- Whether each source's reasoning figure is a subset of its output, or
  separate, is checked per source (D1's subset rule).
- The `provider` for Claude and Codex records has no source field in the
  research. Whether the collector may set it from the harness alone (a
  Claude Code session can run against another backend) is open.

#### When a token field is omitted

A missing field becomes 0 only when the source's documented semantics
prove omission means zero, with the evidence cited here. Otherwise it is
**null**. `total_input_tokens` is null if any component is null.

| D1 field | Omitted means | Evidence |
|---|---|---|
| `uncached_input_tokens` | **null**, every source | No research file documents omission as zero for any source (R1, R2). For the Codex rollout, null if either `input_tokens` or `cached_input_tokens` is missing |
| `cache_read_input_tokens` | **null**, every source | As above |
| `cache_write_input_tokens` | **null**, every source | As above |
| `output_tokens` | **null**, every source | As above |
| `reasoning_output_tokens` | **null**, every source | As above. omp's `reasoningTokens` is on some records only (R2), and Claude OTel has no reasoning field at all, so its value is always null |

- A Codex `token_count` event with `info:null` (97 seen, R2) produces no
  observation. It is not a zero.
- Changing any row to "zero" needs the source's documented semantics,
  cited in this table.

### 2. Sessions

| D1 field | Claude transcript | Claude OTel | Codex rollout | Codex OTel | omp session | omp OTel |
|---|---|---|---|---|---|---|
| `session_id` | `sessionId` | `session.id` | `session_meta.payload.id` | `conversation.id` | session header `id` | `gen_ai.conversation.id` |
| `cwd` | `cwd` (every assistant line) | none | `session_meta.payload.cwd`; per-turn `turn_context` `cwd` | on span `run_sampling_request`: attribute name to confirm from a recorded sample | session header `cwd` | none |
| branch | `gitBranch` | none | `session_meta.payload.git.branch` | none | none: from git at ingest, live sessions only (D2) | none |
| `parent_session_key` (from the parent's ID) | subagent files at `<sessionId>/subagents/*.jsonl` carry `isSidechain:true` and `agentId` (R2); the parent is the directory's `sessionId` | none | `session_meta.payload.source` marks subagents; the parent's ID: to confirm from a recorded sample | none | `parentSession` (on some subagent sessions) | none |
| `started_at`, `last_seen_at` | from the records' `observed_at`: to confirm from a recorded sample | as for observations | as for observations | as for observations | as for observations | as for observations |
| version | `version` | `app.version` (off by default) | `session_meta.payload.cli_version` | `app.version` | session header `version` | none |

- Codex `session_meta.payload.git.commit_hash` and
  `git.repository_url` are allowed as inputs to D2's checkout and
  repository lookups only. `originator` is not read.
- `live` comes from the topology scan, not from a record.
- Claude OTel's `vcs.*` attributes (R1: inferred, off by default) are not
  read until confirmed live.

### 3. Capacity anchors

| D1 field | `claude.statusline` (the tap's snapshot of stdin) | `claude.cached_utilization` (`~/.claude.json` `.cachedUsageUtilization`) | `omp.usage_cache` (`agent.db` `cache`, `usage_cache:report:*` values) | `codex.rollout` (`payload.rate_limits` on `token_count` events) | `omp.usage_history` [X] (`agent.db` `usage_history`) |
|---|---|---|---|---|---|
| `window` | `rate_limits.five_hour` → `session`; `rate_limits.seven_day` → `weekly` | `five_hour` → `session`; `seven_day` → `weekly`; per-model weekly windows → `weekly:<model>` (their key names: to confirm from a recorded sample) | `window.id` / `scope.windowId`, with `scope.modelId` for per-model windows (value spellings: to confirm from a recorded sample) | to confirm from a recorded sample; mapped by `window_seconds`, not by name (D1) | the window label column (name to confirm from a recorded sample; values seen: `anthropic:5h`, `anthropic:7d`, `anthropic:7d:fable`, `openai-codex:primary`, `openai-codex:secondary`) |
| `stream_key` (keyed hash, D5) | the snapshot's session | the Claude home | the usage-cache entry (one per login) | the rollout's session | the `account_key` column, keyed-hashed [X] |
| `window_seconds` | none | none | `window.durationMs` ÷ 1000 | to confirm from a recorded sample | none |
| `used_pct` | `.used_percentage` (0–100) | to confirm from a recorded sample | `amount.usedFraction` × 100 | to confirm from a recorded sample | `used_fraction` × 100 |
| `resets_at` | `.resets_at` (Unix seconds) × 1000 | to confirm from a recorded sample | `window.resetsAt` (unit: to confirm from a recorded sample) | to confirm from a recorded sample | `resets_at` (Unix ms) |
| `status` | none: `unknown` | none: `unknown` | `status` (`ok`, `warning`, `exhausted`, `unknown`) | to confirm from a recorded sample | `status` |
| `observed_at` | the tap's own timestamp | `fetchedAtMs` | `value.fetchedAt` | the event's `observed_at` (to confirm, as for observations) | the row's time column: to confirm from a recorded sample |
| account-identity input | the snapshot's `session_id` → that session's effective account | the account UUID it holds (field name: to confirm from a recorded sample); the file's `oauthAccount` (table 5) | `scope.accountId` + `scope.orgId` (hashed on read); `value.provider`; identity in `metadata` (paths: to confirm from a recorded sample) | none in the file (R7): the rollout's `session_meta.payload.id` → that session's effective account | `account_key` (R3) / `accountKey` (R10), hashed on read |

- `rate_limits.spend_limit.*` is not read.
- `amount.used`, `amount.limit`, `amount.remainingFraction` and
  `amount.unit` are not read.
- The Codex usage **endpoint** (0.1) uses `primary_window`,
  `secondary_window`, `used_percent`, `limit_window_seconds` and
  `reset_at`. The rollout's `rate_limits` may share that shape, but
  nothing in the research shows it, so none of those names is assumed.
- Every account-identity input becomes attribution evidence with
  `subject_kind = capacity_sample`. The anchor itself holds no identity.

### 4. Limit events

| D1 field | `claude.transcript` | `screen` (today's adapters) |
|---|---|---|
| record | `error:"rate_limit"` records and system notices (R9) | an adapter match on a pane |
| `stream_key` (keyed hash, D5) | the record's session | the pane's `session_key` when joined, else a keyed hash of pane ID plus pane PID, so a reused pane ID can't collide |
| `source_key` | the line's `uuid` (to confirm from a recorded sample) | the stall occurrence's `stall_id` (D8), never the `error_key` alone |
| `kind` | `hit` or `reset`, from which notice it is (the notice's field: to confirm from a recorded sample) | the adapter's classification |
| `window` | if the notice states it ("session limit") | if the message states it |
| `resets_at` | parsed from the notice text ("resets …", "continuing automatically at …") | the adapter's reset hint ("try again at 10:29 PM", R3) |
| `observed_at` | as for observations: to confirm from a recorded sample | the scan's own time |
| account-identity input | the record's `sessionId` → that session's effective account | the pane's session → its effective account |

- Notice and screen text is parsed in memory. Only `kind`, `window` and
  `resets_at` are kept, never the text.
- Account inputs become attribution evidence with
  `subject_kind = limit_event`.
- No limit-event source is known for Codex rollouts or omp sessions.
  omp's `auth_credential_blocks` is not read (it sits in the credential
  store's database, and R7 only infers its meaning).

### 5. Identity and attribution evidence inputs

Every value here is hashed on read (D5) unless marked otherwise, and
becomes an alias (D7) or an attribution-evidence row (D2).

| Evidence | Source fields | Becomes |
|---|---|---|
| Claude account and org | `~/.claude.json` `oauthAccount.accountUuid` + `oauthAccount.organizationUuid` | `anthropic.account_org` alias; `time_bounded`, `inferred` |
| Claude account and org | Claude OTel `user.account_uuid` + `organization.id` | `anthropic.account_org` alias; `historical`, `authoritative`. `user.account_id` was also seen live (R1): what it holds is to confirm from a recorded sample, so it isn't read |
| Claude transcript owner | `ownerAccountUuid` + `ownerOrganizationUuid` (on some lines) | `anthropic.account_org` alias; `historical`, `authoritative` |
| Codex account | Codex OTel `user.account_id` | `openai.account` alias; `historical`, `authoritative` |
| Codex billing route | Codex OTel `auth_mode`, on the session given by `conversation.id` | `billing_route` evidence on the session; `historical`, `authoritative`; not hashed |
| Codex plan | rollout `plan_type` (path: to confirm from a recorded sample) | the account's `plan` (not hashed) |
| omp login per request | session `message.credentialId` → `auth_credentials.id` → `auth_credentials.identity_key` | `omp.identity_key` alias; `historical`, `authoritative`. Only those two columns are selected. The login's provider column: to confirm from a recorded sample |
| omp report account | usage cache `scope.accountId` + `scope.orgId`; usage history `account_key` / `accountKey` | `omp.report_account` alias |
| Pane, from telemetry | OTel resource attribute `usage_watch.pane` (set at launch, H3) | pane evidence, `authoritative`, not hashed |
| Project, from telemetry | OTel resource attribute `usage_watch.project` | project evidence, not hashed |
| Pane, Claude | `~/.claude/sessions/<pid>.json`: `sessionId`, `tmux` (`session:@window.%pane`), `cwd`, `version`, `status`, `updatedAt` | pane evidence, `authoritative`; `updatedAt` as a liveness hint |
| Pane, omp | `~/.omp/agent/terminal-sessions/tmux-%N`: line 1 `cwd`, line 2 the session file path | pane evidence, `authoritative` |
| Pane, by open file | the pane's process (`pane_pid` and children), then its open session file (`lsof -p`, or `/proc/<pid>/fd`) | pane evidence, `observed` |
| Pane, by hook | `TMUX_PANE`, read inside the harness | pane evidence, `authoritative` |
| Harness home | the environment variables `CLAUDE_CONFIG_DIR` and `CODEX_HOME` only | which home's state file applies |
| Checkout and repository | git, from the session's `cwd`: the real path of the common directory, and the `origin` remote | `checkout_id`, `repository_id` (keyed hashes, D5) |
| Branch, live | git's current branch for the session's `cwd` | branch evidence, `live`, `inferred`; live sessions only (D2) |
| Role | workmux and topology output (`lane`, `orchestrator`, `standalone`) | role evidence, `live` [X] |

- `user.email`, omp's `email`, `access`, `refresh` and every other
  credential or email field are **not read**, or, where they arrive
  unasked in OTel, dropped on arrival (D5).
- `credential_pin` is not read (R7).

### 6. State samples

From the `screen` adapters only. Always `inferred`. The adapter outputs
are already defined in code (`src/usage_watch/adapters/`, `Reading` in
`base.py`).

| D1 field | Source |
|---|---|
| `pane` | the tmux pane ID from the scan |
| `harness` | the adapter that matched |
| `session_key` | the pane's session, through the pane evidence in table 5 |
| `state` | adapter output `state` (`busy`, `idle`, `stalled`, `resuming`, `typing`, `unknown`) |
| `model` | adapter output `model`, the model name as the screen shows it, where it has one |
| `error_key` | adapter output `error_key`, which identifies one stall |
| `reset_hint` | adapter output `reset_hint`, when the screen says the limit lifts |
| `note` | adapter output `note`, the adapter's own fixed explanation |

- Pane text is parsed in memory and never stored.

## Outbound: export [L, with the defaults fixed now]

- Export has its **own allowlist**, in D5. It is not derived from the
  inbound tables above.
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
