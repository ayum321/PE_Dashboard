---
description: "Use when working on PE batch analysis, SLA compliance, buffer calculations, findings rules, regression detection, breach analysis, workflow normalization, audit quality, pe_config thresholds."
applyTo: "services/batch_calculator.py, services/sla_engine.py, services/sla_merger.py, services/sla_parser.py, services/sla_intelligence.py, services/pe_config.py, services/smart_findings.py, routers/findings.py, routers/sla_matrix.py, routers/sla_intelligence.py, routers/batch.py"
---

# PE Analysis Rules

## Single Source of Truth
```
Upload → batch_calculator.py → _compute_sla_matrix() → session_cache["resolved_workflow_df"]
```
Every screen reads from `resolved_workflow_df`. No screen recomputes metrics.

## SLA Resolution (Tier 1 → 2 → 3)
1. Tier 1: `_batch_sla_xlsx` (BatchSLA_info.xlsx) via `_norm()` key
2. Tier 2: `_sow_sla_windows` (SOW PDF) batch-type ceilings
3. Tier 3: `pe_config` global defaults (DAILY=6h, WEEKLY=8h)

## Buffer Formula (must be identical everywhere)
```python
buffer_pct = (SLA_h - runtime_h) / SLA_h * 100
```
- `> 40` → OK | `15-40` → LONG_JOB | `0-15` → AT_RISK | `≤ 0` → BREACH

## Mandatory Guards
- NaN: `math.isnan(val)` — NEVER `float(NaN) or 0`
- Division-by-zero: `np.nan` guard then `fillna(-100)`
- Timedelta cap: Run_Sec from End-Start capped at 168h
- ALL thresholds from `services/pe_config.py` — never hardcode

## Provenance (every metric row)
`sla_source`, `reason_code`, `debug_join_hit`, `debug_buffer_reason`, `debug_runtime_source`

## Normalization
`_norm(s)` = strip env prefix (PROD_/TEST_/UAT_/DEV_/STG_) → UPPERCASE
