"""Forward-only schema migrations, numbered in code (D3).

A migration's SQL is frozen once released: never edit one, add the next.
The SQL is literal, not built from `usage_watch.model` constants, so a later
change to the model cannot silently rewrite an old migration.

Every column of every key and unique constraint is NOT NULL (D1 invariant
13, D3), with sentinels where a value can be absent: valid_from 0,
price_version '', field '', window 'other:unstated'.

Readings of the design that it leaves open are marked "Reading:".
"""

_CONF = "('authoritative', 'observed', 'inferred')"

MIGRATION_1 = f"""
-- Entities --------------------------------------------------------------

CREATE TABLE sessions (
    session_key        TEXT PRIMARY KEY NOT NULL,
    harness            TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    parent_session_key TEXT,
    started_at         INTEGER,
    last_seen_at       INTEGER,
    live               INTEGER NOT NULL DEFAULT 0 CHECK (live IN (0, 1)),
    cwd                TEXT,     -- D2: historical cwd, not an identity
    project            TEXT,     -- D2: display label, never a key
    CHECK (session_key = harness || ':' || session_id)
);

CREATE TABLE checkouts (
    checkout_id   TEXT PRIMARY KEY NOT NULL,
    repository_id TEXT,          -- null when there is no remote (D2)
    display_name  TEXT,
    local_path    TEXT,
    non_repo      INTEGER NOT NULL DEFAULT 0 CHECK (non_repo IN (0, 1))
);

CREATE TABLE accounts (
    account_key TEXT PRIMARY KEY NOT NULL
        CHECK (length(account_key) = 32 AND account_key NOT GLOB '*[^0-9a-f]*'),
    provider    TEXT NOT NULL,
    label       TEXT,
    plan        TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    removed_at  INTEGER
);

-- Reading: D3 gives account_aliases a unique identity, not a primary key,
-- so it gets a surrogate alias_id plus the UNIQUE constraint.
CREATE TABLE account_aliases (
    alias_id       INTEGER PRIMARY KEY,
    alias_kind     TEXT NOT NULL CHECK (alias_kind IN
        ('anthropic.account_org', 'openai.account', 'omp.identity_key', 'omp.report_account')),
    alias_hash     TEXT NOT NULL,
    account_key    TEXT NOT NULL REFERENCES accounts(account_key),
    asserted_by    TEXT NOT NULL,
    evidence       TEXT NOT NULL CHECK (evidence IN ('reported', 'co_reported')),
    confidence     TEXT NOT NULL CHECK (confidence IN {_CONF}),
    verified_by    TEXT,
    first_seen     INTEGER NOT NULL,
    last_confirmed INTEGER NOT NULL,
    revoked_at     INTEGER,
    UNIQUE (alias_kind, alias_hash, account_key, asserted_by)
);

CREATE TABLE account_merges (
    merge_id    INTEGER PRIMARY KEY,
    from_key    TEXT NOT NULL REFERENCES accounts(account_key),
    into_key    TEXT NOT NULL REFERENCES accounts(account_key),
    evidence    TEXT NOT NULL CHECK (evidence IN ('co_reported', 'user')),
    verified_by TEXT,
    created_at  INTEGER NOT NULL,
    revoked_at  INTEGER,
    CHECK (from_key <> into_key)
);
-- D7 5.: an account has at most one unrevoked outgoing merge.
CREATE UNIQUE INDEX account_merges_one_outgoing
    ON account_merges (from_key) WHERE revoked_at IS NULL;

-- Usage -------------------------------------------------------------------

CREATE TABLE usage_observations (
    observation_id           INTEGER PRIMARY KEY,
    source                   TEXT NOT NULL,
    stream_key               TEXT NOT NULL,
    source_request_key       TEXT NOT NULL,
    provider_request_key     TEXT,
    confidence               TEXT NOT NULL CHECK (confidence IN {_CONF}),
    parser_version           TEXT,
    observed_at              INTEGER NOT NULL,
    harness                  TEXT,
    provider                 TEXT,
    model                    TEXT,
    session_key              TEXT,
    uncached_input_tokens    INTEGER,
    cache_read_input_tokens  INTEGER,
    cache_write_input_tokens INTEGER,
    output_tokens            INTEGER,
    reasoning_output_tokens  INTEGER,
    native                   TEXT,   -- JSON object of the source's numeric fields only
    auxiliary                INTEGER NOT NULL DEFAULT 0 CHECK (auxiliary IN (0, 1)),
    UNIQUE (source, stream_key, source_request_key)
);
CREATE INDEX usage_observations_provider_request
    ON usage_observations (provider_request_key);

CREATE TABLE usage_events (
    usage_id                  INTEGER PRIMARY KEY,
    accounting_observation_id INTEGER NOT NULL UNIQUE
        REFERENCES usage_observations(observation_id),
    observed_at               INTEGER NOT NULL,
    session_key               TEXT,
    uncached_input_tokens     INTEGER,
    cache_read_input_tokens   INTEGER,
    cache_write_input_tokens  INTEGER,
    output_tokens             INTEGER,
    reasoning_output_tokens   INTEGER,
    auxiliary                 INTEGER NOT NULL DEFAULT 0 CHECK (auxiliary IN (0, 1)),
    reconciled_version        INTEGER NOT NULL,
    disagreement              TEXT    -- Reading: JSON; null when all linked observations agreed
);

CREATE TABLE event_observations (
    usage_id       INTEGER NOT NULL REFERENCES usage_events(usage_id),
    observation_id INTEGER NOT NULL REFERENCES usage_observations(observation_id),
    role           TEXT NOT NULL CHECK (role IN ('supporting', 'metadata')),
    field          TEXT NOT NULL,
    PRIMARY KEY (usage_id, observation_id, role, field),
    -- D1: field is '' for a supporting row, the field name for a metadata row.
    CHECK ((role = 'supporting' AND field = '') OR (role = 'metadata' AND field <> ''))
);
CREATE UNIQUE INDEX event_observations_one_supported_event
    ON event_observations (observation_id) WHERE role = 'supporting';
CREATE UNIQUE INDEX event_observations_one_metadata_source
    ON event_observations (usage_id, field) WHERE role = 'metadata';

-- Attribution ---------------------------------------------------------------

-- Reading: subject_id holds a session_key or an integer id; TEXT affinity
-- stores an integer id as its decimal text, so 5 and '5' are one subject.
CREATE TABLE attribution_evidence (
    evidence_id       INTEGER PRIMARY KEY,
    subject_kind      TEXT NOT NULL CHECK (subject_kind IN
        ('session', 'usage_event', 'capacity_sample', 'limit_event')),
    subject_id        TEXT NOT NULL,
    dimension         TEXT NOT NULL CHECK (dimension IN
        ('account', 'billing_route', 'checkout', 'repository', 'project',
         'branch', 'worktree', 'pane', 'role', 'task')),
    value             TEXT NOT NULL,
    method            TEXT NOT NULL,
    source            TEXT NOT NULL,
    confidence        TEXT NOT NULL CHECK (confidence IN {_CONF}),
    validity          TEXT NOT NULL CHECK (validity IN ('historical', 'live', 'time_bounded')),
    valid_from        INTEGER NOT NULL DEFAULT 0,   -- 0 = unbounded start
    valid_to          INTEGER,                      -- null = open end; not in the identity
    first_observed_at INTEGER NOT NULL,
    last_confirmed_at INTEGER NOT NULL,
    UNIQUE (subject_kind, subject_id, dimension, method, value, valid_from)
);

CREATE TABLE effective_attributions (
    subject_kind TEXT NOT NULL CHECK (subject_kind IN
        ('session', 'usage_event', 'capacity_sample', 'limit_event')),
    subject_id   TEXT NOT NULL,
    dimension    TEXT NOT NULL CHECK (dimension IN
        ('account', 'billing_route', 'checkout', 'repository', 'project',
         'branch', 'worktree', 'pane', 'role', 'task')),
    state        TEXT NOT NULL CHECK (state IN ('attributed', 'ambiguous', 'unattributed')),
    value        TEXT,
    confidence   TEXT CHECK (confidence IN {_CONF}),
    evidence_id  INTEGER REFERENCES attribution_evidence(evidence_id),
    note         TEXT,
    PRIMARY KEY (subject_kind, subject_id, dimension),
    -- D1: value is set only, and always, when attributed.
    CHECK ((state = 'attributed') = (value IS NOT NULL))
);

-- Capacity, limits, cost ----------------------------------------------------

CREATE TABLE capacity_samples (
    capacity_sample_id INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,
    stream_key         TEXT NOT NULL,
    "window"           TEXT NOT NULL CHECK ("window" IN ('session', 'weekly')
                           OR "window" LIKE 'weekly:_%' OR "window" LIKE 'other:_%'),
    window_seconds     INTEGER,
    used_pct           REAL CHECK (used_pct BETWEEN 0 AND 100),
    resets_at          INTEGER,
    status             TEXT NOT NULL CHECK (status IN ('ok', 'warning', 'exhausted', 'unknown')),
    confidence         TEXT NOT NULL CHECK (confidence IN {_CONF}),
    observed_at        INTEGER NOT NULL,
    UNIQUE (source, stream_key, "window", observed_at)
);

CREATE TABLE limit_events (
    limit_event_id INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,
    stream_key     TEXT NOT NULL,
    source_key     TEXT NOT NULL,
    "window"       TEXT,          -- if stated; not in the identity
    kind           TEXT NOT NULL CHECK (kind IN ('hit', 'reset')),
    resets_at      INTEGER,
    confidence     TEXT NOT NULL CHECK (confidence IN {_CONF}),
    observed_at    INTEGER NOT NULL,
    UNIQUE (source, stream_key, source_key)
);

-- Reading: D1 says cost amounts carry provenance (source, confidence), so
-- confidence and observed_at are stored alongside the D1 table's fields.
CREATE TABLE cost_events (
    scope_kind      TEXT NOT NULL CHECK (scope_kind IN ('request', 'session')),
    scope_id        TEXT NOT NULL,   -- a usage_id as decimal text, or a session_key
    source          TEXT NOT NULL,
    basis           TEXT NOT NULL CHECK (basis IN ('actual_billed', 'harness_estimate', 'list_price')),
    price_version   TEXT NOT NULL DEFAULT '',
    cost_usd_micros INTEGER NOT NULL,
    confidence      TEXT NOT NULL CHECK (confidence IN {_CONF}),
    observed_at     INTEGER NOT NULL,
    PRIMARY KEY (scope_kind, scope_id, source, basis, price_version),
    -- D1: '' for bases other than list_price; list_price names its table version.
    CHECK ((basis = 'list_price') = (price_version <> ''))
);

-- State and context -----------------------------------------------------------

CREATE TABLE state_samples (
    pane        TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    harness     TEXT,
    session_key TEXT,
    state       TEXT NOT NULL CHECK (state IN
        ('busy', 'idle', 'stalled', 'resuming', 'typing', 'unknown')),
    model       TEXT,
    reset_hint  INTEGER,
    error_key   TEXT,
    note        TEXT,              -- the adapter's fixed explanation, never screen text
    source      TEXT NOT NULL,
    confidence  TEXT NOT NULL DEFAULT 'inferred' CHECK (confidence = 'inferred'),
    PRIMARY KEY (pane, observed_at)
);

CREATE TABLE context_events (
    context_event_id INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN
        ('task_start', 'task_end', 'role', 'handoff', 'retry')),
    source           TEXT NOT NULL,
    observed_at      INTEGER NOT NULL,
    session_key      TEXT,
    pane             TEXT,
    task             TEXT,
    issue            TEXT,
    pr               TEXT,
    role             TEXT,
    work_kind        TEXT
);

-- Runtime ---------------------------------------------------------------------

CREATE TABLE watermarks (
    collector   TEXT NOT NULL,
    file        TEXT NOT NULL,
    path        TEXT NOT NULL,
    inode       INTEGER,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (collector, file)
);

-- Reading: orphan counts are derived by query (D1: link state is never
-- stored), so they are not a column here.
CREATE TABLE collector_status (
    collector       TEXT PRIMARY KEY NOT NULL,
    last_run_at     INTEGER,
    last_success_at INTEGER,
    last_new_at     INTEGER,       -- D6: when it last produced something new
    last_error      TEXT           -- field names only (D5)
);

CREATE TABLE runtime (
    runtime_id   INTEGER PRIMARY KEY,
    pid          INTEGER NOT NULL,
    started_at   INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL,
    version      TEXT NOT NULL
);

CREATE TABLE looks (
    who          TEXT NOT NULL,
    "view"       TEXT NOT NULL,
    opened_at    INTEGER,
    last_seen_at INTEGER,
    closed_at    INTEGER,
    PRIMARY KEY (who, "view")
);

-- D8 is settled before F5; these are the fields D3 and D8 name.
CREATE TABLE nudges (
    nudge_id     INTEGER PRIMARY KEY,
    stall_id     TEXT NOT NULL,
    pane         TEXT NOT NULL,
    session_key  TEXT,
    error_key    TEXT,
    action       TEXT NOT NULL CHECK (action IN ('nudge', 'wait', 'escalate')),
    reason_code  TEXT NOT NULL,
    evidence     TEXT,             -- JSON: anchor or signal, source, age, confidence, account
    decided_at   INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL, -- last evaluation of a repeated wait
    count        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE schema_version (
    id          INTEGER PRIMARY KEY CHECK (id = 1),   -- one row
    version     INTEGER NOT NULL,
    migrated_at INTEGER NOT NULL
);
"""

# Index i holds migration i + 1.
MIGRATIONS: tuple[str, ...] = (MIGRATION_1,)
