---
name: dashboard-builder
description: >
  Dashboard UI building — frontend panels, charts, Tailwind styling, Chart.js/Plotly.js
  visualizations, upload handlers, and executive KPI gauges. Auto-triggers on:
  building UI panels, adding charts, modifying app.js or deep_dive.js, creating new
  dashboard sections, fixing frontend rendering, styling with Tailwind.
autoActivate: true
---

# Dashboard Builder Skill

## When to Activate
- Working on `static/app.js`, `static/deep_dive.js`, `templates/index.html`
- Building new dashboard panels, charts, or KPI cards
- Fixing frontend rendering, upload flows, or data display
- Adding Chart.js or Plotly.js visualizations

## Frontend Architecture

### Tech
- Vanilla JS (ES2020+), NO framework
- Tailwind v3 via CDN
- Chart.js for bar/line/doughnut charts
- Plotly.js for heatmaps and deep-dive timeseries

### Global State
```javascript
window.appData = {
  slaMatrix: { workflow_summary: [...], job_summary: [...], kpis: {...} },
  config: { sla_atrisk_pct: 15, sla_longjob_pct: 40, ... },
  batchSlaInfo: { workflows: [...] },
  sowCompare: { ... },
  resourceSummary: [...],
  lastFindings: [...],
  lastRedFlags: [...]
}
```

### Key Functions
| Function | Purpose |
|---|---|
| `_renderSlaCommitmentsPanel()` | Tier 1 SLA panel with buffer bars |
| `_renderSlaMatrix(data)` | Job-level SLA compliance table |
| `_handleFetchError(err, label)` | All network errors route here |
| `_showServerDownBanner()` | Red banner + 5s auto-retry |
| `_normWf(n)` | Frontend mirror of Python `_norm()` |
| `triggerPeNarrative()` | Fires PE narrative generation |
| `triggerGenerateFindings()` | Fires findings engine |

### Session Boundary
- `sessionStorage`-based tracking via `_markSessionActive()` / `_isSessionActive()`
- New tab = fresh start (fires `/api/clear-session`)
- Same-tab reload = restores from server cache

### Rules
- Thresholds from `window.appData.config` — NEVER hardcode values
- Legend colors/labels must match `pe_config` thresholds
- Every upload handler must call `_markSessionActive()`
- `clearSessionData()` must call `_clearSessionMarker()`
- Exec cache (`_execCache`) must be nulled after SLA upload

### Color Coding (from pe_config thresholds)
```
OK:        green  (buffer > 40%)
LONG_JOB:  amber  (buffer 15-40%)
AT_RISK:   orange (buffer 0-15%)
BREACH:    red    (buffer ≤ 0%)
```

### Error Handling Pattern
```javascript
try {
  const resp = await fetch('/api/endpoint', { method: 'POST', body: formData });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  const data = await resp.json();
  // render...
} catch (err) {
  _handleFetchError(err, 'Panel Name');
}
```
