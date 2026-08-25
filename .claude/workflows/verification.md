# Verification — Batching and Trust Rules

## Agent reports are not evidence
A builder or verifier subagent's "done" / "confirmed working" message is a
**claim**, not proof. Always re-run the actual gates yourself before treating a
change as shipped. Select gates by the file group actually changed:
```
# React MFE (pe-dashboard-mfe/)
npm run check:api-contract
npm run lint
npx tsc --noEmit --pretty false
npm test -- --watchAll=false --runInBand

# FastAPI (app/)
py -3.14 _check_pe_config_refs.py
py -3.14 _test_<relevant_area>.py
```
If you didn't personally see these print success for the current state of the
files, it isn't verified — regardless of what an agent said.

## Batch verifiers by file, not by task
When multiple changes land in the same session, don't spin up a separate
verifier per logical task — batch by which files actually changed:
- One verifier pass for all MFE panel/theme/API adapter changes → run the MFE
  contract check, typecheck, focused component tests, then build when relevant.
- One verifier pass for all `app/routers/`/`app/services/` changes → run
  `_check_pe_config_refs.py` once, then the specific `_test_*.py` files touched.
- This avoids burning context/tokens on redundant full-file re-validation between every small edit, while still gating the final state before you call it done.

## Order of operations for a non-trivial change
1. Builder(s) implement, running their own required MFE or backend gate before reporting done.
2. `verifier` agent runs the full gate chain + a mutation-test pass on any guard the change touched + adversarial edge-case hits against the real endpoint.
3. **You** re-run the relevant gates yourself, at least the MFE contract/typecheck or backend config gate.
4. Only then is the change "verified," and only for the scenarios actually exercised — say so explicitly if a manual browser/upload smoke test wasn't done.

## What counts as real verification here (not just green gates)
- Uploading an actual Ctrl-M CSV / SLA XLSX / resource DOCX and reading what the dashboard renders — not just that the endpoint returned 200.
- Checking a specific number against the documented formula (`buffer_pct = (SLA_h − runtime_h) / SLA_h × 100`) with real values, not trusting that "the code looks right."
- For a findings-engine change: confirming the specific rule fires (or doesn't) for a constructed scenario that should trigger it.
