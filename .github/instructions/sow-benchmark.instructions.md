---
description: "Use when working on SOW contract parsing, volume comparison, DFU/SKU analysis, benchmark processing, issues register, correlation engine, export, red flags."
applyTo: "routers/sow.py, routers/benchmark.py, routers/correlation.py, routers/export.py, routers/redflags.py, routers/upload.py, routers/final_judgment.py, services/sow_parser.py, services/correlation_engine.py, services/data_reviewer.py, services/verdict_reconciler.py"
---

# SOW / Benchmark / Support Rules

## SOW Flow
1. Upload PDF → `/api/sow/parse` → fills `sow_baseline` + `ac_set("sow_contract")`
2. Manual DFU/SKU input → `triggerPeNarrative()` live
3. Save & Compare → `/api/sow/baseline` + `/api/sow/compare` → findings + narrative
4. Clear Session → DELETE `/api/sow/baseline` + wipe

## SOW Baseline (config_store)
- `sow_baseline` = `{daily_dfu, daily_sku}`
- Cleared by DELETE `/api/sow/baseline` and POST `/api/clear-session`

## Benchmark
- Upload XLSX → `/api/process-benchmark`
- Findings: threshold checks, worst tx, categories, fill rate

## Coding Rules
- 250-300 customers with different values — NO hardcoded data
- All upload routes validate file type
- Division-by-zero guards on all ratio calculations
