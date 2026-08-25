# React MFE ↔ FastAPI Parity Workflow

## When to use

Use for a change that crosses the React panel/API adapter and FastAPI calculation
or response contract.

## Rules

1. Freeze the shared layer before fanning out. Investigators are read-only;
   builders own either MFE or backend files, never the same contract file.
2. Write the payload contract first: field name, unit, null semantics,
   calculation basis, source/provenance, and consumer panel.
3. Backend publishes calculated values; React renders and explains them. No
   duplicated formula, threshold, spike, or findings logic.
4. Verify by file group: backend direct tests/config gate, then MFE contract
   check/typecheck/component tests/build. A verifier then probes representative
   data; the lead reruns gates personally.
5. Treat agent reports as leads, not evidence. Record exact output and skipped
   checks in the handoff.

## Manual parity check

- Upload Ctrl-M + SLA Matrix; compare Matrix, Batch Window, and Findings for
  the same workflow/status/headroom.
- Verify a missing source produces an explicit missing state, not a green claim.
- Export once and confirm the frozen Registry row matches the exported evidence.
