---
name: pe-analysis
description: Analyze or change PE dashboard batch SLA, compliance buffers, resource health, findings rules, SOW/benchmark data, provenance, or customer-facing PE narrative. Use for Ctrl-M CSV, SLA XLSX, resource DOCX/PDF, metrics that look wrong, and findings accuracy.
---

# PE analysis

1. Read the relevant architecture and hard-won-gotcha sections of `CLAUDE.md`; use `pe-analyst` for a read-only trace when the request asks where a value came from or why it is wrong.
2. Preserve the single source of truth: upload → parser/calculator → `session_cache["resolved_workflow_df"]` → `window.appData.slaMatrix.workflow_summary`. Do not recompute the same metric in another panel.
3. Resolve SLA values in order: BatchSLA XLSX by normalized key, SOW batch-type ceiling, then `pe_config` fallback. Record provenance with `sla_source`, `reason_code`, and `debug_*` fields.
4. Keep the canonical buffer calculation: `(SLA_h - runtime_h) / SLA_h * 100`. Read thresholds from `services/pe_config.py`; never hardcode them elsewhere.
5. Use `math.isnan()` for NaN and an explicit `np.nan`/`fillna(-100)` guard for zero denominators. Cap inferred End−Start runtime at 168 hours.
6. When a result changes, trace it through the affected route, service, session key, finding, and UI. Run `npm run check:pe-config`, the relevant direct `_test_*.py`, and a representative real-data or TestClient scenario.

Write PE conclusions with numbers first, concrete evidence, and no invented customer values.
