---
description: "Use when building dashboard UI, frontend panels, Chart.js charts, Plotly visualizations, upload handlers, KPI gauges, Tailwind styling, session boundary, executive dashboard, SLA matrix rendering."
applyTo: "static/app.js, static/deep_dive.js, templates/index.html, templates/report_export.html"
---

# Dashboard Builder Rules

## Global State
```javascript
window.appData = {
  slaMatrix: { workflow_summary, job_summary, kpis },
  config: { sla_atrisk_pct, sla_longjob_pct, ... },
  batchSlaInfo: { workflows },
  sowCompare, resourceSummary, lastFindings, lastRedFlags
}
```

## Critical Rules
- Thresholds from `window.appData.config` — NEVER hardcode values
- Every upload handler must call `_markSessionActive()`
- `clearSessionData()` must call `_clearSessionMarker()`
- `_execCache = null` after SLA upload so gauges refresh
- Legend colors match pe_config: OK=green, LONG_JOB=amber, AT_RISK=orange, BREACH=red

## Session Boundary
- `sessionStorage`-based: `_markSessionActive()` / `_isSessionActive()`
- New tab = fresh (fires `/api/clear-session`)
- Reload = restores from server cache

## Error Pattern
```javascript
try {
  const resp = await fetch(url, opts);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  const data = await resp.json();
} catch (err) { _handleFetchError(err, 'Panel Name'); }
```

## Normalization
`_normWf(n)` mirrors Python `_norm()` — strip PROD_/TEST_/UAT_/DEV_/STG_ prefix, uppercase
