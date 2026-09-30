# Prototype assumptions

Written while the owner was away (2026-10-01), to build the proof of
concept without blocking on decisions. **Each entry is an assumption the
owner should confirm or overturn.** Entries are added as the work goes;
none is a settled decision until the owner says so.

| # | Assumption | Why | Where it bites if wrong |
|---|---|---|---|
| A1 | D8's defaults are adopted as written: `min_remaining_pct = 5`, `stale_window_fallback = off`, `stale_window_max_age = 60 min`, `stale_window_min_remaining_pct = 50` (unused while the fallback is off) | Conservative. With the fallback off, the policy waits rather than guessing | The nudge policy (F5) may wait more often than you'd like |
| A2 | The proof-of-concept scope is the plan's: F4 and F5, C1 (capacity from credential-free sources), C2 (usage from session logs), V1 (usage breakdown), V2 ("since I last looked"), A1 (pool alerts). The OTLP receiver (C3), cost pricing (K), hooks (H) and export (E) are out | The plan's Scope section | Nothing |
| A3 | The Claude status line tap is **built but not installed**. `init --claude-statusline` shows the change and only applies it on confirmation. The prototype reads Claude capacity from `~/.claude.json` `cachedUsageUtilization` in the meantime | Installing it edits your Claude Code settings. That's yours to approve (an open decision in the plan) | Claude capacity is only as fresh as the last time `/usage` was opened, until the tap is installed |
| A4 | No background process is started on your machine. `run` works, but you start it | Claude Code's own permission checks block detached processes, and starting one is your call | Nothing collects until you run it |
| A5 | Collectors read only fields confirmed from real files on this machine (key names only, never content). Any field still unconfirmed is left out, and listed here | D4's rule: "to confirm from a recorded sample" | Some columns stay null in the prototype |
