# Design

Status: **draft, revised after review**, 2026-10-01. Nothing here is built
yet. Each document answers one design task from [the plan](../plan.md), and
cites the research it rests on.

| Doc | Decides |
|---|---|
| [D1: model](D1-model.md) | Records, token fields, the three kinds of capacity evidence, cost bases, attributions, **invariants** |
| [D2: joins](D2-joins.md) | How records are attributed to sessions, checkouts, repositories, branches, panes and accounts; live versus historical validity |
| [D3: storage and runtime](D3-storage.md) | The database, the four separate runtime parts, retention, "last looked" |
| [D4: naming](D4-naming.md) | The per-source field mapping, export names, cardinality |
| [D5: privacy](D5-privacy.md) | Forbidden, developer-context and measurement data; allowlists in and out; hashing |
| [D6: collectors](D6-collectors.md) | Pull and push contracts, each source's counting rule, reconciliation, freshness |
| [D7: accounts](D7-accounts.md) | Canonical accounts, aliases, reversible merges |
| [D8: nudge policy](D8-nudge-policy.md) | The exact conditions for a nudge |

**Scope markers**, in every document:
- **[F]** must be settled before the foundation build;
- **[X]** needs only an extension point now;
- **[L]** belongs to a later phase and is sketched only.

**The shape, from source to view:**

```
session logs ─┐                                        ┌─ status / dashboard / usage
OTel (OTLP) ──┤ pull and push     write path:          │
limit data ───┼─> sources ──> allowlist ─> observations ─> reconcile ─> events ─> store ─┼─ nudge policy (D8)
tmux, git, ───┘    (D6)         (D5)                    (D6)      + attributions (D2)    └─ export (later)
workmux
```

Decisions a reviewer is asked to make are marked **Decision:** or listed
under the plan's Open questions.
