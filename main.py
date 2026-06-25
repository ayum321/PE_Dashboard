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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import os

from routers import ai as ai_router
from routers import batch as batch_router
from routers import benchmark as benchmark_router
from routers import config as config_router
from routers import correlation as correlation_router
from routers import executive as executive_router
from routers import export as export_router
from routers import final_judgment as final_judgment_router
from routers import findings as findings_router
from routers import pe_consultant as pe_consultant_router
from routers import pe_narrative as pe_narrative_router
from routers import redflags as redflags_router
from routers import resource as resource_router
from routers import sla_matrix as sla_matrix_router
from routers import sla_intelligence as sla_intelligence_router
from routers import sow as sow_router
from routers import upload as upload_router
from routers import agent as agent_router
from routers import azure_resource as azure_resource_router

# ── Paths ───────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

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
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://127.0.0.1:*,http://localhost:*")
_CORS_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


# ── Static files with cache-busting headers ─────────────────────
# StaticFiles as a mounted sub-app bypasses parent middleware,
# so we serve static files through a catch-all route instead.
from starlette.responses import Response as _StarletteResponse
import hashlib as _hashlib
import mimetypes as _mimetypes

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
    full = STATIC_DIR / file_path
    if not full.resolve().is_relative_to(STATIC_DIR.resolve()) or not full.is_file():
        from fastapi.responses import JSONResponse
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
app.include_router(batch_router.router,       prefix="/api", tags=["batch"])
app.include_router(resource_router.router,    prefix="/api", tags=["resource"])
app.include_router(export_router.router,      prefix="/api", tags=["export"])
app.include_router(findings_router.router,    prefix="/api", tags=["findings"])
app.include_router(ai_router.router,          prefix="/api", tags=["ai"])
app.include_router(correlation_router.router, prefix="/api", tags=["correlation"])
app.include_router(executive_router.router,   prefix="/api", tags=["executive"])
app.include_router(redflags_router.router,    prefix="/api", tags=["redflags"])
app.include_router(sla_matrix_router.router,  prefix="/api", tags=["sla-matrix"])
app.include_router(sla_intelligence_router.router, prefix="/api", tags=["sla-intelligence"])
app.include_router(benchmark_router.router,   prefix="/api", tags=["benchmark"])
app.include_router(config_router.router,      prefix="/api", tags=["config"])
app.include_router(sow_router.router,         prefix="/api", tags=["sow"])
app.include_router(final_judgment_router.router, prefix="/api", tags=["judgment"])
app.include_router(pe_consultant_router.router, prefix="/api", tags=["consultant"])
app.include_router(pe_narrative_router.router,  prefix="/api", tags=["pe-narrative"])
app.include_router(agent_router.router,         prefix="/api", tags=["agent"])
app.include_router(azure_resource_router.router, prefix="/api", tags=["azure"])


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
    status = {
        "batch":    "loaded" if _f("batch_kpis")        else "missing",
        "sla":      "loaded" if _f("sla_matrix_kpis")   else "missing",
        "resource": "loaded" if _f("resource_summary")  else "missing",
        "sow":      "loaded" if _f("sow_contract")      else "missing",
        "uat":      "loaded" if _f("uat_df")             else "missing",
    }
    loaded = sum(1 for v in status.values() if v == "loaded")
    total  = len(status)

    # Include daily_jobs + hourly_counts from last_batch for concurrency
    # timeline restore on page reload (not stored in ac slots due to size).
    last_batch = session_cache.get("last_batch") or {}
    extra = {}
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
                # Also strip the DEFAULT_SLA data warning from data_coverage
                _dc = last_batch.get("data_coverage") or {}
                if isinstance(_dc.get("warnings"), list):
                    _dc["warnings"] = [
                        w for w in _dc["warnings"]
                        if w.get("code") != "DEFAULT_SLA"
                    ]
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
    """Render the SPA shell. Cache-bust key via content hash of app.js."""
    _v = _file_content_hash(STATIC_DIR / "app.js")
    response = templates.TemplateResponse(request, "index.html", {"static_v": _v})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Serve SVG favicon — eliminates the 404 in browser console."""
    ico = BASE_DIR / "static" / "favicon.svg"
    if ico.exists():
        return FileResponse(str(ico), media_type="image/svg+xml")
    return RedirectResponse("/static/favicon.svg")


_PE_IDENTITY = "pe-audit-dashboard"

@app.get("/api/health", tags=["meta"])
async def health() -> dict:
    """Liveness probe + identity. start.bat uses 'service' to verify
    no foreign app is squatting on this port."""
    return {"status": "ok", "service": _PE_IDENTITY, "version": app.version,
            "pid": os.getpid()}
