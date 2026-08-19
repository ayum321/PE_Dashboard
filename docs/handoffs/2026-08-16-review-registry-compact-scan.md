# Review Registry compact scan - 2026-08-16

## Gates run (measured)

- `py -3.14 _test_report_archive.py` -> passed, including the isolated 250-record registry fixture, independent signed/attention filtering, compact-chip behaviour, and exact Fleet-score archive/report comparison.
- `py -3.14 _test_export_report.py` -> passed.
- `py -3.14 _validate_js.py` -> `static/app.js` and `static/deep_dive.js` passed.
- Inline JavaScript extracted from `templates/report_archive.html` -> `node --check` passed.
- `py -3.14 _check_pe_config_refs.py` -> 118 known names resolved across 60 files.
- Local loopback smoke -> temporary `uvicorn` on `127.0.0.1:8768` returned `200` for `/archive` and `/api/report-archive`; the API retained `Cache-Control: no-store`.
- Independent verifier -> passed the focused archive suite, full inline-script parse, a second local-only `/archive` response check, and the collapsed-row/lazy-detail implementation review.
- `git diff --check` -> no whitespace errors; existing LF-to-CRLF warnings remain.
- `npm run check:js` is not defined by this repository's `package.json`; the direct JavaScript validator above was used instead.

## What shipped

- Default registry rows show six compact, severity-coloured snapshot chips: SLA, Fleet, SOW, Benchmark, Issues, and Checklist gaps.
- The existing detailed six-panel snapshot is retained behind an accessible per-customer Show/Hide breakdown control and is constructed only on first expansion.
- The review-completion cell now places the evidence-gap count next to the sign-off status.
- Signed-off and needs-attention remain independent facts. A signed review with evidence gaps is visible in both filters.
- The registry regression fixture creates 250 isolated records without adding test data to the real archive database.
- Fleet-score regression coverage compares the value saved in archive metadata with the Fleet score rendered in that exact exported HTML.

## Remaining live check

- No interactive browser session was available here. Open `/archive` with a dense real or non-production fixture dataset and confirm the compact chips and per-row expansion at the target display width.
