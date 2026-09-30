# D8: Nudge policy

Status: draft, revised after third review (2026-10-01). **Scope
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
| `stale_window_fallback` | `off` | **off until calibrated** | While `off`, a non-blocking window without a fresh anchor makes the policy wait (condition 4). It may be turned on only after calibration (below) |
| `stale_window_max_age` | 60 min | **provisional; used only when the fallback is on** | The longest display age of any nudge-eligible source (D6, `claude.cached_utilization`). Past that, every view already marks a reading stale, so the policy never trusts a reading the views would call stale. Conservative next to the old rule, which accepted an anchor of any age |
| `stale_window_min_remaining_pct` | 50 | **provisional; used only when the fallback is on** | Replaces the old "below 75% used" (25% remaining), which had no stated basis. Conservative: for a window passing this test to block, the account would have to use half a window's capacity within `stale_window_max_age`. Whether that ever happens is not yet known |

**Calibration, before the fallback may be turned on,** uses recorded
anchor history: for each account and window, the largest drop in remaining
share seen between two current-instance anchors no more than
`stale_window_max_age` apart. `stale_window_min_remaining_pct` is set to
that largest observed drop plus a safety margin. Until that history exists,
`stale_window_fallback` stays `off`.

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
- **Window discovery.** Each source, or each report where it varies by
  report, declares its window discovery, with evidence, as D7 does for
  accounts:

  | Value | Meaning |
  |---|---|
  | `complete` | the report enumerates every capacity window applicable to the account |
  | `partial` | the report may omit windows |

  For example, an omp usage-cache report listing its limits may be
  `complete`. The Claude status line gives only `five_hour` and
  `seven_day`, so it's `partial` unless verified otherwise.
- **Known applicable windows (per account).** The applicable windows in
  the account's latest `complete` report, plus those seen in anchors for
  that account (by effective attribution) within the last **8 days** (one
  weekly window plus a day). The look-back only recalls windows seen
  before; it is never proof that no other window exists.
- **The set is known** only when:
  - the account has a report from a source with `complete` window
    discovery, fresh (within that source's control age, D6) and for the
    current instance; **and**
  - no applicable window has been seen for that account, within the
    look-back, whose only anchors are from ended instances. A window seen
    before, but not in its current instance, exists and can't be
    evaluated.

  With only `partial` discovery, the set is never known. **If the set
  isn't known, the policy fails closed: it waits.**
- **Stall onset.** The `observed_at` of the first state sample showing the
  stall after the pane was last seen not stalled.
- **Stall occurrence (`stall_id`).** One stall is identified by
  `stall_id`, generated from `(session_key, or the pane when the session is
  unknown, error_key, stall onset)`. The same visible text can recur on a
  later, independent stall, so "nudge once", `wait` de-duplication,
  recovery-signal linking and limit events derived from the screen all use
  `stall_id`, never `error_key` alone. A limit event derived from the
  screen carries its `stall_id` as its source key (D3).

## A nudge requires all four [F5]

**1. The pane is stalled, freshly and specifically.**
- The screen's adapter reads `stalled`, from a state sample under 30
  seconds old, with an `error_key` identifying the limit condition.
- The input box is empty.
- **Immediately before typing, the pane is re-read**, and must still show
  the same stall occurrence (the same `stall_id`), and still be idle and
  empty.

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
  the onset of this `stall_id`**, and doesn't name a different window.
- A link is to one `stall_id`. Evidence linked to an earlier stall with
  the same `error_key` never links to a later one.
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
  by a fresh anchor, as in 3(a). **While `stale_window_fallback` is `off`
  (the default), a window without one makes the policy wait.**
- Only when the fallback is on may such a window instead be clear by a
  **stale-window anchor**: the latest anchor for that window, which
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
- Nudge twice for one `stall_id`.
- Treat an unnamed stall as `session`, or an unknown window set as clear.
- Treat `partial` window discovery as proof that no other window exists.
- Use a stale-window anchor while `stale_window_fallback` is `off`.
- Act for an `ambiguous` or `unattributed` account.
- Act when any condition can't be evaluated. It waits, and shows which
  condition is missing.

## Recording and escalation [F5]

- **Decisions are written to `nudges`**, which only the nudge policy
  writes. Each row holds the pane, the `session_key`, the error key, the
  action (`nudge`, `wait` or `escalate`), the reason code, and the evidence
  used: the anchor or signal, its source, age and confidence, and the
  effective account attribution it rests on. Each row also holds the
  `stall_id`. So "why did it nudge?" and
  "why is it waiting?" can be answered afterwards.
- **A repeated `wait` is recorded once, then updated.** When a new `wait`
  is identical to the latest row, meaning the same `(stall_id, reason
  code)`, that row's last-evaluated time and evaluation count are updated
  instead of a new row being written every polling interval. A change in
  either writes a new row.
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
