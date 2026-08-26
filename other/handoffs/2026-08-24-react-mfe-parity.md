# React MFE parity handoff — 2026-08-24

## Shipped

- SLA Matrix now presents the backend's observed start/end, elapsed duration,
  canonical SLA-measured duration, and duration headroom/overrun.  It does not
  recalculate a verdict in the browser.
- Daily Batch Window displays persistent breach/spike markers from the server's
  `window[].spike` evidence.  Client z-score calculation remains only as a
  labelled fallback for older payloads that do not have this field.
- Resource Review's Fleet Heatmap now renders every server/time bucket. Missing
  telemetry is a visible hatched, bordered `not emitted` state rather than a
  blank background; healthy periods remain visible as healthy cells.
- PE Findings presents the backend-ranked `top_action`, keeps finding
  provenance collapsed until requested, and renders UAT only when explicit UAT,
  UI benchmark, or comparable batch-performance evidence exists. Critical and
  warning findings are now the readable, expanded action list; informational
  and healthy rows are collapsed as supporting evidence by default.
- Review Registry archive coverage remains in the React suite and the archive
  backend regression confirms export snapshots are retained safely.
- The React visual system adds stronger semantic hierarchy, table borders,
  focus-visible outlines, and panel hover/glow surfaces without manufacturing
  metrics.

## Measured gates

Run from `pe-dashboard-mfe`:

```text
npm run check:api-contract                                  PASS
npm run lint                                                PASS
npx tsc --noEmit --pretty false                             PASS
npm run test-no-coverage -- --watchAll=false --runInBand    PASS: 18 suites, 36 tests
npm run build                                               PASS
```

Run from `app`:

```text
py -3.14 _test_sla_workflow_contract.py                     PASS
py -3.14 _test_batch_audit_contract.py                      PASS
py -3.14 _test_window_compliance_regression.py              PASS
py -3.14 _test_pattern_detection.py                         PASS
py -3.14 _test_report_archive.py                            PASS
py -3.14 _check_pe_config_refs.py                           PASS
py -3.14 _test_config_deployment_safety.py                  PASS
py -3.14 _test_mfe_spa_fallback.py                          PASS
git diff --check                                            PASS (CRLF warnings only)
```

The final MFE production build completed successfully. Its generated initial
gzip sizes were 397.70 kB JavaScript and 4.28 kB CSS.

## Required deployment follow-through

- Local Docker image verification was attempted but could not run because the
  Docker CLI is not installed on this workstation.  DevOps must run
  `docker build -t pe-dashboard:VERSION .` in CI, then health-check
  `GET /api/health` before promotion.
- Supply API URLs, AI/Azure credentials, and allowed origins through the
  deployment secret manager/runtime environment only.  Do not put secrets in
  `env.js`; it is public browser code.
- Keep portal and API same-site until the session/cookie design is explicitly
  changed.  Hosted Azure access needs approved workload identity/OAuth rather
  than the local interactive browser-login path.

## Still needs a human screen check

No live signed-in browser/API session was available in this verification pass.
Before release, verify one representative Ctrl-M upload plus SLA Matrix upload
in the deployed ingress: confirm the upload progress/confirmation, hover/focus
states, SLA timing fields, server spike marker, UAT suppression without a
relevant document, and Review Registry entry after export.
