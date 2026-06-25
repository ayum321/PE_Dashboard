"""
Batch (Ctrl-M) processing router.

Two endpoints:
    POST /api/process-batch
        multipart file upload (.csv / .xlsx / .xls) — parses the file
        with services.batch_calculator.load_ctrlm_bytes and returns the
        KPI + chart-ready JSON payload.

    POST /api/process-batch/json
        accepts pre-parsed Ctrl-M rows as JSON (list of records with
        Job_Name / Start_Time / Run_Sec etc.) — useful when the frontend
        already holds the rows in memory.

Both paths funnel into `batch_calculator.build_batch_payload(df)` which
returns plain dicts (no DataFrames), per the Phase 3 brief.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field

from services.batch_calculator import (
    DAILY_LIMIT_HRS,
    build_batch_payload,
    load_ctrlm_bytes,
)

router = APIRouter()

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_BATCH_FILES = 8


# ── Pydantic response models ────────────────────────────────────
class BatchKPIs(BaseModel):
    model_config = ConfigDict(extra="allow")

    compliance_pct: float = 0.0
    job_sla_compliance_pct: float = 0.0
    window_compliance_pct: float = 0.0
    batch_window_compliance: float = 0.0
    total_runs: int = 0
    total_jobs: int = 0
    total_hrs: float = 0.0
    jobs_breach: int = 0
    jobs_at_risk: int = 0
    jobs_ok: int = 0
    # Execution-status counts (ENDED OK vs ENDED NOT OK / FAILED / ABENDED) —
    # separate from SLA breach which is a performance signal.
    ok_runs:     int = 0
    failed_runs: int = 0
    fail_rate_pct: float = 0.0
    daily_limit_hrs: float = DAILY_LIMIT_HRS
    monthly_limit_hrs: float = 8.0
    fleet_sla_buffer: Optional[Dict[str, Any]] = None


class TopJobRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    Job_Name: str = Field(..., alias="Job_Name")
    peak_hrs: float
    avg_hrs: float
    total_hrs: float
    buffer_pct: Optional[float] = None   # None when SLA quality is INSUFFICIENT
    sla_used_pct: Optional[float] = None  # None when sla_hrs == 0
    buffer_status: str


class WindowPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_date:  str
    total_hrs: float
    job_count: int
    breach:    bool
    top_job:   Optional[str] = None   # top contributing job for chart annotation


class BatchResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    filename: str
    kpis: BatchKPIs
    top_jobs: List[TopJobRow]
    top_breaches: List[TopJobRow]
    window: List[WindowPoint]
    sub_stats: List[Dict[str, Any]]
    anomalies: List[Dict[str, Any]]
    hourly_counts: Optional[Dict[str, Any]] = None
    sla_heatmap: Optional[Dict[str, Any]] = None
    hour_heatmap: Optional[Dict[str, Any]] = None
    # ── Phase 11 new sections ──
    elapsed_window: Optional[Dict[str, Any]] = None
    summed_runtime: Optional[Dict[str, Any]] = None
    worst_job: Optional[Dict[str, Any]] = None
    sla_source: Optional[Dict[str, Any]] = None
    # Full-dataset SLA Matrix (interconnects with /api/generate-findings + /api/red-flags)
    sla_matrix: Optional[Dict[str, Any]] = None
    data_coverage: Optional[Dict[str, Any]] = None
    # ── AI narrative (best-effort, populated by narrator) ──
    ai_narrative: Optional[str] = None
    ai_model: Optional[str] = None
    # Customer name extracted from the Ctrl-M filename (only source the dashboard trusts).
    customer_name: Optional[str] = None
    # Per-day job timings for the concurrency Gantt chart.
    daily_jobs: Optional[Dict[str, Any]] = None
    # Per-sub-app WINDOW rollup (worst daily batch window vs contracted ceiling
    # + breach days). Drives the Executive at-risk panels from the binding
    # window metric instead of peak-vs-window-ceiling.
    window_sub_app: Optional[List[Dict[str, Any]]] = None


class BatchJsonRequest(BaseModel):
    """JSON body for the pre-parsed endpoint."""
    filename: Optional[str] = None
    rows: List[Dict[str, Any]]


# ── Helpers ─────────────────────────────────────────────────────
def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


# Customer-name patterns recognised in Ctrl-M filenames.
# Examples that match:
#   Report_of_CS_ACMECORP_SCPO_ctrlm.csv     -> ACMECORP
#   _CS_ACME_CORP_SCPO_runs.xlsx             -> ACME CORP
#   CUSTOMER_ACMECORP_SCPO.csv               -> ACMECORP
_CUSTOMER_PATTERNS = (
    r"Report_of_CS[_]([A-Z][A-Z0-9_]+?)_SCPO",
    r"_CS_([A-Z][A-Z0-9_]+?)_SCPO",
    r"CUSTOMER_([A-Z][A-Z0-9_]+?)_SCPO",
)


def _extract_customer_from_ctrlm_filename(filename: str) -> Optional[str]:
    """Pull a customer name from a Ctrl-M report filename.

    Only the filename is consulted — never the file contents — so the value
    is deterministic and never inferred from job/server text.
    Returns None if no pattern matches.
    """
    if not filename:
        return None
    stem = re.sub(r"\.(csv|xlsx|xls)$", "", os.path.basename(filename), flags=re.IGNORECASE)
    for pat in _CUSTOMER_PATTERNS:
        m = re.search(pat, stem, flags=re.IGNORECASE)
        if m:
            raw = m.group(1).replace("_", " ").strip()
            if not raw:
                return None
            parts = raw.split()
            return " ".join(p if (len(p) <= 4 and p.isupper()) else p.title() for p in parts)
    return None


def _payload_to_response(
    filename: str,
    payload: Dict[str, Any],
    df=None,
    customer_name: Optional[str] = None,
) -> BatchResponse:
    # Compute full-dataset SLA Matrix from the parsed dataframe so PE Findings
    # + Red Flags + PE Consultant all see ALL runs (not just top_jobs).
    sla_mx_dict: Optional[Dict[str, Any]] = None
    if df is not None:
        try:
            from routers.sla_matrix import _compute_sla_matrix
            from services import config_store
            # Use detected mode as smart default when no explicit user override exists
            _detected_mode = (payload.get("kpis") or {}).get("sla_detected_mode", "")
            _user_mode     = config_store.get("sla_mode") or ""
            sla_mode       = (_user_mode or _detected_mode or "daily").lower()
            custom   = config_store.get("custom_sla_hrs", 6.0)
            # Cache hour_heatmap BEFORE computing the matrix so the adaptive
            # baseline layer can correlate breaches with peak-load hours.
            try:
                from services import session_cache
                session_cache.set("last_hour_heatmap", payload.get("hour_heatmap"))
            except Exception:
                pass
            sla_mx_dict = _compute_sla_matrix(df, sla_mode, custom).model_dump()

            # Store a slim copy of the full run dataframe so /api/sla-matrix/json
            # can always re-compute with the latest XLSX/SOW config (not just the
            # truncated top_jobs sample).  Only the columns _compute_sla_matrix needs
            # are kept to minimise memory footprint.
            _SLIM_COLS = ["Job_Name", "Sub_Application", "Status",
                          "Start_Time", "End_Time", "Run_Sec", "run_time_hrs"]
            try:
                from services import session_cache as _sc2
                _slim = df[[c for c in _SLIM_COLS if c in df.columns]].copy()
                _sc2.set("job_runs_df", _slim.to_dict(orient="records"))
            except Exception:
                pass
        except Exception:
            sla_mx_dict = None

    resp = BatchResponse(
        filename=filename,
        kpis=BatchKPIs(**payload.get("kpis", {})),
        top_jobs=payload.get("top_jobs", []),
        top_breaches=payload.get("top_breaches", []),
        window=payload.get("window", []),
        sub_stats=payload.get("sub_stats", []),
        anomalies=payload.get("anomalies", []),
        hourly_counts=payload.get("hourly_counts"),
        sla_heatmap=payload.get("sla_heatmap"),
        hour_heatmap=payload.get("hour_heatmap"),
        elapsed_window=payload.get("elapsed_window"),
        summed_runtime=payload.get("summed_runtime"),
        worst_job=payload.get("worst_job"),
        sla_source=payload.get("sla_source"),
        data_coverage=payload.get("data_coverage"),
        sla_matrix=sla_mx_dict,
        customer_name=customer_name,
        daily_jobs=payload.get("daily_jobs"),
        window_sub_app=payload.get("window_sub_app", []),
    )

    # Cache the full batch response so the agent tools can query it
    # (top_jobs, top_breaches, sla_matrix breakdown, etc.).
    try:
        from services import session_cache
        resp_dict = resp.model_dump()
        session_cache.set("last_batch", resp_dict)
        if sla_mx_dict:
            session_cache.set("last_sla_matrix", sla_mx_dict)

        # ── Audit context: E2 job_runs + E3 workflow rollup + batch KPIs ─
        kpis_d  = resp_dict.get("kpis") or {}
        session_cache.ac_set("batch_kpis",      kpis_d)
        # Persist the auto-detected schedule mode so sla_matrix can default to it
        if kpis_d.get("sla_detected_mode"):
            session_cache.ac_set("sla_detected_mode", kpis_d["sla_detected_mode"])
        session_cache.ac_set("batch_top_jobs",  resp_dict.get("top_jobs") or [])
        session_cache.ac_set("job_summary",     resp_dict.get("top_jobs") or [])   # canonical merged slot
        session_cache.ac_set("daily_window_series", resp_dict.get("window") or [])
        session_cache.ac_set("regression_df",   resp_dict.get("anomalies") or [])
        if customer_name:
            session_cache.ac_set("customer_name", customer_name)
        # sla_matrix slots written by _compute_sla_matrix call (see below)
        if sla_mx_dict:
            session_cache.ac_set("sla_resolved",     sla_mx_dict.get("breaches") or [])
            session_cache.ac_set("sla_job_summary",  sla_mx_dict.get("job_summary") or [])
            session_cache.ac_set("job_summary",      sla_mx_dict.get("job_summary") or resp_dict.get("top_jobs") or [])
            session_cache.ac_set("adaptive_sla",     sla_mx_dict.get("job_baselines") or [])

        # ── Smart findings stub: seed a baseline so pe_narrative can
        #    generate prose even before /api/generate-findings is called. ──
        if not session_cache.get("last_smart_findings"):
            top_breaches = resp_dict.get("top_breaches") or []
            anomalies = resp_dict.get("anomalies") or []
            stub_findings = []
            for tb in top_breaches[:5]:
                if tb.get("buffer_status") == "BREACH":
                    stub_findings.append({
                        "level": "critical",
                        "text": f"{tb.get('Job_Name', '?')} breaches SLA "
                                f"(peak {tb.get('peak_hrs', 0):.2f}h)",
                        "source": "batch_upload",
                        "root_cause": "RUNTIME_BREACH",
                    })
            for an in anomalies[:3]:
                stub_findings.append({
                    "level": "warning",
                    "text": str(an.get("finding") or an.get("text", "Anomaly detected")),
                    "source": "batch_upload",
                    "root_cause": "ANOMALY",
                })
            if stub_findings:
                session_cache.set("last_smart_findings", {
                    "kpis": kpis_d,
                    "findings": stub_findings,
                    "summary": {
                        "critical": len([f for f in stub_findings if f["level"] == "critical"]),
                        "warning":  len([f for f in stub_findings if f["level"] == "warning"]),
                        "total":    len(stub_findings),
                    },
                })
    except Exception:
        pass

    return resp


# ── Endpoints ───────────────────────────────────────────────────
@router.post(
    "/process-batch",
    response_model=BatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Upload a Ctrl-M CSV/XLSX file and return batch KPIs + chart data",
)
async def process_batch(file: UploadFile = File(...)) -> BatchResponse:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    try:
        from services import session_cache as _sc_reset
        _sc_reset.ac_clear()
        _sc_reset.set("last_batch", {})
        _sc_reset.set("last_sla_matrix", {})
    except Exception:
        pass

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
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
        )

    try:
        df = load_ctrlm_bytes(raw, filename=file.filename)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse Ctrl-M file: {exc}",
        ) from exc

    try:
        payload = build_batch_payload(df)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Batch metrics computation failed: {exc}",
        ) from exc

    customer = _extract_customer_from_ctrlm_filename(file.filename)
    if customer:
        try:
            from services import config_store, session_cache as _sc
            # Fix 4b — SOW cache isolation: if customer changed, clear last_sow_compare
            # so SOW override data from the previous customer never bleeds into this one.
            _prev_customer = _sc.ac_get("customer_name") or ""
            if _prev_customer and _prev_customer.lower() != customer.lower():
                _sc.set("last_sow_compare", None)
            config_store.set("customer_name", customer)
        except Exception:
            pass
    return _payload_to_response(file.filename, payload, df=df, customer_name=customer)


@router.post(
    "/process-batch/multi",
    response_model=BatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Upload up to 8 Ctrl-M CSV/XLSX files and return merged batch KPIs + chart data",
)
async def process_batch_multi(
    files: List[UploadFile] = File(...),
) -> BatchResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum is {MAX_BATCH_FILES}, got {len(files)}.",
        )

    try:
        from services import session_cache as _sc_reset
        _sc_reset.ac_clear()
        _sc_reset.set("last_batch", {})
        _sc_reset.set("last_sla_matrix", {})
    except Exception:
        pass

    frames: list[pd.DataFrame] = []
    filenames: list[str] = []

    for f in files:
        if not f.filename:
            continue
        ext = _ext(f.filename)
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type '{ext}' in '{f.filename}'. "
                       f"Allowed: {sorted(ALLOWED_EXTENSIONS)}",
            )

        raw = await f.read()
        if not raw:
            continue  # skip empty files silently
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File '{f.filename}' exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
            )

        try:
            df = load_ctrlm_bytes(raw, filename=f.filename)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Failed to parse '{f.filename}': {exc}",
            ) from exc

        if not df.empty:
            df["_source_file"] = f.filename
            frames.append(df)
            filenames.append(f.filename)

    if not frames:
        raise HTTPException(status_code=400, detail="All uploaded files were empty or unparseable.")

    merged = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]

    # ── P2 #11: dedup identical job-run rows across multi-file uploads ────────
    # Uploading the same export twice (or two files that overlap on a date range)
    # would otherwise double-count every KPI. Hash each row on its natural
    # identity — (Job_Name, Sub_Application, Start_Time, Run_Sec) — and drop exact
    # duplicates. Only applied to multi-file merges; single files are untouched.
    if len(frames) > 1:
        _dedup_keys = [c for c in ("Job_Name", "Sub_Application", "Start_Time", "Run_Sec")
                       if c in merged.columns]
        if _dedup_keys:
            _before = len(merged)
            merged = merged.drop_duplicates(subset=_dedup_keys, keep="first").reset_index(drop=True)
            _removed = _before - len(merged)
            if _removed > 0:
                import logging as _logd
                _logd.getLogger(__name__).info(
                    "process_batch_multi: dropped %d duplicate job-run row(s) across %d files "
                    "(dedup keys: %s)", _removed, len(frames), _dedup_keys,
                )

    try:
        payload = build_batch_payload(merged)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Batch metrics computation failed: {exc}",
        ) from exc

    combined_name = " + ".join(filenames)
    customer: Optional[str] = None
    for fn in filenames:
        c = _extract_customer_from_ctrlm_filename(fn)
        if c:
            customer = c
            break
    if customer:
        try:
            from services import config_store
            config_store.set("customer_name", customer)
        except Exception:
            pass
    return _payload_to_response(combined_name, payload, df=merged, customer_name=customer)


@router.post(
    "/process-batch/json",
    response_model=BatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Accept pre-parsed Ctrl-M rows as JSON and return batch KPIs + chart data",
)
async def process_batch_json(body: BatchJsonRequest) -> BatchResponse:
    if not body.rows:
        raise HTTPException(status_code=400, detail="`rows` is empty.")
    try:
        from services import session_cache as _sc_reset
        _sc_reset.ac_clear()
        _sc_reset.set("last_batch", {})
        _sc_reset.set("last_sla_matrix", {})
    except Exception:
        pass
    try:
        df = pd.DataFrame(body.rows)
        # Coerce the expected columns — the frontend may supply raw
        # Ctrl-M records that haven't been passed through load_ctrlm.
        if "run_time_hrs" not in df.columns and "Run_Sec" in df.columns:
            df["run_time_hrs"] = pd.to_numeric(df["Run_Sec"], errors="coerce").fillna(0) / 3600.0
        if "run_date" not in df.columns and "Start_Time" in df.columns:
            df["run_date"] = pd.to_datetime(df["Start_Time"], errors="coerce").dt.date
        if "month" not in df.columns and "Start_Time" in df.columns:
            df["month"] = pd.to_datetime(df["Start_Time"], errors="coerce").dt.to_period("M").astype(str)
        if "Sub_Application" not in df.columns:
            df["Sub_Application"] = "UNKNOWN"
        df.dropna(subset=["run_date"], inplace=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid row payload: {exc}") from exc

    payload = build_batch_payload(df)
    return _payload_to_response(body.filename or "payload.json", payload, df=df)


@router.post(
    "/batch/refresh",
    response_model=BatchResponse,
    status_code=status.HTTP_200_OK,
    summary="Re-run batch KPIs using cached run data + current SLA config",
)
def refresh_batch() -> BatchResponse:
    """Re-compute the batch payload from the job_runs_df stored in session cache.

    Called automatically by the frontend after the SLA matrix is uploaded or
    removed, so charts immediately reflect the new per-job SLA ceilings without
    requiring the user to re-upload the batch file.
    """
    from services import session_cache as _sc

    raw_rows = _sc.get("job_runs_df")
    if not raw_rows:
        raise HTTPException(
            status_code=404,
            detail="No cached batch data. Please re-upload the Ctrl-M file.",
        )

    try:
        df = pd.DataFrame(raw_rows)
        # Restore datetime columns
        for col in ("Start_Time", "End_Time"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        if "Run_Sec" in df.columns:
            df["Run_Sec"] = pd.to_numeric(df["Run_Sec"], errors="coerce").fillna(0)
        if "run_time_hrs" not in df.columns and "Run_Sec" in df.columns:
            df["run_time_hrs"] = df["Run_Sec"] / 3600.0
        if "run_date" not in df.columns and "Start_Time" in df.columns:
            df["run_date"] = pd.to_datetime(df["Start_Time"], errors="coerce").dt.date
        # Derived columns required by compute_metrics — rebuild from Start_Time
        if "month" not in df.columns and "Start_Time" in df.columns:
            df["month"] = pd.to_datetime(df["Start_Time"], errors="coerce").dt.to_period("M").astype(str)
        if "hour" not in df.columns and "Start_Time" in df.columns:
            df["hour"] = pd.to_datetime(df["Start_Time"], errors="coerce").dt.hour
        if "Sub_Application" not in df.columns:
            df["Sub_Application"] = "UNKNOWN"
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error restoring cached data: {exc}") from exc

    payload = build_batch_payload(df)
    filename = (_sc.get("last_batch") or {}).get("filename") or "cached_batch.csv"
    customer = _sc.ac_get("customer_name")
    return _payload_to_response(filename, payload, df=df, customer_name=customer)
