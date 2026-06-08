# PE Audit Dashboard

## Project Overview
Performance Engineering audit dashboard for 250-300 customers. FastAPI backend + vanilla JS frontend. Replaces a legacy Streamlit monolith. Each customer has different DFU/SKU/SLA values — NO hardcoded values anywhere.

## Tech Stack
- **Backend**: FastAPI + Python 3.14, Pydantic v2
- **Frontend**: Vanilla JS (ES2020+), Tailwind v3 CDN, Chart.js + Plotly.js
- **AI**: Google Gemini (genai SDK primary + legacy fallback)
- **Azure**: azure-identity, azure-monitor-query, azure-mgmt-compute/resource/subscription
- **Data**: pandas, numpy, openpyxl, PyMuPDF, python-docx, pypdf

## Commands
```bash
# Development mode (auto-reload — save file + refresh browser, no restart needed)
dev.bat
# or manually:
py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload --reload-dir routers --reload-dir services --reload-dir templates --reload-dir static

# Production start (no reload, full validation)
start.bat

# Validate JS before shipping
py -3.14 _validate_js.py

# Activate venv
.venv\Scripts\Activate.ps1
```

## Architecture

### Single Source of Truth Pipeline
```
Upload → batch_calculator.py → _compute_sla_matrix() → session_cache["resolved_workflow_df"]
                                                      → window.appData.slaMatrix.workflow_summary
```
Every screen reads from `resolved_workflow_df`. No screen recomputes metrics.

### SLA Resolution Order (Tier 1 → 2 → 3)
1. **Tier 1**: `_batch_sla_xlsx` (BatchSLA_info.xlsx) via normalized key `_norm()`
2. **Tier 2**: `_sow_sla_windows` (SOW PDF) batch-type ceilings
3. **Tier 3**: `pe_config` global defaults (DAILY=6h, WEEKLY=8h)

### Data Pillars (uploads)
| Pillar | Route | Session Key |
|---|---|---|
| Batch (Ctrl-M CSV) | `/api/process-batch` | `batch_kpis`, `job_summary`, `regression_df` |
| Resource (DOCX/PDF) | `/api/process-resource` | `resource_summary` |
| SLA Matrix (XLSX) | `/api/process-sla-matrix` | `sla_matrix_kpis`, `workflow_sla_summary` |
| SOW Contract (PDF) | `/api/sow/parse` | `sow_contract`, `volume_vs_sow` |
| Benchmark (XLSX) | `/api/process-benchmark` | `last_benchmark` |
| Issues Register | `/api/upload-issues` | `last_issues` |

## Directory Structure
```
main.py              — FastAPI entrypoint, lifespan wipes stale data on restart
routers/             — API endpoints (batch, resource, sla_matrix, findings, etc.)
services/            — Business logic, parsers, AI engine, session cache
static/app.js        — Main frontend logic (SLA panels, charts, uploads)
static/deep_dive.js  — Azure deep-dive visualization
templates/           — Jinja2 HTML shells
.pe_config.json      — Persisted config (config_store.py)
.pe_cache.json       — Persisted session data (session_cache.py)
```

## Key Files

### Threshold System
**All thresholds live in `services/pe_config.py`** — never hardcode in routers or JS.
- `SLA_ATRISK_PCT` = 15.0 → buffer ≤ this = AT_RISK
- `SLA_LONGJOB_PCT` = 40.0 → buffer ≤ this = LONG_JOB
- `SLA_DAILY_HRS` = 6.0, `SLA_WEEKLY_HRS` = 8.0
- Buffer formula everywhere: `buffer_pct = (SLA_h − runtime_h) / SLA_h × 100`

### Session & Config
- `services/session_cache.py` — in-memory audit context (`ac_set`/`ac_get`/`ac_snapshot`)
- `services/config_store.py` — persisted JSON (`.pe_config.json`)
- `services/pe_config.py` — canonical defaults, `reload()` re-reads from config_store

### Findings Engine (routers/findings.py)
14 rule sections: batch (R0-R8), resource, cross-source, SLA matrix, benchmark, SOW, regression, adaptive SLA, issues, intelligence (A1-A10), narrative, audit gaps.

### Normalization
`_norm(s)` = strip env prefix (PROD_/TEST_/UAT_/DEV_/STG_) → UPPERCASE
Frontend mirror: `_normWf(n)` in `static/app.js`

## Coding Conventions
- No hardcoded/mock values — extract real data from uploads
- Code should be smart, connected across all dashboard panels
- Prefer streamlined, accurate, informative implementations
- Don't add unnecessary abstractions or over-engineer
- Provenance columns always present: `sla_source`, `reason_code`, `debug_*`
- NaN guard: use `math.isnan()` not `float(NaN) or 0`
- Division-by-zero: use `np.nan` guard then `fillna(-100)`

## PE Review Writing Style
- Direct, factual — no hedging or AI fluff
- Lead with numbers: "59,316 SKU" not "the SKU volume is approximately..."
- Use parenthetical specifics: "(19/19)", "(~4.03 hours)", "(33% buffer)"
- Status markers: "✓ COMPLIANT", "APPROVED"
- 4 sections: Data Volume, Batch SLA, Infrastructure, UAT

## Known Patterns & Fixes
- **Dev mode**: Use `dev.bat` or `--reload` — auto-restarts on file changes, no manual stop/start
- **Production**: Use `start.bat` — validates JS syntax before launching
- **JS validation**: `py _validate_js.py` — run before shipping zip to customers
- `annual_fee` safe format: `f"{float(_fee_raw):,.0f}"` (catches None)
- Mixed date formats: `_parse_dt()` multi-pass with dayfirst=True fallback
- Run_Sec inferred from End-Start capped at 168h (1 week)
- SOW engagement keys wiped on server restart (lifespan handler)
- Session boundary: sessionStorage-based tracking (new tab = fresh, reload = restore)
