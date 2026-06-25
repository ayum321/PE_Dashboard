"""
File upload router for the PE Audit Dashboard.

POST /api/upload
    Accepts a single PDF or DOCX resource-utilization file.
    Routes to text parser → if image-only, enriches via Gemini Vision.
    Returns a JSON payload the frontend can render.

POST /api/smart-upload
    Accepts ANY file (CSV, XLSX, PDF, DOCX, TXT, HTML).
    Auto-classifies the file type and routes to the right engine.
    Returns { type, data, classification } — frontend routes display.
"""
from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from services import config_store
from services.resource_parser import get_health_score
from services.resource_parser_generic import parse_resource_file
from services.sla_parser import detect_resource_mode
from services.smart_router import classify

router = APIRouter()

# ── Constants ───────────────────────────────────────────────────
ALLOWED_EXTENSIONS     = {".pdf", ".docx", ".csv", ".xlsx", ".xls"}
SMART_UPLOAD_ALLOWED   = {".pdf", ".docx", ".csv", ".xlsx", ".xls", ".txt", ".html", ".htm"}
MAX_FILE_BYTES         = 50 * 1024 * 1024  # 50 MB


# ── Pydantic response models ────────────────────────────────────
class ServerRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    host:            str
    type:            str   = "APP"
    label:           Optional[str] = None
    cpu_used:        float = 0.0
    cpu_avg:         float = 0.0
    mem_used:        float = 0.0
    mem_total_gb:    float = 0.0
    disk_used_max:   float = 0.0
    disks:           Dict[str, float] = Field(default_factory=dict)
    health_score:    float = 0.0
    image_only:      bool  = False
    vision_enriched: bool  = False


class UploadResponse(BaseModel):
    filename:         str
    file_type:        str
    server_count:     int
    image_only:       bool
    vision_attempted: bool = False
    customer_name:    Optional[str] = None   # extracted from document heading
    servers:          List[ServerRecord]
    ai_summary:       Optional[str] = None   # post-upload Gemma/Llama briefing
    ai_model:         Optional[str] = None


class SmartUploadResponse(BaseModel):
    filename:       str
    classification: Dict[str, Any]    # { type, confidence, notes }
    data:           Dict[str, Any]    # engine-specific result payload


# ── Helpers ─────────────────────────────────────────────────────
def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _enrich(record: Dict[str, Any], image_only: bool) -> Dict[str, Any]:
    def _safe_float(v):
        try:
            f = float(v or 0)
            return f if f == f else 0.0  # NaN guard
        except (ValueError, TypeError):
            return 0.0
    cpu  = _safe_float(record.get("cpu_used"))
    mem  = _safe_float(record.get("mem_used"))
    disk = _safe_float(record.get("disk_used_max"))
    stype = record.get("type", "APP") or "APP"

    record["health_score"]    = float(get_health_score(cpu, mem, disk, stype))
    record["image_only"]      = bool(record.get("_image_only", image_only))
    record["vision_enriched"] = bool(record.get("_vision_enriched", False))
    return record


def _run_vision_enrichment(raw: bytes, filename: str, servers: list) -> list:
    """Call Gemini Vision to fill in metrics for image-only servers (parallel)."""
    try:
        from services.gemini_vision import enrich_servers_with_vision
        api_key = config_store.get_gemini_key()
        if not api_key:
            return servers
        enriched = enrich_servers_with_vision(
            raw, filename, servers, api_key,
            max_images=60, max_workers=8,
        )
        return enriched
    except Exception:
        return servers


def _run_post_upload_summary(
    *, kind: str, filename: str,
    servers: Optional[list] = None,
    payload: Optional[dict] = None,
    customer_name: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Generate a short post-upload AI briefing using the unified ai_engine.

    Triggered automatically after every /api/upload and /api/smart-upload
    so the UI can surface 'what this file actually shows' before the user
    clicks anything else.  Returns (text, model_id) or (None, None) when
    AI is disabled / unavailable.
    """
    try:
        if not bool(config_store.get("ai_post_upload", True)):
            return None, None
        from services.ai_engine import chat as _ai_chat
    except Exception:
        return None, None

    # Build a compact, model-friendly digest of the upload
    if kind == "resource":
        rows = []
        for s in (servers or [])[:40]:
            if s.get("image_only"):
                continue
            rows.append({
                "host": (s.get("host") or "?").split(".")[0],
                "type": s.get("type", "APP"),
                "cpu":  round(float(s.get("cpu_used")  or 0), 1),
                "mem":  round(float(s.get("mem_used")  or 0), 1),
                "disk": round(float(s.get("disk_used_max") or 0), 1),
            })
        digest = {
            "file":     filename,
            "customer": customer_name or "",
            "servers":  rows,
            "counts": {
                "total":      len(servers or []),
                "with_data":  sum(1 for s in (servers or [])
                                  if (float(s.get("cpu_used") or 0) > 0
                                      or float(s.get("mem_used") or 0) > 0
                                      or float(s.get("disk_used_max") or 0) > 0)),
                "image_only": sum(1 for s in (servers or []) if s.get("image_only")),
            },
        }
        prompt = (
            "You just received a server resource utilization report. Read the "
            "JSON digest below and write a 5-line briefing covering: (1) fleet "
            "health one-liner, (2) top 3 servers by CPU/MEM/DISK with exact %, "
            "(3) any servers that look mis-classified, (4) one risk to watch, "
            "(5) one immediate action.  Use hostnames. No filler.\n\n"
            f"DIGEST: {payload or digest}"
        )
    elif kind == "batch":
        prompt = (
            "You just received a Ctrl-M batch run report. Write a 4-line "
            "briefing covering: SLA compliance, top breaching jobs (with hours), "
            "trend vs prior runs if visible, and one immediate action.\n\n"
            f"DIGEST: {payload or {}}"
        )
    else:
        prompt = (
            f"Briefly summarize this uploaded {kind} report in 4 lines: what "
            f"the data shows, biggest signal, biggest risk, recommended next step.\n\n"
            f"DIGEST: {payload or {}}"
        )

    try:
        text, model_id = _ai_chat(
            prompt,
            system=("You are a Senior Performance Engineering consultant. "
                    "Be terse, specific, and quote exact numbers."),
            max_tokens=512,
            temperature=0.3,
        )
        return text.strip() or None, model_id
    except Exception as exc:
        # Never break the upload because the LLM is down
        import logging
        logging.getLogger("pe_dashboard.upload").info(
            "ai post-upload summary skipped: %s", exc,
        )
        return None, None


# ── /api/upload (resource only) ──────────────────────────────────
@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Upload a Zabbix PDF or DOCX resource utilization report",
)
async def upload(file: UploadFile = File(...)) -> UploadResponse:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = _ext(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds 50 MB limit.")

    vision_attempted = False

    try:
        # RULE 4 — parse_resource_file() calls detect_resource_mode() internally.
        # save_resource_session() clears stale keys from the previous upload.
        servers_raw: List[Dict[str, Any]] = parse_resource_file(raw, file.filename)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to parse {file.filename}: {exc}") from exc

    # image_only = True when ALL servers have zero metrics (no text data at all).
    def _has_data(s):
        return (float(s.get("cpu_used") or 0) > 0
                or float(s.get("mem_used") or 0) > 0
                or float(s.get("disk_used_max") or 0) > 0)

    image_only = not any(_has_data(s) for s in (servers_raw or []))

    # Trigger Gemini Vision if:
    #   • The entire file is image-only (no text metrics at all), OR
    #   • At least ONE server has all-zero metrics (mixed files: some text, some image charts).
    # This fixes silent miss-enrichment in Leonardo/Distell DOCXs where the file
    # has one text-parseable server but the rest are chart screenshots.
    # Skip Vision for CSV/XLSX — those are structured data, not image charts.
    _ext_lower = ext.lower()
    _skip_vision = _ext_lower in (".csv", ".xlsx", ".xls")
    _zero_servers = [s for s in (servers_raw or []) if not _has_data(s)]

    if not _skip_vision and (image_only or _zero_servers) and config_store.get_gemini_key():
        vision_attempted = True
        servers_raw = _run_vision_enrichment(raw, file.filename, servers_raw or [])
        # Recalculate image_only after enrichment
        image_only = not any(_has_data(s) for s in (servers_raw or []))

    # Customer name is ONLY extracted from Ctrl-M filenames (see routers/batch.py).
    # Resource uploads never contribute to customer identity.
    customer_name: Optional[str] = None

    enriched = [_enrich(dict(s), image_only) for s in (servers_raw or [])]

    # Cache the last resource snapshot so cross-pillar engines (SLA matrix
    # adaptive baselines, correlation) can read it without an extra request.
    try:
        from services import session_cache
        res_payload = {"servers": enriched}
        session_cache.set("last_resource", res_payload)
        # ── Audit context E4: resource_summary ────────────────────────
        session_cache.ac_set("resource_summary", res_payload)
    except Exception:
        pass

    # ── Post-upload AI summary (Gemma→Llama→Gemini waterfall) ───────
    ai_summary, ai_model = _run_post_upload_summary(
        kind="resource", filename=file.filename,
        servers=enriched, customer_name=customer_name,
    )

    return UploadResponse(
        filename=file.filename,
        file_type=ext.lstrip("."),
        server_count=len(enriched),
        image_only=bool(image_only),
        vision_attempted=vision_attempted,
        customer_name=customer_name,
        servers=enriched,
        ai_summary=ai_summary,
        ai_model=ai_model,
    )


# ── /api/smart-upload (any file type) ────────────────────────────
@router.post(
    "/smart-upload",
    response_model=SmartUploadResponse,
    status_code=status.HTTP_200_OK,
    summary="Auto-classify and route any PE document file",
)
async def smart_upload(file: UploadFile = File(...)) -> SmartUploadResponse:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = _ext(file.filename)
    if ext not in SMART_UPLOAD_ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=f"File type '{ext}' not supported for smart upload.",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    # Classify
    classification = classify(raw, file.filename)
    file_type = classification["type"]

    # Route to appropriate engine
    data: Dict[str, Any] = {}

    try:
        if file_type == "batch":
            from services.batch_calculator import build_batch_payload, load_ctrlm_bytes
            df = load_ctrlm_bytes(raw, file.filename)
            data = build_batch_payload(df)
            data["filename"] = file.filename

        elif file_type == "resource":
            # RULE 4 — single entry point; detect_resource_mode + session clear inside
            servers_raw = parse_resource_file(raw, file.filename)
            img_only = not any(
                s.get("cpu_used", 0) > 0 or s.get("mem_used", 0) > 0
                for s in (servers_raw or [])
            )

            # Vision enrichment if no text metrics found
            if img_only and config_store.get_gemini_key():
                servers_raw = _run_vision_enrichment(raw, file.filename, servers_raw or [])

            enriched = [_enrich(dict(s), img_only) for s in (servers_raw or [])]
            data = {
                "filename": file.filename,
                "file_type": ext.lstrip("."),
                "server_count": len(enriched),
                "image_only": img_only,
                "servers": enriched,
            }

        elif file_type == "sla_matrix":
            from services.batch_calculator import load_ctrlm_bytes
            from routers.sla_matrix import _compute_sla_matrix
            df = load_ctrlm_bytes(raw, file.filename)
            result = _compute_sla_matrix(df, "daily", None)
            data = result.model_dump()
            data["filename"] = file.filename

        elif file_type == "benchmark":
            from routers.benchmark import _parse_benchmark_file, _compute_benchmark
            rows = _parse_benchmark_file(raw, file.filename)
            threshold = float(config_store.get("benchmark_threshold", 10.0))
            result_rows, summary = _compute_benchmark(rows, threshold)
            data = {
                "filename": file.filename,
                "rows": [r.model_dump() for r in result_rows],
                "summary": summary,
                "threshold_pct": threshold,
            }

        elif file_type == "kpi_txt":
            # Store raw text for display
            text = raw.decode("utf-8", errors="replace")
            data = {"filename": file.filename, "text": text[:10_000]}

        elif file_type == "awr":
            # Basic AWR info extraction
            text = raw.decode("utf-8", errors="replace")[:5_000]
            data = {"filename": file.filename, "text": text}

        else:  # extra
            text = raw.decode("utf-8", errors="replace")[:10_000]
            data = {"filename": file.filename, "text": text}

    except Exception as exc:
        data = {"filename": file.filename, "error": str(exc)[:300]}

    # ── Post-upload AI briefing (best-effort) ────────────────────
    if not data.get("error"):
        ai_text, ai_model = _run_post_upload_summary(
            kind=file_type,
            filename=file.filename,
            servers=data.get("servers"),
            payload={k: v for k, v in data.items() if k != "servers"},
        )
        if ai_text:
            data["ai_summary"] = ai_text
            data["ai_model"]   = ai_model

    return SmartUploadResponse(
        filename=file.filename,
        classification=classification,
        data=data,
    )


# ── /api/detect-environment ──────────────────────────────────────
class EnvDetectRequest(BaseModel):
    """Request body for environment detection."""
    files: List[Dict[str, Any]]  # [{filename, rows: [{Job_Name, ...}]}]


class EnvDetectResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    results: List[Dict[str, Any]]
    comparison: Optional[Dict[str, Any]] = None


@router.post(
    "/detect-environment",
    response_model=EnvDetectResponse,
    summary="Auto-detect PROD/TEST/UAT/etc from file metadata",
)
async def detect_env(body: EnvDetectRequest) -> EnvDetectResponse:
    """Detect environment from uploaded file names and row-level metadata.

    Uses weighted evidence stacking across filename, Job_Name, Sub_Application,
    server hostname, and other metadata fields. Returns confidence scores and
    signals so the UI can present a pre-analysis review panel.
    """
    from services.env_detector import detect_multi, compare_environments

    results = detect_multi(body.files)
    comparison = compare_environments(results)

    return EnvDetectResponse(
        results=[r.to_dict() for r in results],
        comparison=comparison.to_dict(),
    )


# ── /api/sla-ceilings (SLA window extraction) ────────────────────
@router.post(
    "/sla-ceilings",
    summary="Extract SLA window ceilings from a customer SLA Matrix XLSX",
)
async def extract_sla_ceilings(file: UploadFile = File(...)) -> Dict[str, Any]:
    """
    Parse a customer SLA Matrix XLSX and return SLA window (hours) per batch type.

    Looks for Start Time + Expected End Time columns (any column name variant).
    Falls back to pe_config defaults when parsing fails or file isn't SLA format.

    RULE 2: These ceilings are stored client-side as `appData.slaCeilings` and
    passed to /api/generate-findings so job_status() uses customer-specific values.

    Response example: {"DAILY": 4.0, "WEEKLY": 6.0, "MONTHLY": 8.0, "CUSTOM": 6.0}
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = _ext(file.filename)
    if ext not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(
            status_code=415,
            detail=f"SLA ceilings endpoint accepts .xlsx, .xls or .csv — got '{ext}'",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    from services.sla_engine import ingest_sla_file, build_sla_traceability
    from services.sla_parser import extract_sla_from_xlsx

    # Run SLA intelligence engine first
    sla_intelligence = ingest_sla_file(raw, file.filename)

    # Use engine ceilings if available, else fall back to legacy parser
    sla_map = sla_intelligence.ceilings if sla_intelligence.valid_rows > 0 else extract_sla_from_xlsx(raw)

    # Persist extracted ceilings in config_store so batch_calculator and
    # sla_matrix read the customer-specific limits instead of defaults.
    _SLA_KEY_MAP = {
        "DAILY": "daily_sla_hrs", "WEEKLY": "weekly_sla_hrs",
        "MONTHLY": "monthly_sla_hrs", "CUSTOM": "custom_sla_hrs",
    }
    for sla_key, hrs in sla_map.items():
        cfg_key = _SLA_KEY_MAP.get(sla_key)
        if cfg_key:
            config_store.set(cfg_key, hrs)

    # Persist SLA intelligence for downstream consumers — run through the
    # display gate first to strip parser-noise contracts before any UI sees them.
    try:
        from services.display_gate import gate as _gate
        _intel_dict = _gate(sla_intelligence.to_dict(), kind="sla_intel")
    except Exception:
        _intel_dict = sla_intelligence.to_dict()
    config_store.set("_sla_intelligence", _intel_dict)
    config_store.set("_sla_source_type",
                     "sla_matrix" if (_intel_dict.get("valid_rows") or 0) > 0 else "default")

    # Reload pe_config so module-level constants pick up new values
    from services import pe_config
    pe_config.reload()

    # GAP-1/GAP-3: recompute batch KPIs with the new SLA ceilings immediately.
    _recompute_status = "no_ctrlm_data_cached"
    try:
        from services.batch_calculator import recompute_with_new_sla as _rwns_c
        from services import session_cache as _sc_ceil
        _new_p = _rwns_c()
        if _new_p is not None:
            _new_k = _new_p.get("kpis")
            if _new_k:
                _lb = _sc_ceil.get("last_batch") or {}
                if _lb:
                    _lb["kpis"] = _new_k
                    _sc_ceil.set("last_batch", _lb)
                _sc_ceil.ac_set("batch_kpis", _new_k)
            _recompute_status = "recomputed"
    except Exception as _e:
        import logging as _log_c
        _log_c.getLogger("pe_dashboard").warning("sla-ceilings recompute failed: %s", _e)

    return {**sla_map, "recompute_status": _recompute_status}


# ── /api/sla-intelligence (full SLA analysis) ────────────────────
@router.post(
    "/sla-intelligence",
    summary="Ingest SLA file and return full intelligence analysis",
)
async def sla_intelligence(file: UploadFile = File(...)) -> Dict[str, Any]:
    """Ingest an SLA file through the SLA intelligence engine.

    Returns the full schema detection, contract extraction, schedule
    classification, and source traceability — not just ceilings.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = _ext(file.filename)
    if ext not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(
            status_code=415,
            detail=f"SLA intelligence accepts .xlsx, .xls or .csv — got '{ext}'",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    from services.sla_engine import ingest_sla_file, build_sla_traceability

    result = ingest_sla_file(raw, file.filename)

    # Also persist ceilings + intelligence
    _SLA_KEY_MAP = {
        "DAILY": "daily_sla_hrs", "WEEKLY": "weekly_sla_hrs",
        "MONTHLY": "monthly_sla_hrs", "CUSTOM": "custom_sla_hrs",
    }
    for sla_key, hrs in result.ceilings.items():
        cfg_key = _SLA_KEY_MAP.get(sla_key)
        if cfg_key:
            config_store.set(cfg_key, hrs)

    # Run the display gate to strip parser-noise contracts before the UI
    # ever sees them. This keeps the "Resolved SLA Rules" panel concise.
    try:
        from services.display_gate import gate as _gate
        intel_dict = _gate(result.to_dict(), kind="sla_intel")
    except Exception:
        intel_dict = result.to_dict()

    config_store.set("_sla_intelligence", intel_dict)
    config_store.set("_sla_source_type",
                     "sla_matrix" if (intel_dict.get("valid_rows") or 0) > 0 else "default")

    from services import pe_config
    pe_config.reload()

    # ── GAP-1/GAP-3: Silent batch KPI recompute — use new SLA ceilings immediately ──
    # When this SLA file is uploaded AFTER Ctrl-M, all prior gauge/compliance
    # numbers used the old (default 6h) ceiling.  Re-running compute_metrics
    # against the persisted job_runs_df picks up the customer's actual SLA.
    _batch_refreshed = False
    _updated_batch_kpis: dict | None = None
    try:
        from services.batch_calculator import recompute_with_new_sla as _rwns_ir
        from services import session_cache as _sc_ir
        _new_payload = _rwns_ir()
        if _new_payload is not None:
            _updated_batch_kpis = _new_payload.get("kpis")
            if _updated_batch_kpis:
                _last_batch = _sc_ir.get("last_batch") or {}
                if _last_batch:
                    _last_batch["kpis"] = _updated_batch_kpis
                    _sc_ir.set("last_batch", _last_batch)
                _sc_ir.ac_set("batch_kpis", _updated_batch_kpis)
            _batch_refreshed = True
    except Exception:
        pass

    return {
        "ceilings":             result.ceilings,
        "traceability":         build_sla_traceability(result),
        "intelligence":         intel_dict,
        "batch_refreshed":      _batch_refreshed,
        "updated_batch_kpis":   _updated_batch_kpis,
    }


# ── /api/batch-sla/upload — BatchSLA_info.xlsx workflow SLA intake ───────────
@router.post(
    "/batch-sla/upload",
    status_code=status.HTTP_200_OK,
    summary="Upload BatchSLA_info.xlsx — workflow-level SLA contracts (Tier 1 source)",
)
async def upload_batch_sla(file: UploadFile = File(...)) -> dict:
    """
    Parse a BatchSLA_info.xlsx (or any workflow-SLA spreadsheet).

    Stores parsed workflow rows to config_store under '_batch_sla_xlsx' so the
    SLA matrix resolver can use them as Tier 1 (most specific) SLA source.

    Returns the parsed workflow list + summary for the UI status card.
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    ext = _ext(file.filename)
    if ext not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(
            status_code=415,
            detail=f"Workflow SLA file must be .xlsx, .xls or .csv — got '{ext}'",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    from services.sla_merger import parse_batch_sla_xlsx
    result = parse_batch_sla_xlsx(raw, file.filename)

    # Persist workflow SLA rows for the 3-tier SLA resolver
    config_store.set("_batch_sla_xlsx", result)

    # ── Extract per-schedule MAX ceilings from workflows → config_store ───
    # Use MAX (widest) SLA per schedule type as the global batch window ceiling.
    # Per-workflow compliance is handled by workflow_sla_summary.
    _SLA_KEY_MAP = {
        "DAILY": "daily_sla_hrs", "WEEKLY": "weekly_sla_hrs",
        "MONTHLY": "monthly_sla_hrs", "CUSTOM": "custom_sla_hrs",
    }
    _ceil: dict[str, float] = {}
    for w in (result.get("workflows") or []):
        sla_h = w.get("sla_hours")
        sched = (w.get("batch_type") or "").upper()
        if sla_h and sla_h > 0 and sched in _SLA_KEY_MAP:
            if sched not in _ceil or sla_h > _ceil[sched]:
                _ceil[sched] = sla_h
    for sla_key, hrs in _ceil.items():
        cfg_key = _SLA_KEY_MAP.get(sla_key)
        if cfg_key:
            config_store.set(cfg_key, hrs)
    if _ceil:
        try:
            from services import pe_config
            pe_config.reload()
        except Exception:
            pass

    # Mark source type as batch_sla_xlsx so downstream sla_source detection works
    if (result.get("workflows") or []):
        config_store.set("_sla_source_type", "batch_sla_xlsx")

    # NOTE: BatchSLA workflow rows are persisted to config_store under
    # '_batch_sla_xlsx' (above) — the single source the SLA engines read.
    # They are deliberately NOT mirrored into the 'sow_contract' audit slot:
    # doing so used to make findings/narrative falsely report "SOW volume
    # comparison included" when only a BatchSLA matrix (not a SOW) was uploaded.

    # ── Silent batch KPI recompute so the gauge reflects the new per-job SLAs ──
    # When BatchSLA_info.xlsx is uploaded AFTER Ctrl-M, the gauge and compliance
    # tiles were computed with the old (default 6h) SLA. Re-running compute_metrics
    # against the stored job_runs_df with the updated config_store fixes this.
    _updated_batch_kpis = None
    try:
        from services.batch_calculator import recompute_with_new_sla as _rwns_r
        from services import session_cache as _sc_r
        _new_payload = _rwns_r()
        if _new_payload is not None:
            _updated_batch_kpis = _new_payload.get("kpis")
            if _updated_batch_kpis:
                _last_batch = _sc_r.get("last_batch") or {}
                if _last_batch:
                    _last_batch["kpis"] = _updated_batch_kpis
                    _sc_r.set("last_batch", _last_batch)
                _sc_r.ac_set("batch_kpis", _updated_batch_kpis)
    except Exception:
        pass

    # Summary for UI status badge
    workflows = result.get("workflows") or []
    with_sla       = sum(1 for w in workflows if w.get("sla_hours"))
    with_explicit  = sum(1 for w in workflows if w.get("sla_source") == "BATCH_SLA_XLSX")
    with_fallback  = sum(1 for w in workflows if w.get("sla_source") in ("SOW_EXTRACTED", "GLOBAL_DEFAULT"))
    types = list({w.get("batch_type", "?") for w in workflows})

    return {
        "filename":            file.filename,
        "workflow_count":      len(workflows),
        "with_sla_count":      with_sla,
        "with_explicit_sla":   with_explicit,
        "with_fallback_sla":   with_fallback,
        "batch_types":         sorted(types),
        "warnings":            result.get("warnings") or [],
        "workflows":           workflows,
        # Recomputed batch KPIs using new per-job SLAs — present when Ctrl-M
        # was already uploaded; None when batch hasn't been processed yet.
        "updated_batch_kpis": _updated_batch_kpis,
    }
