# D1: The model

Status: draft, revised after second review (2026-10-01). Rests on
[R1](../research/R1-native-otel.md), [R2](../research/R2-local-token-records.md),
[R3](../research/R3-capacity-sources.md), [R9](../research/R9-credential-free-limits.md),
[R10](../research/R10-omp-extension-limits.md).

Scope markers used in every design document:
- **[F]** must be settled before the foundation build (F1, F2);
- **[X]** needs only an extension point now;
- **[L]** belongs to a later phase and is sketched only.

D8 is **[F5]**: settled before F5, not before F1 or F2.

## Provenance, at two levels [F]

1. **Measured facts carry the record's own provenance.** Token counts,
   capacity percentages and cost amounts carry:
   - `source`, the collector, e.g. `claude.transcript`, `omp.usage_cache`;
   - `confidence`:
     - `authoritative`: the harness or provider stated it;
     - `observed`: read from a copy another tool kept;
     - `inferred`: deduced, e.g. a state read from a screen.

   The order is `authoritative` > `observed` > `inferred`.
2. **Enrichment carries its own provenance**, separately, as
   **attribution evidence**, resolved into one **effective attribution**
   per subject and dimension (below). Project, account, branch, pane and
   the other enriched dimensions are never stored as plain columns on a
   usage record, capacity sample or limit event (D2).

**Authoritative token counts never make an inferred attribution look
authoritative.** A query can therefore say "94% of usage attributed to an
account authoritatively, 4% inferred, 2% unattributed".

## Units and conventions [F]

- Times: UTC milliseconds, integers.
- Money: USD micro-units, integers (`…_usd_micros`).
- Percentages: 0–100, floats.
- Tokens: integers.
- **Absent is null, never 0** (see null arithmetic).
- Identities are hashed keys (D5, D7). Raw emails and IDs are never stored.
- **`session_key`** = `"<harness>:<session_id>"`, e.g. `"claude:1d73…"`.
  It is the only way any record refers to a session. The raw `session_id`
  is a field of the session entity only. Session IDs are random harness
  identifiers, not personal data. Where a record's session is unknown,
  `session_key` is null.

## Null arithmetic [F]

- A missing source field becomes **0 only when that source's documented
  semantics prove omission means zero**. The collector's mapping (D4) says
  so per field, with the evidence. Otherwise it is **null**.
- **`total_input_tokens` is null if any of its components is null.**
- **Aggregates:** a sum over records where some values are null reports
  the **known sum plus the count of records with unknowns**. It is never
  presented as a complete total.

## Token fields [F]

Explicit names, so no field means something surprising:

| Field | Meaning |
|---|---|
| `uncached_input_tokens` | input not served from cache |
| `cache_read_input_tokens` | input served from cache |
| `cache_write_input_tokens` | input written to cache |
| `output_tokens` | all output, **including** reasoning |
| `reasoning_output_tokens` | the reasoning part of `output_tokens`, a subset, never added to it |
| **`total_input_tokens`** | **derived**: `uncached + cache_read + cache_write`, null if any component is null. Never stored, always computed |

- Each collector maps its source's fields into these, with the rule tested
  against a recorded sample (D4 holds the per-source mapping).
- Where a source reports reasoning *separately* from output, the collector
  adds it into `output_tokens` so the subset rule holds. Whether each
  source does this is verified per source, not assumed.
- Each observation also keeps its source's **native numeric token fields**,
  as a small JSON object of numbers only, so normalisation can be re-run
  if a harness changes its meaning. No text is kept.

## Capacity evidence: three kinds, never confused [F]

| Kind | What it proves | Used for |
|---|---|---|
| **Anchor** | Current capacity, as of `observed_at`: a real reading from a harness (status line, omp's cache, Codex's `rate_limits`, Claude's cache) | views, alerts, **and nudges** when fresh enough (D8) |
| **Recovery signal** | That a *previously observed* blocking condition has expired: an anchor's `resets_at` has passed, or a harness notice says the limit reset. It does **not** prove capacity is available now, because usage elsewhere may have happened since | nudges, under the conditions in D8 |
| **Estimate** | A forecast from the usage stream between anchors (V4) | views and alerts **only, never a nudge** |

- **Estimates are computed at query time and never stored.** A change to
  the estimator can't leave stale numbers behind.
- Whenever one is shown, it comes with its estimator version, the anchor
  it starts from, the calibration's age, and an error range where one can
  be computed. [L]

## Records

### Capacity sample (anchors only) [F]

Its account comes through **effective attribution** (`subject_kind`
`capacity_sample`, dimension `account`), not a plain field. The nudge
policy and views read the effective account.

| Field | Meaning |
|---|---|
| `capacity_sample_id` | surrogate integer, used for references |
| `stream_key` | what the source reports on, keyed-hashed (D5): the session for the status line and Codex `rate_limits`, one omp usage-cache entry per login, the Claude home for `cachedUsageUtilization` |
| `window` | `session` (5 h), `weekly` (7 d), `weekly:<model>`, or `other:<name>` |
| `window_seconds` | when known. Codex's `primary`/`secondary` map to `session`/`weekly` by this value (18000, 604800), not by name |
| `used_pct` | 0–100 |
| `resets_at` | when the window resets |
| `status` | `ok`, `warning`, `exhausted` or `unknown` |
| `source`, `confidence`, `observed_at` | provenance |

**Identity:** `(source, stream_key, window, observed_at)`. No record key
includes `account`.

### Limit event [F]

A harness's own statement that a limit was **hit** or has **reset**: Claude
transcript notices, screen messages. Fields: `limit_event_id` (surrogate
integer), `stream_key` (keyed-hashed, D5: the session, for transcript
notices), `source_key` (the source's own key for the notice), `window` if
stated, `kind` (`hit` or `reset`), `resets_at` if stated, and provenance.
**Identity:** `(source, stream_key, source_key)`. Its account comes through **effective attribution**
(`subject_kind` `limit_event`, dimension `account`); the nudge policy and
views read the effective account. A `reset` event is a recovery signal.

### Usage observation [F]

What **one source** said about **one request**:
- `observation_id`: surrogate integer, used for references;
- **identity:** `(source, stream_key, source_request_key)`:
  - `stream_key` is fixed when the observation is first stored, and never
    changes. For a pull source it is the `session_key`, which is normally
    known from the file header before any record (Claude path, Codex
    `session_meta`, omp session header), else a keyed hash of the source
    file path (D5), which is rare. For a push source (OTLP) it is the
    `session_key` when known, else a keyed hash of the trace ID;
  - `source_request_key` is whatever that source uses, scoped to its
    stream. **A collector that can't produce a stable key per stream may
    not emit observations;**
- `parser_version`, `observed_at`;
- `harness`, `provider`, `model`, `session_key`;
- the token fields above, plus `native` (the source's numeric fields);
- `auxiliary`: set for a harness's own side calls, such as omp's judgment
  model;
- `link_state`: `primary`, `linked` or `orphan` (below, D6).

**Within one source, repeats collapse before they become an observation.**
Claude writes a request once per content block while streaming, with
`output_tokens` rising. Those are partial snapshots of one fact, not
disagreeing evidence. The source's counting rule (D6) keeps the final
snapshot as the observation.

### Usage event (canonical) [F]

What usage-watch **counts** for one request:
- `usage_id`: surrogate integer;
- the token fields and `auxiliary`, taken **atomically from the single
  accounting observation**: as one unit, never mixed field by field with
  another observation's;
- `disagreement`: null when every linked observation agreed, otherwise the
  largest per-field difference across **all** linked `supporting`
  observations, e.g. transcript 12,400 versus OTel 12,402;
- `billing_route` (`api_key`, `subscription` or `unknown`) is not a stored
  field. It is the event's effective attribution for dimension
  `billing_route`, normally inherited from its session: Codex OTel
  `auth_mode` gives session-level evidence (D2). With no evidence it is
  `unknown`. Claude and omp are open, see the plan.

**Link to observations:** `event_observations(usage_id, observation_id,
role, field)`. `field` is never null: the field name for a `metadata` row,
`''` otherwise.

| Role | Meaning |
|---|---|
| `accounting` | exactly one per event: the primary observation the token fields come from |
| `supporting` | a linked observation from another source, kept as evidence |
| `metadata` | supplied one named request-level metadata field the accounting observation lacks; the field name is recorded |

**Primary, secondary, unlinked:**
- For each harness, **one source is primary for counting**: its session
  log (`claude.transcript`, `codex.rollout`, `omp.session`). OTel stays a
  preferred telemetry source for other facts, but is **secondary for
  counting**.
- A **primary observation** creates, or is, the canonical counting event,
  and is its `accounting` link.
- A **linked secondary observation** (shares a provider request ID with a
  primary one, e.g. Claude `requestId` = OTel `request_id`) is
  `supporting` evidence. Differences go into `disagreement`.
- An **unlinked secondary observation** is stored as evidence and **never
  counts**. It may link later if a matching primary observation arrives.
  Orphans per source are shown in `doctor` and `collector_status`.
- **Request-level metadata** the accounting observation lacks may come
  from a linked observation, when a source has it, recorded as a
  `metadata` link with the field name. **Token fields never come this
  way.** Session-level facts, such as Codex's billing route, are
  attribution evidence instead.
- If a harness's primary source is unavailable (e.g. OTel but no readable
  session log), that harness is **not counted** and is reported as a
  coverage gap. A secondary source is never promoted automatically;
  promotion is an explicit per-harness config choice.

Reconciliation rules are versioned. Re-running them over stored
observations rebuilds usage events and their links without losing
evidence.

### Attribution evidence [F]

Table `attribution_evidence`. Every piece of evidence is kept:
- `evidence_id`;
- `subject_kind`: `session`, `usage_event`, `capacity_sample` or
  `limit_event`; and `subject_id`;
- `dimension`: `account`, `billing_route`, `checkout`, `repository`,
  `project`, `branch`, `worktree`, `pane`, `role`, `task`;
- `value`, `method`, `source`, `confidence`;
- `validity`: `historical`, `live` or `time_bounded` (D2);
- `valid_from`, `valid_to`: both nullable, the span in which the evidence
  holds. For `live` evidence, `valid_from = first_observed_at` and
  `valid_to = last_confirmed_at`, closed when no longer confirmed;
- `first_observed_at`, `last_confirmed_at`.

**Identity:** `(subject_kind, subject_id, dimension, method, value,
valid_from)`. Seeing the same evidence again updates `last_confirmed_at`,
and adds no row. A new value from the same method is a new row, and the
previous row's `valid_to` is closed.

### Effective attribution [F]

Table `effective_attributions`: **exactly one row per `(subject_kind,
subject_id, dimension)` for a subject with its own evidence** for that
dimension, recomputed whenever the evidence changes. Inherited values
(below) are computed at query time, not stored.
- `state`: `attributed`, `ambiguous` or `unattributed`;
- `value`: set only when `attributed`;
- `confidence` and `evidence_id`: what the row rests on.

**Resolution rule:**
1. Only evidence valid at the subject's time counts: for a usage event,
   its `observed_at`; for a session, its span; for a capacity sample or
   limit event, its `observed_at`.
2. Take the highest confidence present (`authoritative` > `observed` >
   `inferred`).
3. If every value at that confidence agrees, the state is `attributed`.
4. If they disagree, the state is `ambiguous`: never a guess, and never a
   fall back to a lower confidence.
5. No evidence means `unattributed`.

**Inheritance:** a usage event's effective attribution for a dimension is
its own if it has any evidence for that dimension, else its session's.
Most dimensions attach to the session; a usage event has its own evidence
only where it differs, e.g. a branch recorded per request.

**Each counting event counts once:** under its single effective value, or
under `ambiguous` or `unattributed`. Conflicting authoritative accounts
never make an event appear twice.

### Cost event [F: shape; L: pricing]

| Field | Meaning |
|---|---|
| `scope_kind`, `scope_id` | `request` (a `usage_id`) or `session` (a `session_key`) |
| `source` | who produced the figure |
| `basis` | `actual_billed` (a real bill), `harness_estimate` (the harness's own figure), or `list_price` (usage-watch: tokens × a public price table) |
| `price_version` | for `list_price`: the table's version and entry |
| `cost_usd_micros` | the amount |

**Identity:** `(scope_kind, scope_id, source, basis, price_version)`.
Revised estimates sit alongside older ones. Views use the newest
`price_version` unless asked otherwise.

**What a figure is called depends on its basis and the request's billing
route:**

| Basis | Billing route | Shown as |
|---|---|---|
| `actual_billed` | any | **Spend**. The only thing ever called spend |
| `list_price` or `harness_estimate` | `api_key` | Estimated bill |
| `list_price` or `harness_estimate` | `subscription` | API list-price equivalent |
| `list_price` or `harness_estimate` | `unknown` | List-price value, billing route unknown |

**No double counting:** a session's cost is the sum of its request-level
costs when any exist. A session-level figure is used only for sessions with
no request-level costs, and the two are never added.

### Agent-state sample [F]

`pane`, `harness`, `session_key`, `state` (`busy`, `idle`, `stalled`,
`resuming`, `typing`, `unknown`), `model`, `reset_hint`, `error_key`, and
`note` (the adapter's own fixed explanation, never screen text).
Always `inferred`. Short retention (D3).

### Context event [X]

`kind` (`task_start`, `task_end`, `role`, `handoff`, `retry`), `session_key`,
`pane`, and user-supplied `task`, `issue`, `pr`, `role`, `work_kind`. This
is developer-context data (D5). Defined now so phase 8 adds rows, not a new
model.

## Entities [F]

- **Session:**
  - `session_key` (identity), with `harness` and the raw `session_id` as
    its fields; `parent_session_key`;
  - `started_at`, `last_seen_at`, `live` (seen in a running harness
    process during this collection pass, D2);
  - its attribution evidence and effective attributions.
- **Checkout and repository:** D2.
- **Account and aliases:** D7.
- **Pane:** a live tmux fact only, never an entity.

## Invariants [F]

These become tests, and later drift detectors when a harness changes its
formats.

1. **Totals partition:** for any dimension, total = sum of attributed
   values + ambiguous + unattributed. Each counting event appears exactly
   once.
2. **Grouping never changes the total**, unless a view applies a named
   filter.
3. **Auxiliary calls are inside the total:** provider total = primary +
   auxiliary.
4. **Input composition:** `total_input_tokens = uncached + cache_read +
   cache_write` when all are known, **null otherwise**. And
   `reasoning_output_tokens ≤ output_tokens` when both are known.
5. **Accounting identity:** exactly one counting event per distinct
   primary observation key. usage-watch counts only what its primary
   sources recorded. It doesn't claim knowledge of requests no primary
   source saw, and coverage gaps are reported.
6. **Cost bases never mix.** Only `actual_billed` is called spend.
7. **No estimate authorises a nudge.**
8. **Nothing unknown becomes zero**, per the null arithmetic above.
9. **Unlinked secondary observations never affect canonical usage
   totals.**

## Not in the model

Prompt and response content, tool inputs and outputs, file contents, raw
OTLP bodies, credentials of any kind (D5).
