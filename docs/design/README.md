# Design

Status: **draft for review**, 2026-10-01. Nothing here is built yet. Each
document answers one design task from [the plan](../plan.md), and cites the
research it rests on.

| Doc | Decides |
|---|---|
| [D1: model](D1-model.md) | The record types, their fields and units, and how provenance is carried |
| [D2: joins](D2-joins.md) | How records are linked to sessions, panes, projects and accounts, and what a failed join looks like |
| [D3: storage](D3-storage.md) | The database, schema versioning, retention, "last looked" markers |
| [D4: naming](D4-naming.md) | Internal names versus OTel names, and which attributes may go on metrics |
| [D5: privacy](D5-privacy.md) | What is never stored, how identities are hashed, what export may send |
| [D6: collectors](D6-collectors.md) | The collector interface, each collector, deduplication, disagreement, and the first dependency |
| [D7: accounts](D7-accounts.md) | Account identity, the registry, labels, and attribution |

The shape, from source to view:

```
harness session logs ─┐
native OTel (OTLP) ───┤
harness limit data ───┼─> collectors ─> normalise + enrich ─> store ─> status / dashboard / usage / alerts / export
tmux, ps, git, workmux┘     (D6)           (D1, D2, D7)        (D3)
```

Decisions a reviewer is asked to make are marked **Decision:** in each
document.
