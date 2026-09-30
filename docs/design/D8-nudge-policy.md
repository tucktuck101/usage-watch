# D8: Nudge policy

Status: draft, added after review (2026-10-01). Needed before F5, when the
watcher starts reading the store. Scope markers as in [D1](D1-model.md).

A nudge types into someone's agent. A wrong one can derail it. **The
policy acts only on evidence, and when evidence is missing, it waits.**
This document is the only place nudge conditions are defined.

## The three kinds of capacity evidence (from D1)

```text
fresh anchor       = evidence of current capacity
recovery signal    = evidence that a previously observed blocking condition has expired
estimate           = display and forecast only; never used here
```

## A nudge requires all four [F]

**1. The pane is stalled, freshly and specifically.**
- The screen's adapter reads `stalled`, from a state sample under 30
  seconds old, with an `error_key` identifying the limit condition.
- The input box is empty.
- **Immediately before typing, the pane is re-read**, and must still show
  the same stall, with the same `error_key`, and still be idle and empty.

**2. The account is known well enough.**
- The session's account attribution (D2) is `authoritative` or `observed`.
- Or it's `inferred`, **and** that harness has exactly one account for the
  model family in the registry, so nothing else could be meant.
- Otherwise the pane waits, with the reason shown.

**3. The blocking window is clear.** It's the window named by the stall,
or `session` when the stall doesn't say. It's clear when either:
- **(a)** a **fresh anchor** (within that source's control age, D6) shows
  it below the threshold, which is 95% used by default; or
- **(b)** a **recovery signal** shows the *observed* blocking condition has
  expired:
  - the `resets_at` of the anchor or limit event that described this stall
    has passed; or
  - the harness itself reported the limit reset.

(b) doesn't claim capacity is available now: usage elsewhere may have
refilled the window since. **It's allowed because condition 1 has just
verified the pane is still stalled on that same condition, and the
evidence shows that condition should have expired.** What it acts on is
narrow, "the thing that stopped this pane is over", and the policy never
generalises it.

**4. No other window is known to block.** For every other window of the
account (e.g. `weekly`, `weekly:<model>`):
- a fresh anchor shows it below the threshold; or
- no fresh anchor exists, **and** the latest anchor, of any age, shows it
  below 75% used **and** not `exhausted`, **and** no hit event for that
  window is recorded since.

If a window's latest anchor shows it `exhausted`, or at or above the
threshold, only a fresh anchor or that window's own recovery signal clears
it.

## What the policy never does

- Use an estimate (invariant 7 in D1).
- Use `omp.usage_history` (control age: never, D6).
- Nudge twice for one `error_key`.
- Act when any condition can't be evaluated. It waits, and shows which
  condition is missing.

## Recording and escalation [F]

- **Every decision is written to `nudges`:** the pane, the error key, the
  action (`nudge`, `wait` or `escalate`), and the evidence used. Evidence
  means the anchor or signal, its source, age and confidence, and the
  account attribution. So "why did it nudge?" and "why is it waiting?" can
  be answered afterwards.
- **Escalation is unchanged from today:**
  - the same stall persisting after a nudge escalates;
  - so do repeated nudges without the pane being seen busy;
  - both notify the user.

## Why this is stricter than today

Today the watcher nudges whenever `openusage` shows capacity, with no
freshness rule or attribution check. That source has since been removed.
This policy writes down the conditions the fail-safe principle already
implied.
