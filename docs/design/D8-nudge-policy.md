# D8: Nudge policy

Status: draft, revised after the second review (2026-10-01). **Scope
[F5]: settle before F5, when the watcher starts reading the store. It
doesn't need settling before F1 or F2.** Scope markers otherwise as in
[D1](D1-model.md).

A nudge types into someone's agent. A wrong one can derail it. **The
policy acts only on evidence, and when evidence is missing, it waits.**
This document is the only place nudge conditions are defined.

## The three kinds of capacity evidence (from D1)

```text
fresh anchor       = evidence of current capacity
recovery signal    = evidence that a previously observed blocking condition has expired
estimate           = display and forecast only; never used here
```

## Parameters [F5]

Every threshold is a named parameter, so it can be calibrated without
rewriting the rules. Remaining share is `100 - used_pct`.

| Parameter | Default | Status | Justification |
|---|---|---|---|
| `min_remaining_pct` | 5 ("at least 5% remaining") | inherited, not calibrated | Carried over from today's watcher ("below 95% used"). It's applied only to **fresh** anchors, so it only has to cover what can be used during one source's control age (D6, at most 15 min). No evidence yet shows 5 is enough or too much |
| `stale_window_max_age` | 60 min | **provisional** | The longest display age of any nudge-eligible source (D6, `claude.cached_utilization`). Past that, every view already marks a reading stale, so the policy never trusts a reading the views would call stale. Conservative next to the old rule, which accepted an anchor of any age |
| `stale_window_min_remaining_pct` | 50 | **provisional** | Replaces the old "below 75% used" (25% remaining), which had no stated basis. Conservative: for a window passing this test to block, the account would have to use half a window's capacity within `stale_window_max_age`. Whether that ever happens is not yet known |

**Calibrating the provisional values** uses recorded anchor history: for
each account and window, the largest drop in remaining share seen between
two current-instance anchors no more than `stale_window_max_age` apart.
`stale_window_min_remaining_pct` may be lowered only to a value above that
drop plus a margin. Until that history exists, the defaults above stand.

## Definitions [F5]

- **Effective account.** Every record's account is its **effective
  attribution** for the `account` dimension (`effective_attributions`):
  for the stalled session, for each capacity sample and for each limit
  event. A record whose effective account is `ambiguous` or
  `unattributed` is never evidence for any account.
- **Current window instance.** An anchor belongs to its window's current
  instance when its `resets_at` is still in the future at decision time.
  An anchor whose `resets_at` has passed describes an instance that has
  ended.
- **Applicable window.** A window of the pane's account that can block the
  pane's request: `session`, `weekly`, `other:<name>`, and
  `weekly:<model>` when `<model>` is the session's model. If the session's
  model is unknown, every `weekly:<model>` window is applicable.
- **Matching a model to `weekly:<model>`.** The window suffix the source
  names (e.g. `fable` from `anthropic:7d:fable` or `seven_day_fable`) is
  compared, lowercased, with the family word of the pane's model (e.g.
  "Fable 5.1" gives `fable`).
- **Known applicable windows (per account).** The applicable windows seen
  in anchors for that account (by effective attribution) within the last
  **8 days** (one weekly window plus a day). A window unseen for longer no
  longer counts as applicable.
- **The set is known** only when the account has at least one
  current-instance anchor, **and** no applicable window has been seen for
  that account, within the look-back, whose only anchors are from ended
  instances. A window seen
  before, but not in its current instance, exists and can't be evaluated.
  **If the set isn't known, the policy waits.**
- **Stall onset.** The `observed_at` of the first state sample in the
  unbroken run showing this pane stalled with this `error_key`.

## A nudge requires all four [F5]

**1. The pane is stalled, freshly and specifically.**
- The screen's adapter reads `stalled`, from a state sample under 30
  seconds old, with an `error_key` identifying the limit condition.
- The input box is empty.
- **Immediately before typing, the pane is re-read**, and must still show
  the same stall, with the same `error_key`, and still be idle and empty.

**2. The account is known well enough.** The account is always the
session's **effective** account.
- It's `attributed`, resting on `authoritative` or `observed` evidence.
- Or it's `attributed` resting on `inferred` evidence, **and** that
  harness has exactly one account for the model family in the registry,
  **and** account discovery is complete for that harness, as D7 defines
  it. Where discovery isn't complete, this fallback isn't available.
- `ambiguous` or `unattributed`: the pane waits.
- Otherwise the pane waits, with the reason shown.

**3. The blocking window is clear.**

*Which window blocks.* The window named by the stall. When the stall's
message doesn't name one, the **adapter must declare, with evidence** (a
recorded screen, a cited harness source), which window that generic
message means. **An unnamed stall never defaults to `session`.** If the
adapter can't declare it, the stall's window is **unknown**, and condition
3 is met only when the set of known applicable windows is known and
**every one of them is clear by a fresh anchor**, as in (a). Recovery
signals and stale-window anchors don't apply to an unknown window.

A known window is clear when either:
- **(a)** a **fresh anchor** (within that source's control age, D6), for
  the current instance, shows at least `min_remaining_pct` remaining and,
  where the source provides status, isn't `exhausted`; or
- **(b)** a **linked recovery signal** shows the *observed* blocking
  condition has expired:
  - a linked `resets_at` has passed; or
  - a linked harness reset notice exists.

*Linking a recovery signal to its stall.*
- A `resets_at` links only if it comes from an anchor or limit event:
  - whose effective account is the **same** as the session's;
  - for the **same window**;
  - observed **at or before stall onset**;
  - describing the window instance that was blocking: its `resets_at` is
    the **first reset after stall onset** (later than onset, with no
    earlier post-onset reset of that window shown by any such record).
- A harness reset notice (a limit event of kind `reset`) links if it's in
  the **same session** (`session_key`) as the stall, observed **after
  stall onset**, and doesn't name a different window.
- Nothing else is a recovery signal for this stall: not a `resets_at`
  from another account, window or instance.

(b) doesn't claim capacity is available now: usage elsewhere may have
refilled the window since. **It's allowed because condition 1 has just
verified the pane is still stalled on that same condition, and the linked
evidence shows that condition should have expired.** What it acts on is
narrow, "the thing that stopped this pane is over", and the policy never
generalises it.

**4. No other window is known to block, and none is unknown.**
- The set of known applicable windows must be known (see Definitions).
  "All known windows are clear" is not the same as "no other window
  exists". If the set isn't known, the policy waits.
- Every known applicable window other than the blocking one must be clear
  by either:
  - a fresh anchor, as in 3(a); or
  - a **stale-window anchor**: the latest anchor for that window, which
    - belongs to the current instance (`resets_at` in the future);
    - is no older than `stale_window_max_age`;
    - shows at least `stale_window_min_remaining_pct` remaining;
    - where the source provides status, isn't `exhausted`;

    **and** no `hit` limit event for that window and account is recorded
    since that anchor.
- An anchor older than `stale_window_max_age`, or from an ended instance,
  clears nothing.
- If a window's latest current-instance anchor shows it `exhausted`, or
  below `min_remaining_pct` remaining, only a fresh anchor or that
  window's own linked recovery signal clears it.

## What the policy never does

- Use an estimate (invariant 7 in D1).
- Use `omp.usage_history` (control age: never, D6).
- Nudge twice for one `error_key`.
- Treat an unnamed stall as `session`, or an unknown window set as clear.
- Act for an `ambiguous` or `unattributed` account.
- Act when any condition can't be evaluated. It waits, and shows which
  condition is missing.

## Recording and escalation [F5]

- **Decisions are written to `nudges`**, which only the nudge policy
  writes. Each row holds the pane, the `session_key`, the error key, the
  action (`nudge`, `wait` or `escalate`), the reason code, and the evidence
  used: the anchor or signal, its source, age and confidence, and the
  effective account attribution it rests on. So "why did it nudge?" and
  "why is it waiting?" can be answered afterwards.
- **A repeated `wait` is recorded once, then updated.** When a new `wait`
  is identical to the latest row, meaning the same `(pane, error_key,
  reason code)`, that row's last-evaluated time and evaluation count are
  updated instead of a new row being written every polling interval. A
  change in any of the three writes a new row.
- **An older schema means wait.** If the store's schema is older than the
  policy expects, it says "the collector hasn't migrated yet" and waits,
  rather than reading (D3).
- **Escalation is unchanged from today:**
  - the same stall persisting after a nudge escalates;
  - so do repeated nudges without the pane being seen busy;
  - both notify the user.

## Why this is stricter than today

Before this policy, the watcher nudged whenever `openusage` showed
capacity, with no freshness rule or attribution check. That source has
since been removed.
This policy writes down the conditions the fail-safe principle already
implied.
