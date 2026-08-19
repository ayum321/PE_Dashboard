# UAT evidence-aware PE Findings - 2026-08-03

## Gates run (measured)

- `npm run check:js` -> `static/app.js` (23,354 lines) and `static/deep_dive.js` (265 lines) passed.
- `npm run check:pe-config` -> 98 known configuration names resolved across 57 files.
- `.\\.venv\\Scripts\\python.exe _test_uat_evidence.py` -> `UAT evidence checks passed`.
- `.\\.venv\\Scripts\\python.exe -m py_compile main.py routers\\benchmark.py routers\\redflags.py routers\\pe_narrative.py services\\session_cache.py` -> passed.
- Mutation check: disabled the UI-evidence guard temporarily; the direct UAT test failed as expected, then the mutation was reverted.
- `git diff --check` -> no diff whitespace errors. Existing CRLF warnings remain in unrelated changed files.

## What shipped

- `routers/benchmark.py` and `services/session_cache.py` retain UI benchmark and batch benchmark uploads independently, including across the dashboard refresh context.
- `main.py` exposes the two source-specific benchmark payloads and treats UAT evidence as loaded when either source is present.
- `routers/pe_narrative.py` presents separate UI performance benchmark and batch performance data rows; an empty audit has only upload guidance, not a fabricated result. Each row retains its own uploaded filename.
- `routers/redflags.py` creates UAT customer questions only from submitted UI or batch evidence, with source values in the evidence text. Generic testing, DR, and monitoring questions are not created without such evidence.
- `static/app.js` restores both sources independently and routes only evidence-derived UAT questions to the UAT narrative section.
- `_test_uat_evidence.py` covers no-evidence, UI breach, batch regression, mixed narrative, and source-specific filenames.

## Proven vs. assumed

- Proven: direct FastAPI endpoint tests validate the no-evidence, UI-only, batch-only, and combined evidence paths.
- Proven: the cache/context contract preserves the UI and batch payloads under separate keys; the combined narrative retains the UI and batch filenames separately.
- Not completed: visible in-app browser smoke test. The configured browser reported `No browser is available` in this session.
- Assumed: the existing upload controls continue to provide valid UI and batch benchmark payloads; their client-side merge path is exercised by the existing dashboard wiring rather than a browser session.

## Remaining work

- In a browser-enabled session, upload one UI performance benchmark and one batch benchmark, open PE Findings, and confirm the two source rows and their evidence-specific questions render as expected after a page refresh.

## Lessons

- UAT questions must be evidence-derived: missing artifacts should request an upload, not create unsupported operational findings.
- Keep UI and batch benchmark evidence in distinct cache slots so a later upload cannot silently overwrite the other source.
