# PE Audit Dashboard

> **Performance Engineering batch SLA compliance tool for 250–300 enterprise customers.**
> Replaces a legacy Streamlit monolith. Each customer has different DFU/SKU/SLA values — no hardcoded values anywhere.

---

## Quick Start

```
1. Double-click  start.bat
2. Browser opens at  http://127.0.0.1:<port>/
3. Upload your files and run the audit
```

`start.bat` auto-detects Python 3.11+, installs all packages on first run (cached 7 days), finds a free port, and opens the browser.  
`dev.bat` — development mode with auto-reload on file save.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Uvicorn + Pydantic v2 (Python 3.11+) |
| Frontend | Vanilla JS (ES2020+), Tailwind v3 CDN, Chart.js, Plotly.js |
| AI | Google Gemini (`google-genai` SDK) |
| Azure | `azure-identity`, `azure-monitor-query`, `azure-mgmt-*` |
| Data | pandas, numpy, openpyxl, PyMuPDF, python-docx |

---

## Project Structure

```
PE_Dashboard/
├── main.py                   FastAPI entrypoint + router registration
├── start.bat                 Smart launcher (Python auto-detect, pip install, port find)
├── dev.bat                   Dev mode (--reload, watches routers/ services/ static/ templates/)
├── requirements.txt          All Python dependencies
│
├── routers/                  ── API Endpoints (one file per feature) ──
│   ├── batch.py              Ctrl-M CSV upload + batch KPI computation
│   ├── sla_matrix.py         BatchSLA_info.xlsx upload + per-job SLA resolution
│   ├── resource.py           Server resource DOCX/PDF parsing + fleet grading
│   ├── benchmark.py          UI benchmark XLSX comparison (PROD vs UAT/TEST)
│   ├── findings.py           14-rule PE findings engine (R0–R8, intelligence, narrative)
│   ├── sow.py                SOW PDF parsing + volume vs contract comparison
│   ├── pe_narrative.py       AI narrative generation (Gemini)
│   ├── executive.py          Executive summary generation
│   ├── export.py             PDF/HTML report export
│   ├── config.py             Settings read/write API
│   ├── upload.py             Multi-file upload handler + SLA ceilings endpoint
│   ├── azure_resource.py     Azure VM metrics deep-dive
│   ├── correlation.py        Cross-pillar correlation engine
│   ├── redflags.py           Red flag detection
│   ├── final_judgment.py     Overall audit verdict
│   ├── sla_intelligence.py   Adaptive SLA intelligence
│   ├── pe_consultant.py      AI consultant Q&A
│   ├── agent.py              Agent tools router
│   └── ai.py                 AI model management
│
├── services/                 ── Business Logic (no HTTP, pure Python) ──
│   ├── batch_calculator.py   Core SLA + window compliance engine (single source of truth)
│   ├── sla_engine.py         SLA ingestion, schedule classification, ceiling builder
│   ├── sla_merger.py         BatchSLA XLSX fuzzy matching + year-token stripping
│   ├── compliance_engine.py  Shared window compliance (Batch Review + SLA Matrix)
│   ├── sla_parser.py         SLA document parser
│   ├── sla_intelligence.py   Adaptive SLA rule engine
│   ├── pe_config.py          ALL thresholds and defaults (never hardcode elsewhere)
│   ├── session_cache.py      In-memory audit context (ac_set / ac_get / ac_snapshot)
│   ├── config_store.py       Persisted config (.pe_config.json)
│   ├── resource_parser.py    Structured DOCX/PDF resource doc parser
│   ├── resource_parser_generic.py  Generic fallback resource parser
│   ├── resource_calculator.py      Fleet health grading
│   ├── sow_parser.py         SOW PDF parser (DFU/SKU/volume/SLA extraction)
│   ├── ai_engine.py          Gemini wrapper (new SDK + legacy fallback)
│   ├── ai_narrator.py        PE report narrative writer
│   ├── ai_agent.py           AI agent orchestration
│   ├── correlation_engine.py Cross-pillar data correlation
│   ├── data_reviewer.py      Data quality reviewer
│   ├── smart_findings.py     Smart findings prioritisation
│   ├── verdict_reconciler.py Audit verdict reconciliation
│   ├── azure_monitor.py      Azure Monitor query client
│   └── ...
│
├── static/
│   ├── app.js                Main frontend (~17k lines): uploads, panels, charts, findings
│   ├── deep_dive.js          Azure deep-dive timeseries charts
│   └── favicon.svg
│
└── templates/
    ├── index.html            Main dashboard shell (Jinja2)
    └── report_export.html    Print/export report template
```

---

## SLA Resolution Pipeline

```
Upload → sla_merger.py → sla_engine.py → batch_calculator.py
                                        ↓
                         session_cache["resolved_workflow_df"]
                                        ↓
                     window.appData.slaMatrix.workflow_summary
```

Every screen reads from `resolved_workflow_df`. No screen recomputes metrics independently.

### Resolution Tiers (highest priority first)

| Tier | Source | How |
|---|---|---|
| 1 | `BatchSLA_info.xlsx` | Per-job contract targets via fuzzy name matching + `_norm()` |
| 2 | SOW PDF | Batch-type ceilings extracted from contract document |
| 3 | `pe_config` defaults | `DAILY=6h`, `WEEKLY=8h` (configurable in Settings) |

---

## Window Compliance Logic

```
Group by (Sub_Application, run_date)
  elapsed_hrs = max(End_Time) − min(Start_Time)
  breach      = elapsed_hrs > per-sub-app SLA ceiling

Headline compliance % uses DAILY/UNKNOWN sub-apps only.
WEEKLY sub-apps are tracked separately (different cadence).
```

---

## Uploading Data

Order of upload does not matter. Each pillar is independent.

| File | Tab | Session Key |
|---|---|---|
| `BatchSLA_info.xlsx` | SLA Matrix | `sla_matrix_kpis`, `workflow_sla_summary` |
| Ctrl-M CSV | Batch Review | `batch_kpis`, `job_summary`, `regression_df` |
| Resource DOCX/PDF | Resource Review | `resource_summary` |
| Benchmark XLSX | Benchmark | `last_benchmark` |
| SOW PDF | SOW / Volume | `sow_contract`, `volume_vs_sow` |
| Issues Register | Issues | `last_issues` |

---

## Findings Engine

14 rule sections in `routers/findings.py`:

- **R0–R8** — Batch SLA rules (window compliance, job breach, failures, regression)
- **Resource** — Fleet CPU/memory/disk health
- **Cross-source** — Batch × Resource correlation
- **SLA Matrix** — Per-workflow contract coverage
- **Benchmark** — UI performance breach detection
- **SOW** — Volume vs contract delta
- **Regression** — Week-over-week runtime drift
- **Adaptive SLA** — AI-assisted SLA recommendation
- **Issues** — Issues register analysis
- **Intelligence (A1–A10)** — Gemini AI observations
- **Narrative** — Auto-generated PE report text
- **Audit Gaps** — Missing data / low coverage warnings

---

## Key Config (services/pe_config.py)

| Setting | Default | Meaning |
|---|---|---|
| `SLA_ATRISK_PCT` | 15.0 | Buffer ≤ 15% = AT_RISK |
| `SLA_LONGJOB_PCT` | 40.0 | Buffer ≤ 40% = LONG_JOB |
| `SLA_DAILY_HRS` | 6.0 | Fallback daily window SLA |
| `SLA_WEEKLY_HRS` | 8.0 | Fallback weekly window SLA |
| `BENCHMARK_ACTION_SLA` | Load=3s, Export=10s, Save=5s… | Per-action UI SLA thresholds |

Buffer formula: `buffer_pct = (SLA_h − runtime_h) / SLA_h × 100`

---

## Development

```bash
# Auto-reload dev mode
dev.bat

# Validate JS before shipping
py -3.14 _validate_js.py

# Activate venv (if using local venv)
.venv\Scripts\Activate.ps1

# Run server manually
py -3.14 -m uvicorn main:app --host 127.0.0.1 --port 8765
```

---

## Requirements

- Python 3.11+ (3.12 or 3.13 recommended)
- Windows 10/11
- Internet access for first-time package install (~2 min)
- Gemini API key (optional — for AI narrative; set in Settings tab)
