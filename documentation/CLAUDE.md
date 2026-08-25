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

## The Gate
There is no pytest/mypy/ruff/eslint here — this repo's real build→verify loop is:

1. `py -3.14 _validate_js.py` — full Node `--check` parse + brace/bracket balance on `static/app.js` / `deep_dive.js`. **THE hard gate.** A broken `app.js` doesn't error in the browser — it silently kills every JS function on the page (`window.appData` becomes `undefined`, buttons do nothing). This must be clean before any commit that touches `static/`.
2. `py -3.14 _check_pe_config_refs.py` — every `_pc.NAME` / `pe_config.NAME` reference in `routers/` and `services/` must resolve to a real module-level name in `services/pe_config.py`. Catches the `AttributeError: module has no attribute X` class of bug before it reaches a customer machine.
3. The relevant `_test_*.py` script(s) for the area you touched (e.g. `_test_findings_quality.py` for `routers/findings.py` changes) — run directly with `py -3.14 _test_X.py`, not pytest-discovered. Uses `fastapi.testclient.TestClient` against `main:app` with plain asserts.
4. Manual smoke test — start the server (`dev.bat`) and actually exercise the endpoint/screen you changed. Green gates above prove syntax and known-scenario correctness; they do NOT prove the feature works for a real upload.

Both 1 and 2 are wired into `dev.bat` and `start.bat` — they block server startup on failure. Run them yourself before saying something is done; don't rely on someone else running `start.bat` to find out.

## Portable Principles
1. **Green gates are not verification.** `_validate_js.py` passing proves the file parses. `_check_pe_config_refs.py` passing proves no attribute typos. Neither proves a Batch/SLA/Resource screen renders the right number for a real upload. Real verification = upload an actual file, open the actual screen, read what the dashboard actually shows.
2. **Mutation-test every correctness guard.** If you write or touch a rule in `routers/findings.py`, a compliance/buffer calc, or a NaN/div-by-zero guard: break it on purpose, confirm the relevant `_test_*.py` goes RED, then revert. A guard whose test never fails when broken is decoration, not protection. Confirm your mutation actually ran (print/assert the mutated path was hit) — a mutant that never executed is not a survivor.
3. **Deterministic checks beat vigilance.** `_check_pe_config_refs.py` exists because "remember to grep for `_pc.` before adding a constant" failed twice in this repo's history (`DB_MEM_BAND_LOW`, `RESOURCE_CAPTURE_DAYS` — both real `AttributeError` incidents). If a rule can be expressed as an AST/regex check, write the check — don't rely on remembering it.
4. **Delegate investigation, keep decisions.** Use a read-only subagent to trace "where does this SLA value come from", "which findings rule fires for X", or to sweep many files for a pattern. Keep the actual code edits and cross-file coupling decisions (e.g. touching both `_norm()` in Python and `_normWf()` in JS) in the main session. Never ask an investigator agent to also make the fix.
5. **Verify a thing is missing before building it — and don't trust stale docs about this repo's own state.** Repo memory/CLAUDE.md can drift from the code (this happened: notes claimed `start.bat` already ran `_validate_js.py` and that `DB_MEM_BAND_HIGH`/`DB_MEM_WARN` were already added to `pe_config.py` — neither was true when checked). Grep the actual file before assuming a fix, a constant, or a wiring already exists.
6. **Report outcomes faithfully.** If a gate wasn't run, a test wasn't mutated, or a screen wasn't manually checked, say so plainly instead of implying it was verified.

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

## Conventions

### One-owner-per-file (safe to parallelize)
| File | Owner concern |
|---|---|
| `routers/*.py` | One router file = one data pillar's HTTP surface. Don't add cross-pillar logic here — put it in `services/`. |
| `services/pe_config.py` | Thresholds ONLY. Every other file reads from it; nothing else should define a threshold constant. |
| `services/session_cache.py` | The only place that touches `.pe_cache.json` / in-memory `ac_*` state. |
| `static/app.js` | Main dashboard render/upload logic — single 14k-line file, edit narrowly (see gotcha below). |
| `static/deep_dive.js` | Azure timeseries/heatmap rendering only — do not duplicate chart logic from `app.js`. |

### Lockstep pairs (must change together, or drift silently breaks something)
- `_norm()` (Python, multiple `services/*.py`) ↔ `_normWf()` (`static/app.js`) — workflow key normalization. Change one, change both, or SLA lookups silently miss.
- Buffer formula `(SLA_h − runtime_h) / SLA_h × 100` — duplicated in `routers/sla_matrix.py`, `services/sla_merger.py`, `services/compliance_engine.py`, `services/batch_questions.py`, and the frontend legend. Any threshold/formula change must be applied to `services/pe_config.py` first, then every consumer re-reads via `_pc.*` — never hardcode the numbers a second time.
- Any new `_pc.NAME` reference in `routers/`/`services/` MUST have a matching module-level constant in `services/pe_config.py` (+ `reload()` global decl + body line if Settings-overridable). `_check_pe_config_refs.py` enforces this — see The Gate.

## PE Review Writing Style
- Direct, factual — no hedging or AI fluff
- Lead with numbers: "59,316 SKU" not "the SKU volume is approximately..."
- Use parenthetical specifics: "(19/19)", "(~4.03 hours)", "(33% buffer)"
- Status markers: "✓ COMPLIANT", "APPROVED"
- 4 sections: Data Volume, Batch SLA, Infrastructure, UAT

## Hard-Won Gotchas
1. **A duplicate `const`/`let` in `app.js` kills the ENTIRE file silently.** `"use strict"` at the top means one duplicate declaration in the same function scope throws a `SyntaxError` that kills every function in the 14k-line file — `window.appData` becomes `undefined`, buttons do nothing, no console error surfaces to the user. Rule: before adding a variable near existing code, grep for that name in the same function scope. Always run `_validate_js.py` after touching `app.js`.
2. **A `_pc.CONSTANT` reference without a matching definition in `pe_config.py` is an `AttributeError` waiting for the right upload to trigger it.** This has happened twice for real (`DB_MEM_BAND_LOW`, `RESOURCE_CAPTURE_DAYS`) and was caught a third time by `_check_pe_config_refs.py` during a repo audit (`DB_MEM_BAND_HIGH`/`DB_MEM_WARN` referenced in `routers/redflags.py` with no definition in `pe_config.py`, live in the repo until fixed). Rule: run `_check_pe_config_refs.py`, don't rely on remembering to grep.
3. **`float(NaN) or 0` returns `NaN`, not `0`.** Always guard with `math.isnan()` explicitly. Same class of bug for division: guard with `np.nan` then `.fillna(-100)` — a bare `/0` silently produces `inf`/`NaN` that propagates into a customer-facing percentage.
4. **Azure auth has 4 separately-discovered corporate-machine hangs, all permanently patched — do not undo any of them:** `DefaultAzureCredential` (IMDS probe, 30s+ hang — banned, never use as a fallback), `TokenCachePersistenceOptions` (DPAPI hang on Python 3.14 free-threaded — banned), restoring `platform.platform`/`platform.uname` after patching them at import time (WMI hang — must stay patched permanently, not just during import), and IPv6 DNS resolution to `login.microsoftonline.com` (must force `AF_INET`). See `/memories/repo/azure-auth-fixes.md`.
5. **Client/server duplicated logic drifts unless changed together.** `_norm()`/`_normWf()` and the buffer formula are computed in multiple files (see Conventions below) — a fix applied to only one side produces a dashboard where two panels disagree on the same job's status.

## Known Patterns & Fixes
- **Dev mode**: Use `dev.bat` or `--reload` — auto-restarts on file changes, no manual stop/start
- **Production**: Use `start.bat` — validates JS + pe_config references before launching (both block startup on failure)
- **JS validation**: `py _validate_js.py` — run before shipping zip to customers
- **pe_config reference validation**: `py _check_pe_config_refs.py` — run before shipping zip to customers
- `annual_fee` safe format: `f"{float(_fee_raw):,.0f}"` (catches None)
- Mixed date formats: `_parse_dt()` multi-pass with dayfirst=True fallback
- Run_Sec inferred from End-Start capped at 168h (1 week)
- SOW engagement keys wiped on server restart (lifespan handler)
- Session boundary: sessionStorage-based tracking (new tab = fresh, reload = restore)
