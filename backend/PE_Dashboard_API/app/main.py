"""
PE Audit Dashboard — FastAPI entrypoint.

Stateless backend that replaces the original Streamlit monolith
(`app_v2.py`). Serves the Jinja2 shell at `/`, mounts static assets
at `/static`, and exposes `/api/*` REST endpoints.

Run locally:
    uvicorn main:app --host 127.0.0.1 --port 8765 --reload
or use the bundled `start.bat`.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os

from routers import archive as archive_router
from routers import batch as batch_router
from routers import benchmark as benchmark_router
from routers import config as config_router
from routers import correlation as correlation_router
from routers import executive as executive_router
from routers import export as export_router
from routers import final_judgment as final_judgment_router
from routers import findings as findings_router
from routers import redflags as redflags_router
from routers import resource as resource_router
from routers import sla_matrix as sla_matrix_router
from routers import sla_intelligence as sla_intelligence_router
from routers import sow as sow_router
from routers import upload as upload_router
from routers import azure_resource as azure_resource_router

# ── AI routers ──────────────────────────────────────────────────
# pe_narrative + pe_consultant build a COMPLETE deterministic review (4-part
# conclusive verdict + cross-links) with AI off; the LLM only rewrites prose.
# They must always mount so the conclusive review never disappears. Only the
# pure-AI routers (ai, agent) are gated behind AI_ENABLED.
from services import pe_config
from routers import pe_consultant as pe_consultant_router
from routers import pe_narrative as pe_narrative_router
if pe_config.AI_ENABLED:
    from routers import ai as ai_router
    from routers import agent as agent_router

# ── Paths ───────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
# FastAPI remains the processing API for both local experiences.  Only the
# retired browser UI lives outside it so Portal MFE deployments cannot serve
# legacy HTML by accident.
PROJECT_DIR = BASE_DIR.parents[2]
LEGACY_UI_DIR = Path(
    os.environ.get("PE_LEGACY_UI_DIR", PROJECT_DIR / "backend" / "legacy-ui")
).resolve()
LEGACY_STATIC_DIR = LEGACY_UI_DIR / "static"
LEGACY_TEMPLATES_DIR = LEGACY_UI_DIR / "templates"
UI_MODE = os.environ.get("PE_UI_MODE", "api").strip().lower()
if UI_MODE not in {"api", "legacy", "dual", "bundled_mfe"}:
    raise RuntimeError("PE_UI_MODE must be one of: api, legacy, dual, bundled_mfe")
MFE_DIR = BASE_DIR / "mfe"

# ── Engagement-specific keys that must NEVER outlive a session ──
# These keys hold data from a specific customer's SOW upload.
# On every server start we wipe them so the dashboard is always
# blank until the user explicitly uploads files.
_SOW_ENGAGEMENT_KEYS = (
    "sow_baseline",
    "sow_dfu", "sow_sku", "sow_orders", "sow_batch_jobs",
    "_sow_sla_windows",
    "_sow_volume_by_year",
    "_sow_contract_meta",
    "customer_name",
    "customer_name_confidence",
    "customer_name_source",
)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Startup: wipe all engagement-specific SOW data so the dashboard
    always starts blank for SOW-related panels, regardless of what the
    previous session left behind in .pe_config.json.

    Batch/resource/SLA data in session_cache is NOT cleared here — those
    use _PERSIST_AC_SLOTS and are intentionally preserved within a session.
    Only config_store SOW engagement keys are reset."""
    from services import config_store
    for key in _SOW_ENGAGEMENT_KEYS:
        if key == "customer_name":
            config_store.set(key, "")
        elif key == "customer_name_confidence":
            config_store.set(key, 0)
        elif key == "customer_name_source":
            config_store.set(key, "")
        else:
            config_store.set(key, {})
    yield
    # (no shutdown logic needed)


# ── App ─────────────────────────────────────────────────────────
app = FastAPI(
    title="PE Audit Dashboard",
    description="Stateless FastAPI backend for the Performance Engineering Audit Dashboard.",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — origins controlled via ALLOWED_ORIGINS env var (comma-separated).
# Defaults to localhost only for safety; set ALLOWED_ORIGINS='*' only for
# isolated local dev that never faces a network.
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://127.0.0.1:3000,http://localhost:3000",
)
_CORS_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    # Must be True (with explicit, non-wildcard origins above) so the React
    # MFE's cross-origin fetches carry the pe_sid session cookie — without
    # this, every Azure/session-scoped call from the MFE looks like a brand
    # new anonymous session and "Connect Azure" never appears connected.
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    # The React MFE reads these response headers after an HTML export.  CORS
    # hides non-safelisted headers unless they are explicitly exposed, which
    # previously made a successful archive save look "unknown" in Governance.
    expose_headers=["Content-Disposition", "X-Archive-Status", "X-Audit-Id"],
)


# ── Static files with cache-busting headers ─────────────────────
# StaticFiles as a mounted sub-app bypasses parent middleware,
# so we serve static files through a catch-all route instead.
from starlette.responses import Response as _StarletteResponse
import hashlib as _hashlib
import mimetypes as _mimetypes

legacy_templates = Jinja2Templates(directory=str(LEGACY_TEMPLATES_DIR))

_STATIC_MIME_OVERRIDES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

def _file_content_hash(path: Path) -> str:
    """MD5 hex digest of a file's contents for cache-busting."""
    h = _hashlib.md5(usedforsecurity=False)
    h.update(path.read_bytes())
    return h.hexdigest()[:12]

@app.get("/static/{file_path:path}", include_in_schema=False)
async def serve_static(file_path: str):
    """Serve static files with no-cache headers and ETag so code
    changes propagate immediately on browser reload."""
    if UI_MODE in {"legacy", "dual", "api"}:
        full = LEGACY_STATIC_DIR / file_path
        static_root = LEGACY_STATIC_DIR.resolve()
        if not full.resolve().is_relative_to(static_root) or not full.is_file():
            return JSONResponse({"detail": "Not found"}, status_code=404)
    elif UI_MODE == "bundled_mfe":
        mfe_static_root = (MFE_DIR / "static").resolve()
        mfe_full = MFE_DIR / "static" / file_path
        if not mfe_full.resolve().is_relative_to(mfe_static_root) or not mfe_full.is_file():
            return JSONResponse({"detail": "Not found"}, status_code=404)
        full = mfe_full
    else:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    suffix = full.suffix.lower()
    media = _STATIC_MIME_OVERRIDES.get(suffix) or _mimetypes.guess_type(str(full))[0] or "application/octet-stream"
    etag = _file_content_hash(full)
    resp = FileResponse(full, media_type=media)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["ETag"] = f'"{etag}"'
    return resp

# ── Routers ─────────────────────────────────────────────────────
app.include_router(upload_router.router,      prefix="/api", tags=["upload"])
app.include_router(archive_router.router,     prefix="/api", tags=["archive"])
app.include_router(batch_router.router,       prefix="/api", tags=["batch"])
app.include_router(resource_router.router,    prefix="/api", tags=["resource"])
app.include_router(export_router.router,      prefix="/api", tags=["export"])
app.include_router(findings_router.router,    prefix="/api", tags=["findings"])
app.include_router(correlation_router.router, prefix="/api", tags=["correlation"])
app.include_router(executive_router.router,   prefix="/api", tags=["executive"])
app.include_router(redflags_router.router,    prefix="/api", tags=["redflags"])
app.include_router(sla_matrix_router.router,  prefix="/api", tags=["sla-matrix"])
app.include_router(sla_intelligence_router.router, prefix="/api", tags=["sla-intelligence"])
app.include_router(benchmark_router.router,   prefix="/api", tags=["benchmark"])
app.include_router(config_router.router,      prefix="/api", tags=["config"])
app.include_router(sow_router.router,         prefix="/api", tags=["sow"])
app.include_router(final_judgment_router.router, prefix="/api", tags=["judgment"])
app.include_router(azure_resource_router.router, prefix="/api", tags=["azure"])

# ── AI routers ────────────────────────────────────────────────────────────────
# pe_narrative + pe_consultant always mount (deterministic core, AI is optional
# prose). ai + agent require a live LLM and stay gated behind AI_ENABLED.
app.include_router(pe_consultant_router.router,  prefix="/api", tags=["consultant"])
app.include_router(pe_narrative_router.router,   prefix="/api", tags=["pe-narrative"])
if pe_config.AI_ENABLED:
    app.include_router(ai_router.router,             prefix="/api", tags=["ai"])
    app.include_router(agent_router.router,          prefix="/api", tags=["agent"])


# ── Audit Context — lightweight status endpoint ─────────────────────────────
@app.get("/api/audit-context", tags=["audit"], summary="Read the shared audit context snapshot")
async def get_audit_context() -> dict:
    """Return the current audit_context: all engine outputs + timestamps.

    Used by the PE Narrative and PE Findings screens to show which pillars
    have real data vs. are still waiting for uploads.

    Returns:
        {
            "slots": {
                "batch_kpis":      {...} | null,
                "job_summary":     [...] | null,
                "sla_matrix_kpis": {...} | null,
                "resource_summary":{...} | null,
                "sow_contract":    {...} | null,
                "volume_vs_sow":   {...} | null,
                "uat_df":          [...] | null,
                ...
            },
            "status": {
                "batch":    "loaded" | "missing",
                "sla":      "loaded" | "missing",
                "resource": "loaded" | "missing",
                "sow":      "loaded" | "missing",
                "uat":      "loaded" | "missing",
            },
            "timestamps": {...},
            "completeness_pct": 0-100
        }
    """
    from services import session_cache
    ac = session_cache.ac_snapshot()
    ts = ac.pop("_timestamps", {})

    _f = lambda k: bool(ac.get(k))  # noqa: E731
    # UAT evidence consists of explicit test cases and/or either performance
    # benchmark source.  Keep the UI and batch uploads separate so Findings
    # can state exactly which evidence was reviewed.
    last_benchmark_ui = session_cache.get("last_benchmark_ui") or {}
    last_benchmark_batch = session_cache.get("last_benchmark_batch") or {}
    status = {
        "batch":    "loaded" if _f("batch_kpis")        else "missing",
        "sla":      "loaded" if _f("sla_matrix_kpis")   else "missing",
        "resource": "loaded" if _f("resource_summary")  else "missing",
        "sow":      "loaded" if _f("sow_contract")      else "missing",
        "uat":      "loaded" if (_f("uat_df") or last_benchmark_ui or last_benchmark_batch) else "missing",
    }
    loaded = sum(1 for v in status.values() if v == "loaded")
    total  = len(status)

    # Include daily_jobs + hourly_counts from last_batch for concurrency
    # timeline restore on page reload (not stored in ac slots due to size).
    last_batch = session_cache.get("last_batch") or {}
    extra = {}
    if last_benchmark_ui:
        extra["last_benchmark_ui"] = last_benchmark_ui
    if last_benchmark_batch:
        extra["last_benchmark_batch"] = last_benchmark_batch
    if last_batch.get("daily_jobs"):
        extra["daily_jobs"] = last_batch["daily_jobs"]
    if last_batch.get("hourly_counts"):
        extra["hourly_counts"] = last_batch["hourly_counts"]

    # Live-patch stale sla_source in last_batch if BatchSLA XLSX has since been
    # uploaded.  Without this, a page reload after BatchSLA upload would still show
    # the amber "No customer SLA matrix" banner because last_batch was persisted
    # with sla_source.type = "default" from before the upload.
    try:
        from services import config_store as _cs_ctx
        _bsla = _cs_ctx.get("_batch_sla_xlsx") or {}
        _src_type = _cs_ctx.get("_sla_source_type") or ""
        if _bsla.get("workflows") and last_batch:
            _src_obj = last_batch.get("sla_source") or {}
            if isinstance(_src_obj, dict) and _src_obj.get("type") in ("default", None, ""):
                _src_obj["type"] = _src_type or "batch_sla_xlsx"
                last_batch["sla_source"] = _src_obj
                _default_job_count = None
                _total_job_count = 0
                try:
                    import pandas as pd
                    from services.batch_calculator import build_sla_index as _build_sla_index

                    _live_rows = (
                        session_cache.get("_last_ctrlm_df_records")
                        or session_cache.ac_get("job_runs_df")
                        or []
                    )
                    if _live_rows:
                        _live_df = pd.DataFrame(_live_rows)
                        _live_excluded = session_cache.ac_get("manual_excluded_jobs")
                        if _live_excluded is not None and "Job_Name" in _live_df.columns:
                            _excluded_names = {
                                str(name).strip()
                                for name in _live_excluded
                                if name and str(name).strip()
                            }
                            if _excluded_names:
                                _live_df = _live_df[
                                    ~_live_df["Job_Name"].astype(str).isin(_excluded_names)
                                ]
                        if not _live_df.empty and "Job_Name" in _live_df.columns:
                            _job_sla = (_build_sla_index(_live_df) or {}).get("job_sla", {})
                            _default_markers = {"", "default", "assumed", "none"}
                            _group_cols = (
                                ["Sub_Application", "Job_Name"]
                                if "Sub_Application" in _live_df.columns
                                else ["Job_Name"]
                            )
                            _default_job_count = 0
                            for _, _job in _live_df[_group_cols].drop_duplicates().iterrows():
                                _job_name = str(_job.get("Job_Name", "")).strip()
                                if not _job_name:
                                    continue
                                _sub_app = (
                                    str(_job.get("Sub_Application", "")).strip()
                                    if "Sub_Application" in _job.index
                                    else ""
                                )
                                _key = f"{_sub_app}|{_job_name}" if _sub_app else _job_name
                                _src = str(
                                    (_job_sla.get(_key) or _job_sla.get(_job_name) or {}).get("source")
                                    or "default"
                                ).strip().lower()
                                _total_job_count += 1
                                if _src in _default_markers:
                                    _default_job_count += 1
                except Exception:
                    _default_job_count = None
                    _total_job_count = 0

                # Only clear the batch-level DEFAULT_SLA warning when every in-scope
                # job resolves away from assumed/default ceilings after the live upload.
                _dc = last_batch.get("data_coverage") or {}
                if isinstance(_dc.get("warnings"), list):
                    if _default_job_count == 0 and _total_job_count > 0:
                        _dc["warnings"] = [
                            w for w in _dc["warnings"]
                            if not isinstance(w, dict) or w.get("code") != "DEFAULT_SLA"
                        ]
                    elif isinstance(_default_job_count, int) and _default_job_count > 0:
                        _warning_text = (
                            f"{_default_job_count} of {_total_job_count} jobs still use "
                            "assumed/default SLA ceilings. Complete the customer SLA "
                            "mappings for accurate compliance measurement."
                        )
                        _updated = False
                        for _warning in _dc["warnings"]:
                            if isinstance(_warning, dict) and _warning.get("code") == "DEFAULT_SLA":
                                _warning["text"] = _warning_text
                                _warning["severity"] = "info"
                                _updated = True
                        if not _updated:
                            _dc["warnings"].append({
                                "code": "DEFAULT_SLA",
                                "text": _warning_text,
                                "severity": "info",
                            })
    except Exception:
        pass

    return {
        "slots":            ac,
        "status":           status,
        "timestamps":       ts,
        "completeness_pct": round(loaded / total * 100),
        "extra":            extra,
    }


# ── Shell route ─────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    """Render only the explicitly selected local UI shell."""
    if UI_MODE in {"legacy", "dual"} and (LEGACY_TEMPLATES_DIR / "index.html").is_file():
        _v = _file_content_hash(LEGACY_STATIC_DIR / "app.js")
        response = legacy_templates.TemplateResponse(request, "index.html", {"static_v": _v})
        response.headers["Cache-Control"] = "no-store"
        return response
    if UI_MODE == "bundled_mfe" and (MFE_DIR / "index.html").is_file():
        return HTMLResponse((MFE_DIR / "index.html").read_text(encoding="utf-8"))
    return JSONResponse({"detail": "PE Audit API is running. Launch a dashboard UI separately."}, status_code=404)


@app.get("/legacy", response_class=HTMLResponse, include_in_schema=False)
async def legacy_index(request: Request) -> HTMLResponse:
    """Expose the local comparison UI without requiring a second API server.

    React uses the same API process from port 3000.  This route is available
    only when the legacy assets are present in a local checkout; the API-only
    Docker image intentionally does not contain those files.
    """
    index_file = LEGACY_TEMPLATES_DIR / "index.html"
    app_js = LEGACY_STATIC_DIR / "app.js"
    if not index_file.is_file() or not app_js.is_file():
        return JSONResponse({"detail": "The local legacy dashboard assets are unavailable."}, status_code=404)
    response = legacy_templates.TemplateResponse(request, "index.html", {"static_v": _file_content_hash(app_js)})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/archive", response_class=HTMLResponse, include_in_schema=False)
async def report_archive_page(request: Request) -> HTMLResponse:
    """Standalone local Report Archive page — separate from the main
    dashboard SPA on purpose, so browsing past reports never risks the
    main app's state/tabs. Data comes from /api/report-archive*."""
    if UI_MODE not in {"legacy", "dual"} or not (LEGACY_TEMPLATES_DIR / "report_archive.html").is_file():
        return JSONResponse({"detail": "The legacy dashboard UI is not running."}, status_code=404)
    response = legacy_templates.TemplateResponse(request, "report_archive.html", {})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Serve SVG favicon — eliminates the 404 in browser console."""
    ico = LEGACY_STATIC_DIR / "favicon.svg"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/svg+xml")
    return RedirectResponse("/static/favicon.svg")


_PE_IDENTITY = "pe-audit-dashboard"

@app.get("/api/health", tags=["meta"])
async def health() -> dict:
    """Liveness probe + identity. start.bat uses 'service' to verify
    no foreign app is squatting on this port."""
    return {"status": "ok", "service": _PE_IDENTITY, "version": app.version,
            "pid": os.getpid(), "ui_mode": UI_MODE}


@app.get("/{file_path:path}", include_in_schema=False)
async def serve_mfe(file_path: str):
    """Serve built React files and let the client router handle SPA routes.

    Docker copies the React build to ``app/mfe``.  A browser refresh at a
    client-side route such as ``/batch`` does not name a physical file, so it
    must receive the SPA shell.  Requests which look like missing assets (or
    API paths) stay 404s: returning HTML for a missing JavaScript file hides
    deployment errors and causes opaque browser failures.
    """
    if UI_MODE != "bundled_mfe" or not MFE_DIR.is_dir() or not file_path:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = (MFE_DIR / file_path).resolve()
    if not candidate.is_relative_to(MFE_DIR.resolve()):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if candidate.is_file():
        return FileResponse(candidate)

    # API routes are never React routes.  A suffix means this was intended to
    # be a physical asset (for example /static/js/main.hash.js), not an SPA
    # navigation target.
    if file_path.startswith("api/") or Path(file_path).suffix:
        return JSONResponse({"detail": "Not found"}, status_code=404)

    index_file = MFE_DIR / "index.html"
    if not index_file.is_file():
        return JSONResponse({"detail": "Not found"}, status_code=404)
    response = FileResponse(index_file, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response
