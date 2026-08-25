# Session Handoff — Template & Habit

## When to write one
At the end of any substantial work session (multiple files changed, a gate
script added/modified, or a bug actually fixed) — before ending the
conversation, write a handoff note so the next session doesn't have to
rediscover what happened or repeat a mistake.

## Where it goes
Create a dated file in `docs/handoffs/`: `YYYY-MM-DD-topic.md`. This path is
checked into the project so Claude, Codex, and the next developer all receive
the same measured handoff. Use `docs/handoffs/README.md` as the template.

## Required fields (all MEASURED, not remembered)
```markdown
## Session <date> — <one-line summary>

### Gates run (exact command → exact result)
- `py -3.14 _validate_js.py` → [OK] / [FAIL: ...]
- `py -3.14 _check_pe_config_refs.py` → [OK] / [FAIL: ...]
- `py -3.14 _test_X.py` → pass/fail, quote the actual output line

### What shipped
- File-by-file list of what changed and why (one line each)

### What was proven vs. assumed
- Explicitly separate "I ran the gate and saw X" from "this should work but
  wasn't exercised" — do not blur the two.

### What remains / known gaps
- Anything deferred, any manual smoke test not done, any TODO

### Lessons (only if genuinely new)
- A new gotcha, a stale-docs correction, a mistake caught — the kind of thing
  that belongs in CLAUDE.md's Hard-Won Gotchas if it's likely to recur
```

## Rule
Never write "tests passed" or "verified" in a handoff unless you have the
actual terminal output in front of you from THIS session. If a gate wasn't run,
write "not run this session" — that's more useful to the next session than a
false green.

## Bridge note: these agent files are also live subagents in VS Code Copilot
Everything under `.claude/agents/*.md` was written for Claude Code, but VS Code
Copilot Chat also discovers and exposes these exact files as invokable
subagents (dispatched via its own agent-runner — same `name`/`description`/
`tools` frontmatter, same system-prompt body, no translation needed). That
means the scaffold isn't Claude-Code-only: whichever assistant is working this
repo can and should actually dispatch to `backend-builder`/`frontend-builder`
for the edit and `verifier` for the adversarial pass, rather than doing
everything directly in the main thread and only running the gates at the end.

This was learned the hard way: the scaffold existed from 2026-07-25 onward but
went unused for three full sessions of real work (see the 2026-07-26 handoff
entry in `/memories/repo/PE_Dashboard.md`) — the gates still passed every time,
but a scaffold that exists and isn't invoked doesn't add anything. Each
subagent dispatch is stateless by construction (no accumulated bias from the
calling conversation), so "fresh context per task" and "parallel fan-out for
independent read-only work" (per `parallel-work.md`) are automatic once you
actually use the dispatch mechanism — not something to remember to simulate.
