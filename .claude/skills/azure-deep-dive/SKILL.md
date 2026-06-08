---
name: azure-deep-dive
description: >
  Azure resource monitoring and deep-dive analysis — VM metrics, baseline intelligence,
  timeseries visualization, hot hours, trend detection. Auto-triggers on: Azure monitor,
  deep dive, VM metrics, CPU/memory/disk timeseries, baseline analysis, resource graphs,
  infrastructure health, fleet assessment.
autoActivate: true
---

# Azure Deep-Dive Skill

## When to Activate
- Working on `routers/azure_resource.py`, `services/azure_monitor.py`, `static/deep_dive.js`
- Building or debugging Azure metric collection
- Analyzing VM performance baselines
- Working on deep-dive visualizations

## Architecture
```
azure-identity (az login) → azure-monitor-query → azure_monitor.py
                                                 → azure_resource.py endpoints
                                                 → deep_dive.js Plotly charts
```

### Baseline Intelligence Rules (DD4-DD10)
| Rule | Signal |
|---|---|
| DD4 | Observation window assessment (2/7/15 day confidence tiers) |
| DD5 | Per-VM hot hours — consistent pressure at specific times |
| DD6 | Trend acceleration — metrics getting worse |
| DD7 | Weekday vs weekend divergence — batch vs non-batch load |
| DD8 | Chronic pressure — servers at high utilization many days |
| DD9 | Multi-day recurring spikes at same hour — batch fingerprint |
| DD10 | Fleet-wide trend assessment |

### Evidence Upgrade
When Azure data corroborates existing findings, evidence class upgrades:
`"inferred"` → `"measured"`

### 15-Day Baseline
Recommended for PE judgments — gives hot hours, trends, and pattern confidence.

### Key Field Aliases
- `cpu_pct` = canonical (from `normalize_server()`)
- `cpu_used` = alias for backward compat
- Same for `mem_pct`/`mem_used`, `disk_pct`/`disk_used_max`

### Connection
- Uses `az login` first, SDK fallback for environments where az.exe not on PATH
- Subscription/RG endpoints in `routers/azure_resource.py`
