# Parallel Work — When It's Safe, When It Isn't

## The rule
**Disjoint files is NOT isolation.** Two agents editing `routers/findings.py` and
`pe-dashboard-mfe/src/components` at the same time look independent, but if both read
`services/pe_config.py` or `services/session_cache.py` mid-edit, one agent can
read a threshold/shape the other is mid-way through changing. The shared layer
must be **read-only and stable** before you fan out, not just "a different file."

## Safe to parallelize (after the check below)
- `backend-builder` on `app/routers/X.py` + `frontend-builder` on an assigned
  MFE panel/theme file, when neither is adding a new `_pc.*` constant or changing
  normalisation/shared payload semantics.
- Multiple read-only investigators (`pe-analyst`, `code-reviewer`, `security-auditor`, `Explore`) at once — they never write, so there's no shared-state race. Fan these out freely.
- Independent `_test_*.py` runs across different areas.

## NOT safe to parallelize without extra care
- Any change that touches `services/pe_config.py` (new threshold) — do this FIRST, alone, get it merged/stable, THEN fan out builders that will read the new constant. Otherwise a frontend-builder and backend-builder can both "add" the same constant differently, or one reads pe_config.py before the other's addition lands.
- Any change to `_norm()` (Python) or its active React matching consumer — these
  are a lockstep pair. Never let backend and frontend builders change these independently.
- Two builders both editing the same MFE panel, `dashboardApi.ts`, or shared
  context/theme file — serialize or use worktree isolation.

## True parallelism (when file overlap risk is real)
"Different files" is not the same as "isolated." If two builders genuinely need
to work at the same time on code that might touch the same shared module, use
**git worktree isolation**: separate working directories on separate branches,
merged deliberately afterward with the gates re-run on the merged result — not
two agents editing the same working tree concurrently.

## Sequencing checklist before fanning out
1. Is a shared file (`pe_config.py`, `session_cache.py`, `_norm()`/`_normWf()` sites) part of this change? If yes, land that piece first, alone.
2. Are the builders' target files actually disjoint, including anything they might `grep`/read from the shared layer mid-edit?
3. If genuinely uncertain, don't parallelize — serialize. A wrong assumption here produces the exact "two panels disagree on the same job's status" bug class documented in CLAUDE.md.
