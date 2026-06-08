---
description: "Use when working on Azure resource monitoring, deep-dive analysis, VM metrics collection, baseline intelligence, hot hours, trend detection, CPU/memory/disk timeseries, fleet assessment."
applyTo: "routers/azure_resource.py, services/azure_monitor.py, static/deep_dive.js"
---

# Azure Deep-Dive Rules

## Architecture
```
azure-identity (az login) → azure-monitor-query → azure_monitor.py → azure_resource.py → deep_dive.js
```

## Baseline Intelligence (DD4-DD10)
| Rule | Signal |
|---|---|
| DD4 | Observation window assessment (2/7/15 day confidence tiers) |
| DD5 | Per-VM hot hours — consistent pressure at specific times |
| DD6 | Trend acceleration — metrics getting worse |
| DD7 | Weekday vs weekend divergence — batch vs non-batch load |
| DD8 | Chronic pressure — servers at high utilization many days |
| DD9 | Multi-day recurring spikes at same hour — batch fingerprint |
| DD10 | Fleet-wide trend assessment |

## Evidence Upgrade
Azure data corroboration: `"inferred"` → `"measured"`

## Field Aliases
- `cpu_pct` = canonical (from `normalize_server()`)
- `cpu_used` = backward compat alias (executive.py, correlation_engine.py)
- Same for `mem_pct`/`mem_used`, `disk_pct`/`disk_used_max`

## Connection
Uses `az login` first, SDK fallback if az.exe not on PATH.
