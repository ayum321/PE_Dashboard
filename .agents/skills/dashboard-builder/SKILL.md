---
name: dashboard-builder
description: "Build or change this dashboard’s vanilla-JS UI: static/app.js, static/deep_dive.js, Jinja templates, Tailwind styling, Chart.js or Plotly charts, upload handlers, KPI panels, and frontend rendering bugs."
---

# Dashboard builder

1. Read the frontend conventions and matching gotcha in `CLAUDE.md`. For Azure charts also load `azure-deep-dive`.
2. Preserve `"use strict"` in `static/app.js`. Before introducing `const` or `let`, search the surrounding function scope; a duplicate declaration can prevent the entire dashboard from loading.
3. Render already-calculated server/session data. Do not recompute SLA or compliance math in the frontend. Read configurable thresholds from `window.appData.config`.
4. Keep `_normWf()` aligned with Python `_norm()`. Upload handlers mark the session active; clearing data clears the session marker; route failures through `_handleFetchError()`.
5. Keep chart labels, colors, and legends consistent with `pe_config` and the actual data source. Do not add mock customer metrics.
6. Run `npm run check:js`. For behavior changes, open the changed screen with representative data and state explicitly if a browser smoke test was not performed.
