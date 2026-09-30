# Screen fixtures

Each file is a pane screen as `tmux capture-pane -p -J` prints it, with
personal details replaced. Where each came from matters, because an adapter
is only as trustworthy as the screens it was tested against.

| Fixtures | Source |
|---|---|
| `omp_stalled_*`, `omp_busy`, `omp_idle` | Real omp panes, 2026-09-30, trimmed and anonymised |
| `omp_owner_typing`, `omp_resumed_idle` | Real stalled screen, edited to show what followed on the live pane |
| `claude_busy` | Real Claude Code 2.1.285 pane, trimmed |
| `claude_idle`, `claude_typing`, `claude_idle_discussing_limits` | Derived from the real busy screen |
| `claude_stalled`, `claude_resuming`, `claude_context_limit` | **Synthetic.** Wording from the Claude Code 2.1.285 binary; layout assumed. Replace with a real capture when one happens |
| `codex_*` | **Synthetic.** Limit wording from a real Codex session log and the 0.154.0 binary; layout assumed. Replace with real captures |

Record a real screen with `usage-watch doctor --capture <pane> > tests/fixtures/<name>.txt`,
then check it for anything personal before committing.
