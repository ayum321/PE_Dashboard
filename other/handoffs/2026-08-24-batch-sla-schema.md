## Session 2026-08-24 — strict BatchSLA schema ingestion

### Gates run (exact command → measured result)

- `cd app; py -3.14 ..\tests\test_batch_sla_schema_contract.py` → PASS: verified header variants map identically; missing required fields and duplicate canonical mappings block; rejected upload does not overwrite accepted configuration.
- `cd app; py -3.14 _test_sla_workflow_contract.py` → PASS: SLA workflow response exposes the timing/headroom fields consumed by the MFE.
- `cd app; py -3.14 _check_pe_config_refs.py` → PASS: 118 known names, 60 files scanned.
- `cd pe-dashboard-mfe; npm test -- --watchAll=false --runInBand --runTestsByPath src/components/panels/SlaMatrixPanel.test.tsx src/components/panels/UploadPanel.test.tsx` → PASS: 2 suites, 5 tests. Legacy React/Material UI deprecation warnings remain.
- `cd pe-dashboard-mfe; npx tsc --noEmit --pretty false` → exit 0.
- `cd pe-dashboard-mfe; npm run lint` → exit 0.
- `cd pe-dashboard-mfe; npm run check:api-contract` → PASS.
- `git diff --check` → exit 0; Git printed existing LF-to-CRLF advisory warnings only.
- Mutation check: temporarily removed missing-required validation, then ran `test_batch_sla_schema_contract.py` → failed as expected; validation was restored and the test rerun green.
- `cd pe-dashboard-mfe; npm run build` → build artifact `build/index.html` was written at 2026-08-24 02:55, but the terminal integration returned before reporting the compiler exit/success line. Treat the production build as artifact-present, not fully verified.

### What shipped

- `app/services/sla_merger.py` — strict, code-managed schema v1: only documented aliases with provenance; normalization is case, whitespace, and underscore only; duplicate canonical mappings and missing required fields reject the workbook.
- `app/routers/upload.py` — rejected workbook does not replace the prior accepted BatchSLA configuration; response includes an upload-time mapping report.
- `app/routers/sla_matrix.py` — exposes mapping report and precise Ctrl-M measurement diagnostics without changing SLA, buffer, or status formulas.
- `pe-dashboard-mfe/src/components/panels/SlaMatrixPanel.tsx` — mapping transparency table and distinct source states; ambiguous Ctrl-M match shows no duration; unanchored Ctrl-M fallback is visibly labelled.
- `pe-dashboard-mfe/src/components/panels/UploadPanel.tsx` and `src/api/dashboardApi.ts` — successful mapping summary and named server-side rejection errors reach the intake UI.
- `tests/test_batch_sla_schema_contract.py` — tracked regression contract; `.gitignore` narrowly permits it.

### Proven vs unverified

- Proven: strict accepted/rejected ingestion behavior, stale-data protection, React rendering of absent/empty/ambiguous states, API contract, lint, and typecheck.
- Unverified: signed-in browser upload against an actual customer workbook, live Ctrl-M/Azure integration, production build completion signal, Docker/Stratosphere deployment.
- Alias maintenance: v1 is deliberately code-only and requires a reviewed code change/redeploy for each newly verified customer header. No guessed aliases or admin override were introduced.

### Remaining work

- Add aliases only from real, reviewed customer headers and retain their provenance.
- If a real backend matching path can yield two equal Ctrl-M candidates, emit `AMBIGUOUS_MATCH`; the MFE already renders it with no SLA-measured duration or verdict-driving number.
- Run a full production build in the normal CI agent and a signed-in browser/API upload smoke test before release.

### Durable lesson

- Do not turn a missing source column or an unresolved Ctrl-M match into a blank value. Preserve the cause in the API and render it as a distinct evidence state.
