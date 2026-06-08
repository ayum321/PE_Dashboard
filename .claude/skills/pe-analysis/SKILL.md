---
name: pe-analysis
description: >
  Performance Engineering audit analysis — batch SLA compliance, resource utilization,
  workflow regression, findings generation, and PE narrative writing. Auto-triggers on:
  analyzing batch data, SLA calculations, resource health, findings rules, PE reviews,
  audit quality, compliance checks, buffer calculations, breach detection.
autoActivate: true
---

# PE Analysis Skill

## When to Activate
- User asks about batch SLA, compliance, buffer calculations
- Working on `routers/findings.py`, `services/batch_calculator.py`, `services/sla_engine.py`
- Analyzing Ctrl-M CSV data, resource DOCX/PDF, SLA XLSX
- Writing or debugging PE narrative sections
- Reviewing findings quality or accuracy

## Architecture Rules

### Single Source of Truth
All metrics flow through one pipeline:
```
Upload → batch_calculator.py → _compute_sla_matrix() → session_cache["resolved_workflow_df"]
```
Every screen reads from `resolved_workflow_df`. **No screen recomputes metrics.**

### SLA Resolution (Tier 1 → 2 → 3)
1. Tier 1: `_batch_sla_xlsx` (BatchSLA_info.xlsx) via `_norm()` key
2. Tier 2: `_sow_sla_windows` (SOW PDF) batch-type ceilings  
3. Tier 3: `pe_config` global defaults (DAILY=6h, WEEKLY=8h)

### Buffer Formula (identical everywhere)
```python
buffer_pct = (SLA_h - runtime_h) / SLA_h * 100
```
- `> LONGJOB_PCT (40)` → OK
- `<= LONGJOB_PCT` → LONG_JOB
- `<= ATRISK_PCT (15)` → AT_RISK
- `<= 0` → BREACH

### Thresholds
ALL thresholds live in `services/pe_config.py`. Never hardcode in routers or JS.

### Provenance
Every metric row must include: `sla_source`, `reason_code`, `debug_*` columns.

### Data Guards
- NaN: use `math.isnan()` not `float(NaN) or 0`
- Division-by-zero: use `np.nan` guard then `fillna(-100)`
- Timedelta cap: Run_Sec from End-Start capped at 168h (1 week)

## Findings Engine (routers/findings.py)
14 rule sections in order:
1. No-data guard
2. PE Audit Coverage Strip
3. BATCH (R0-R8): no runs, compliance, window, per-job ceiling, worst breach, buffer, anomalies, zero-dur, failure rate, sub-app hotspots
4. RESOURCE: fleet grade, critical/warn/ok, role-specific CPU, dual pressure, saturation
5. CROSS-SOURCE: batch breach + CPU pressure correlation
6. SLA MATRIX: window compliance, per-run, tightest buffer, triage, repeat offenders
7. BENCHMARK: threshold, worst tx, categories
8. SOW COMPARE: exceeded/under/optimal
9. REGRESSION: z-score > 2σ critical, 1.5-2σ info
10. ADAPTIVE SLA: p95 within 10% of SLA ceiling
11. ISSUES REGISTER: open critical/all
12. INTELLIGENCE (A1-A10): misleading green, idle-time, waiver, contradictions
13. NARRATIVE: scope/compliance/rca/impact/evidence
14. AUDIT GAPS: missing files with impact

## PE Review Writing Style
- Direct, factual — no hedging or AI fluff
- Lead with numbers: "59,316 SKU" not "the SKU volume is approximately..."
- Parenthetical specifics: "(19/19)", "(~4.03 hours)", "(33% buffer)"
- Status markers: "✓ COMPLIANT", "APPROVED"
- 4 sections: Data Volume, Batch SLA, Infrastructure, UAT
