---
name: dashboard-builder
description: >
  Deployed React/Luminate dashboard UI — MUI panels, Highcharts visualisations,
  upload feedback, evidence tables, and executive KPI presentation. Auto-triggers
  on React MFE panels, charts, UI rendering, styling, and operational dashboard UX.
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
- React 18 + TypeScript in `pe-dashboard-mfe/src/`
- MUI for components and `src/theme/dashboard.css` for shared evidence states
- Highcharts for operational bar/line/gauge charts
- `src/api/dashboardApi.ts` is the MFE API/session boundary

### State and contracts
- `AppDataContext` holds current session evidence for panels.
- `dashboardApi.ts` returns FastAPI-calculated payloads and preserves `pe_sid`.
- `SlaMatrixPanel`, `BatchPanel`, `ResourcePanel`, and `PeReviewSummary` display
  shared backend evidence; no panel reimplements status/buffer/spike logic.

### Key Functions
| Function | Purpose |
|---|---|
| `SlaMatrixPanel` | SLA evidence, start/end, duration basis, and headroom |
| `BatchPanel` | Daily windows, spike evidence, job-level risk |
| `PeReviewSummary` | Evidence-gated narrative and ranked questions |
| `FindingsPanel` | Decision-first PE Findings composition |
| `dashboardApi.ts` | All FastAPI requests, runtime endpoint, credentials |

### Session Boundary
- Server session state is tied to the `pe_sid` cookie; React context renders the
  returned evidence. Do not split local traffic between `localhost` and `127.0.0.1`.

### Rules
- Thresholds come from backend configuration — never hardcode values in a panel.
- Legends, gauges, colour and text labels must match the backend status.
- State absence must render as explicit missing evidence, never blank/healthy.
- Use shared hover/focus evidence borders and semantic severity tokens, not local
  decorative gradients or unreadable metadata.

### Color Coding (from pe_config thresholds)
```
OK:        green  (buffer > 40%)
LONG_JOB:  amber  (buffer 15-40%)
AT_RISK:   orange (buffer 0-15%)
BREACH:    red    (buffer ≤ 0%)
```

### Error Handling Pattern
Use the typed `dashboardApi.ts` request/upload helpers. They centralize response
errors, runtime base URL, upload progress, and `credentials: 'include'`.
