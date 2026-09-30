"""Table ownership (D3): one writer per table.

Recorded for later enforcement and tests; nothing enforces it yet.
`looks` is owned by each view, and each view writes only its own row.
"""

COLLECTOR = "collector_runtime"
NUDGE_POLICY = "nudge_policy"
VIEWS = "views"  # the dashboard and queries, each writing only its own looks row

TABLE_OWNERS: dict[str, str] = {
    "usage_observations": COLLECTOR,
    "usage_events": COLLECTOR,
    "event_observations": COLLECTOR,
    "attribution_evidence": COLLECTOR,
    "effective_attributions": COLLECTOR,
    "capacity_samples": COLLECTOR,
    "limit_events": COLLECTOR,
    "cost_events": COLLECTOR,
    "state_samples": COLLECTOR,
    "context_events": COLLECTOR,
    "sessions": COLLECTOR,
    "checkouts": COLLECTOR,
    "accounts": COLLECTOR,
    "account_aliases": COLLECTOR,
    "account_merges": COLLECTOR,
    "watermarks": COLLECTOR,
    "collector_status": COLLECTOR,
    "runtime": COLLECTOR,
    "schema_version": COLLECTOR,
    "looks": VIEWS,
    "nudges": NUDGE_POLICY,
}
