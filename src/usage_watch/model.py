"""The model (D1): record and entity types, as standard-library dataclasses.

Units (D1): times are UTC milliseconds (int); money is `*_usd_micros` (int);
percentages are 0-100 (float); tokens are int. Absent is None, never 0.

These types carry no behaviour beyond D1's derived values and null
arithmetic. Storage lives in `usage_watch.store`; the column names there
match the field names here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, get_args

# --- Enumerations (D1, D2, D3, D7, D8) ---------------------------------------

Confidence = Literal["authoritative", "observed", "inferred"]
CONFIDENCES: tuple[str, ...] = get_args(Confidence)
# The order is authoritative > observed > inferred (D1).
CONFIDENCE_RANK: dict[str, int] = {"authoritative": 3, "observed": 2, "inferred": 1}

Validity = Literal["historical", "live", "time_bounded"]
VALIDITIES: tuple[str, ...] = get_args(Validity)

AttributionState = Literal["attributed", "ambiguous", "unattributed"]
ATTRIBUTION_STATES: tuple[str, ...] = get_args(AttributionState)

SubjectKind = Literal["session", "usage_event", "capacity_sample", "limit_event"]
SUBJECT_KINDS: tuple[str, ...] = get_args(SubjectKind)

Dimension = Literal[
    "account", "billing_route", "checkout", "repository", "project",
    "branch", "worktree", "pane", "role", "task",
]
DIMENSIONS: tuple[str, ...] = get_args(Dimension)

# event_observations roles. There is no "accounting" role: that link is
# usage_events.accounting_observation_id (D1).
EventObservationRole = Literal["supporting", "metadata"]
EVENT_OBSERVATION_ROLES: tuple[str, ...] = get_args(EventObservationRole)

# Derived link state of an observation; never stored (D1).
LinkState = Literal["primary", "linked", "orphan"]
LINK_STATES: tuple[str, ...] = get_args(LinkState)

BillingRoute = Literal["api_key", "subscription", "unknown"]
BILLING_ROUTES: tuple[str, ...] = get_args(BillingRoute)

CostBasis = Literal["actual_billed", "harness_estimate", "list_price"]
COST_BASES: tuple[str, ...] = get_args(CostBasis)

CostScopeKind = Literal["request", "session"]
COST_SCOPE_KINDS: tuple[str, ...] = get_args(CostScopeKind)

CapacityStatus = Literal["ok", "warning", "exhausted", "unknown"]
CAPACITY_STATUSES: tuple[str, ...] = get_args(CapacityStatus)

LimitKind = Literal["hit", "reset"]
LIMIT_KINDS: tuple[str, ...] = get_args(LimitKind)

AgentState = Literal["busy", "idle", "stalled", "resuming", "typing", "unknown"]
AGENT_STATES: tuple[str, ...] = get_args(AgentState)

ContextKind = Literal["task_start", "task_end", "role", "handoff", "retry"]
CONTEXT_KINDS: tuple[str, ...] = get_args(ContextKind)

AliasKind = Literal[
    "anthropic.account_org", "openai.account", "omp.identity_key", "omp.report_account",
]
ALIAS_KINDS: tuple[str, ...] = get_args(AliasKind)

AliasEvidence = Literal["reported", "co_reported"]
ALIAS_EVIDENCES: tuple[str, ...] = get_args(AliasEvidence)

# Evidence on an account_merges row: co_reported (verified) or user (D7).
# same_identifier needs no merge (D7 2.), so it is not a merge evidence value.
MergeEvidence = Literal["co_reported", "user"]
MERGE_EVIDENCES: tuple[str, ...] = get_args(MergeEvidence)

NudgeAction = Literal["nudge", "wait", "escalate"]
NUDGE_ACTIONS: tuple[str, ...] = get_args(NudgeAction)

# Sentinels that keep identity columns NOT NULL (D3).
UNBOUNDED_START = 0          # attribution_evidence.valid_from
NO_PRICE_VERSION = ""        # cost_events.price_version, bases other than list_price
SUPPORTING_FIELD = ""        # event_observations.field on a supporting row
UNSTATED_WINDOW = "other:unstated"  # capacity_samples.window when the source names none

# Codex primary/secondary map by window length, not name (D1).
WINDOW_SECONDS = {"session": 18000, "weekly": 604800}


def session_key(harness: str, session_id: str) -> str:
    """`"<harness>:<session_id>"`, the only way a record refers to a session (D1)."""
    return f"{harness}:{session_id}"


# --- Null arithmetic (D1) -----------------------------------------------------

@dataclass(frozen=True)
class PartialSum:
    """A sum over values some of which are unknown: the known sum plus the
    count of unknowns. Never presented as a complete total (D1)."""

    known: int | float
    unknown: int

    @property
    def complete(self) -> bool:
        return self.unknown == 0


def partial_sum(values: Iterable[int | float | None]) -> PartialSum:
    known: int | float = 0
    unknown = 0
    for v in values:
        if v is None:
            unknown += 1
        else:
            known += v
    return PartialSum(known, unknown)


# --- Usage --------------------------------------------------------------------

@dataclass(frozen=True)
class TokenFields:
    """D1's token fields, shared by observations and events. `output_tokens`
    includes reasoning; `reasoning_output_tokens` is its subset.
    `total_input_tokens` is derived. Subclasses are keyword-only."""

    uncached_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    @property
    def total_input_tokens(self) -> int | None:
        """uncached + cache_read + cache_write; None if any is None. Never stored."""
        parts = (self.uncached_input_tokens, self.cache_read_input_tokens, self.cache_write_input_tokens)
        if any(p is None for p in parts):
            return None
        return sum(parts)


@dataclass(frozen=True, kw_only=True)
class UsageObservation(TokenFields):
    """What one source said about one request (D1).
    Identity: (source, stream_key, source_request_key)."""

    source: str
    stream_key: str
    source_request_key: str
    confidence: Confidence
    observed_at: int
    parser_version: str | None = None
    provider_request_key: str | None = None
    harness: str | None = None
    provider: str | None = None
    model: str | None = None
    session_key: str | None = None
    native: str | None = None          # JSON object of the source's numeric fields only
    auxiliary: bool = False
    observation_id: int | None = None  # surrogate, set by the store


@dataclass(frozen=True, kw_only=True)
class UsageEvent(TokenFields):
    """The canonical counting event for one request (D1). Its tokens and
    `auxiliary` come atomically from its single accounting observation."""

    accounting_observation_id: int
    observed_at: int                    # copied from the accounting observation; immutable
    session_key: str | None             # copied from the accounting observation; immutable
    reconciled_version: int
    auxiliary: bool = False
    disagreement: str | None = None     # JSON; None when every linked observation agreed
    usage_id: int | None = None


@dataclass(frozen=True)
class EventObservation:
    """A supporting or metadata link from an event to another observation (D1).
    `field` is the metadata field name, or '' for a supporting row."""

    usage_id: int
    observation_id: int
    role: EventObservationRole
    field: str = SUPPORTING_FIELD


# --- Attribution --------------------------------------------------------------

@dataclass(frozen=True)
class AttributionEvidence:
    """One piece of attribution evidence (D1, D2). Identity:
    (subject_kind, subject_id, dimension, method, value, valid_from).
    `subject_id` is a session_key, or an integer id as decimal text."""

    subject_kind: SubjectKind
    subject_id: str
    dimension: Dimension
    value: str
    method: str
    source: str
    confidence: Confidence
    validity: Validity
    first_observed_at: int
    last_confirmed_at: int
    valid_from: int = UNBOUNDED_START
    valid_to: int | None = None
    evidence_id: int | None = None


@dataclass(frozen=True)
class EffectiveAttribution:
    """The resolved attribution; at most one per (subject_kind, subject_id, dimension)."""

    subject_kind: SubjectKind
    subject_id: str
    dimension: Dimension
    state: AttributionState
    value: str | None = None           # set only when attributed
    confidence: Confidence | None = None
    evidence_id: int | None = None
    note: str | None = None            # why, when unattributed or ambiguous


# --- Capacity, limits, cost, state, context ----------------------------------

@dataclass(frozen=True)
class CapacitySample:
    """An anchor (D1). Identity: (source, stream_key, window, observed_at).
    Account comes through effective attribution, never a field."""

    source: str
    stream_key: str
    window: str                         # session | weekly | weekly:<model> | other:<name>
    confidence: Confidence
    observed_at: int
    status: CapacityStatus = "unknown"
    used_pct: float | None = None
    window_seconds: int | None = None
    resets_at: int | None = None
    capacity_sample_id: int | None = None


@dataclass(frozen=True)
class LimitEvent:
    """A harness's own statement that a limit was hit or reset (D1).
    Identity: (source, stream_key, source_key); window is not part of it."""

    source: str
    stream_key: str
    source_key: str
    kind: LimitKind
    confidence: Confidence
    observed_at: int
    window: str | None = None
    resets_at: int | None = None
    limit_event_id: int | None = None


@dataclass(frozen=True)
class CostEvent:
    """Identity: (scope_kind, scope_id, source, basis, price_version).
    `price_version` is '' for bases other than list_price."""

    scope_kind: CostScopeKind
    scope_id: str                       # a usage_id as decimal text, or a session_key
    source: str
    basis: CostBasis
    cost_usd_micros: int
    confidence: Confidence
    observed_at: int
    price_version: str = NO_PRICE_VERSION


@dataclass(frozen=True)
class AgentStateSample:
    """A screen reading of one pane (D1). Always inferred. Key: (pane, observed_at)."""

    pane: str
    observed_at: int
    state: AgentState
    source: str
    harness: str | None = None
    session_key: str | None = None
    model: str | None = None
    reset_hint: int | None = None       # UTC ms
    error_key: str | None = None
    note: str | None = None             # the adapter's fixed explanation, never screen text
    confidence: Confidence = "inferred"


@dataclass(frozen=True)
class ContextEvent:
    """Developer context [X] (D1)."""

    kind: ContextKind
    source: str
    observed_at: int
    session_key: str | None = None
    pane: str | None = None
    task: str | None = None
    issue: str | None = None
    pr: str | None = None
    role: str | None = None
    work_kind: str | None = None
    context_event_id: int | None = None


# --- Entities -----------------------------------------------------------------

@dataclass(frozen=True)
class Session:
    session_key: str
    harness: str
    session_id: str
    parent_session_key: str | None = None
    started_at: int | None = None
    last_seen_at: int | None = None
    live: bool = False
    cwd: str | None = None              # D2: the session's historical cwd, not an identity
    project: str | None = None          # D2: display label, never a key


@dataclass(frozen=True)
class Checkout:
    checkout_id: str
    repository_id: str | None = None
    display_name: str | None = None
    local_path: str | None = None
    non_repo: bool = False


@dataclass(frozen=True)
class Account:
    account_key: str                    # random 128-bit hex, never derived from an alias
    provider: str
    first_seen: int
    last_seen: int
    label: str | None = None
    plan: str | None = None
    removed_at: int | None = None


@dataclass(frozen=True)
class AccountAlias:
    """Identity: (alias_kind, alias_hash, account_key, asserted_by)."""

    alias_kind: AliasKind
    alias_hash: str
    account_key: str
    asserted_by: str
    evidence: AliasEvidence
    confidence: Confidence
    first_seen: int
    last_confirmed: int
    verified_by: str | None = None
    revoked_at: int | None = None
    alias_id: int | None = None


@dataclass(frozen=True)
class AccountMerge:
    from_key: str
    into_key: str
    evidence: MergeEvidence
    created_at: int
    verified_by: str | None = None
    revoked_at: int | None = None
    merge_id: int | None = None
