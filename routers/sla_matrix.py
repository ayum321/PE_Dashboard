"""
Batch SLA Matrix router.

POST /api/sla-matrix
    Accepts raw Ctrl-M CSV/XLSX bytes (multipart) + optional SLA config.
    Returns a structured SLA compliance analysis with breach details,
    configurable daily/weekly/monthly/custom thresholds.

POST /api/sla-matrix/json
    Same but accepts pre-parsed rows array in JSON body.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict

import re

from services import config_store
from services import pe_config
from services.batch_calculator import load_ctrlm_bytes

router = APIRouter()

MODES = {
    "daily":    ("Daily SLA",     None),    # None = read live from pe_config
    "weekly":   ("Weekly SLA",    None),
    "biweekly": ("Bi-Weekly SLA", None),
    "monthly":  ("Monthly SLA",   None),
}


def _mode_hrs(mode: str) -> float:
    """Return the SLA ceiling for a given mode from pe_config (live)."""
    if mode == "daily":
        return pe_config.SLA_DAILY_HRS
    if mode == "weekly":
        return pe_config.SLA_WEEKLY_HRS
    if mode == "biweekly":
        return pe_config.SLA_BIWEEKLY_HRS
    return pe_config.SLA_MONTHLY_HRS


# ── Models ───────────────────────────────────────────────────────────────────

class SlaBreach(BaseModel):
    job_name:        str
    sub_application: str
    run_date:        str
    start_time:      str
    end_time:        str
    run_hrs:         float
    sla_limit_hrs:   float
    breach_margin_hrs: float   # how far over SLA (positive = over)
    status:          str       # BREACH | AT_RISK | OK


class SlaMatrixResponse(BaseModel):
    sla_mode:          str
    sla_limit_hrs:     float
    sla_label:         str
    total_runs:        int
    total_jobs:        int
    breaching_runs:    int
    at_risk_runs:      int
    long_job_runs:     int      # NEW: 15% < buffer ≤ 40%
    failed_runs:       int      # NEW: execution failures (not SLA-related)
    ok_runs:           int
    compliance_pct:    float
    breach_rate_pct:   float
    worst_job:         Optional[str]
    worst_hrs:         float
    worst_margin_hrs:  float
    breaches:          List[SlaBreach]
    job_summary:       List[Dict[str, Any]]
    job_baselines:     Optional[Dict[str, Dict[str, Any]]] = None
    outliers:          Optional[List[Dict[str, Any]]] = None
    resource_linked:   Optional[List[Dict[str, Any]]] = None
    ai_narrative:      Optional[str] = None
    ai_model:          Optional[str] = None
    window_compliance_pct: Optional[float] = None
    window_total_days:     Optional[int]   = None
    window_breach_days:    Optional[int]   = None
    window_detail:         Optional[List[Dict[str, Any]]] = None
    window_warnings:       Optional[List[str]] = None
    gate_audit:        Optional[Dict[str, Any]] = None
    explicit_sla_matrix: bool = False
    # "per_job" when job names present, "aggregated" when only sub-app-level data
    data_format:       str = "per_job"
    # Canonical per-workflow resolved dataframe — single source of truth for all screens.
    # Columns: workflow_key, workflow_name, sub_application, batch_type,
    #          workflow_start, workflow_end, runtime_h, sla_h, sla_source,
    #          buffer_pct, status, job_count, + debug_* columns.
    workflow_summary:  Optional[List[Dict[str, Any]]] = None


class JsonSlaRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    rows:     List[Dict[str, Any]]
    sla_mode: Optional[str]  = "daily"
    sla_hrs:  Optional[float] = None


# ── Logic ────────────────────────────────────────────────────────────────────

def _compute_sla_matrix(df, sla_mode: str, custom_sla_hrs: float | None) -> SlaMatrixResponse:
    import pandas as pd

    # ── Resolve SLA ceilings ─────────────────────────────────────────────
    # Priority:
    #   1. Per-job SLA from uploaded customer SLA matrix (contracts)
    #   2. Global mode ceiling (daily/weekly/monthly/custom) from pe_config
    # When per-job contracts exist, each run is judged against its own SLA.
    # This prevents the dangerous false-green where a 6h daily ceiling hides
    # a workflow with a 1.5h contractual SLA window.

    if sla_mode == "custom" and custom_sla_hrs:
        global_sla_hrs = float(custom_sla_hrs)
        sla_label = f"Custom SLA ({global_sla_hrs}h)"
    else:
        mode_label, _ = MODES.get(sla_mode, MODES["daily"])
        global_sla_hrs = _mode_hrs(sla_mode)
        sla_label = f"{mode_label} ({global_sla_hrs}h)"

    # Load per-job SLA contracts from the uploaded SLA matrix (if available)
    contracts = []
    ceilings: dict = {}
    has_per_job_sla = False
    # explicit_sla_matrix is True ONLY when a customer SLA matrix file was uploaded
    # with per-job contract rows. Schedule-type ceilings from settings/SOW alone do NOT count.
    explicit_sla_matrix = False
    try:
        from services import config_store as _cs
        sla_intel = _cs.get("_sla_intelligence") or {}
        if sla_intel.get("contracts"):
            from services.sla_engine import SlaContract, resolve_sla, classify_schedule
            import dataclasses as _dc
            _valid_fields = {f.name for f in _dc.fields(SlaContract)}
            contracts = []
            for c in sla_intel["contracts"]:
                if isinstance(c, dict):
                    if c.get("completeness") != "complete":
                        continue
                    filtered = {k: v for k, v in c.items() if k in _valid_fields}
                    try:
                        contracts.append(SlaContract(**filtered))
                    except Exception:
                        continue
                elif hasattr(c, "completeness") and c.completeness == "complete":
                    contracts.append(c)
            ceilings = sla_intel.get("ceilings") or {}
            # Only mark explicit_sla_matrix when actual per-job contracts were loaded
            explicit_sla_matrix = bool(contracts)
            has_per_job_sla = bool(contracts) or bool(ceilings)
    except Exception:
        pass

    # Load the 3-tier SLA merger sources (BatchSLA_info.xlsx + SOW windows)
    _batch_sla_rows: list[dict] = []
    _sow_windows: dict = {}
    try:
        from services import config_store as _cs2
        _bsla = _cs2.get("_batch_sla_xlsx") or {}
        _batch_sla_rows = _bsla.get("workflows") or []
        _sow_windows = _cs2.get("_sow_sla_windows") or {}
    except Exception:
        pass

    # ── Build a fast bulk-normalized lookup from the BatchSLA XLSX ──────
    # This is the O(1) path: strip env prefix from XLSX workflow names once,
    # then look up each Ctrl-M job's normalized Sub_Application / Job_Name.
    #
    # Algorithm (generic, customer-agnostic):
    #   norm(name) = strip(PROD_|TEST_|UAT_|DEV_|STG_) → UPPER
    #
    # Matching order (highest priority first):
    #   A. normalized(sub_app)  exact match → XLSX normalized workflow
    #   B. normalized(job_name) exact match → XLSX normalized workflow
    #   C. token overlap: any XLSX token appears in normalized(sub_app) tokens
    #      or vice-versa — handles partial name differences (WF1 vs WEEKLY_WF1)
    def _norm(s: str) -> str:
        """Strip environment prefix and uppercase — customer-agnostic."""
        from services.sla_merger import _strip_env_prefix
        return _strip_env_prefix(str(s or "").strip()).upper()

    # Build three lookup structures for the bulk join:
    #   _bsla_exact:   {norm_workflow_key → (sla_hours, workflow_name)}
    #   _bsla_by_job:  {norm_first/last_job → (sla_hours, source)}  ← anchors
    #   _bsla_tokens:  [(frozenset_of_tokens, sla_hours, workflow_name)]
    _bsla_exact:  dict[str, tuple[float, str]] = {}
    _bsla_by_job: dict[str, tuple[float, str]] = {}   # first_job / last_job anchors
    _bsla_tokens: list[tuple[frozenset, float, str]] = []
    # Track which secondary keys are produced by multiple workflows (collision detection).
    # When two XLSX workflows share a secondary stripped key (e.g. "DAILY_BATCH" from
    # both "PETBARN_DAILY_BATCH" and "TESCO_DAILY_BATCH"), indexing the secondary form
    # gives last-writer-wins and silently assigns the wrong SLA. Skip colliding secondaries.
    _secondary_key_count: dict[str, int] = {}
    for row in _batch_sla_rows:
        wf_raw = row.get("workflow") or ""
        sla_h  = row.get("sla_hours")
        if not sla_h or sla_h <= 0:
            continue
        try:
            from services.sla_merger import _all_normalized_forms as _anf
            _wf_forms = _anf(wf_raw)
        except Exception:
            _wf_forms = [_norm(wf_raw)]
        # Count occurrences of each secondary (non-primary) form across all XLSX rows
        if len(_wf_forms) > 1:
            for _sf in _wf_forms[1:]:
                _secondary_key_count[_sf] = _secondary_key_count.get(_sf, 0) + 1

    for row in _batch_sla_rows:
        wf_raw = row.get("workflow") or ""
        sla_h  = row.get("sla_hours")
        if not sla_h or sla_h <= 0:
            continue
        sla_f   = float(sla_h)
        # Index under ALL normalized forms: primary + customer-prefix-stripped secondary.
        # PETBARN_DAILY in XLSX indexes as both "PETBARN_DAILY" and "DAILY" so it matches
        # Ctrl-M Sub_Application regardless of whether the customer prefix is present.
        # Exception: secondary keys shared by multiple workflows are SKIPPED to avoid
        # non-deterministic last-writer-wins assignment of the wrong SLA.
        try:
            from services.sla_merger import _all_normalized_forms as _anf
            _wf_forms = _anf(wf_raw)
        except Exception:
            _wf_forms = [_norm(wf_raw)]
        for _i, wf_norm in enumerate(_wf_forms):
            if not wf_norm:
                continue
            is_secondary = _i > 0
            if is_secondary and _secondary_key_count.get(wf_norm, 0) > 1:
                # Collision: skip this secondary key — multiple workflows map to same key
                import logging as _log_col
                _log_col.getLogger("pe_dashboard.sla_matrix").debug(
                    "Skipping colliding secondary BSLA key '%s' (appears in %d workflows)",
                    wf_norm, _secondary_key_count[wf_norm],
                )
                continue
            _bsla_exact[wf_norm] = (sla_f, wf_raw)
            tokens = frozenset(t for t in re.split(r"[_\s]+", wf_norm) if len(t) >= 2)
            if tokens:
                _bsla_tokens.append((tokens, sla_f, wf_raw))
        # Index by first_job + last_job so we can match even when Sub_Application
        # is missing from the Ctrl-M export (defaults to "UNKNOWN").
        for fld in ("first_job", "last_job"):
            anchor = _norm(row.get(fld) or "")
            if anchor and anchor not in ("UNKNOWN", ""):
                _bsla_by_job[anchor] = (sla_f, "batch_sla_xlsx")

    # ── Also index first_job / last_job from _sla_intelligence contracts ──────
    # Dawn Foods-style SLA matrices (window model, no WESCO-style workflow rows)
    # store anchor jobs in the ingest result contracts but not in _batch_sla_rows.
    # Index those contracts into the same lookup structures so Pass C matching works.
    for contract in contracts:
        _c_sla = getattr(contract, "sla_window_hrs", None) or getattr(contract, "sla_duration_hrs", None)
        if not _c_sla or _c_sla <= 0:
            continue
        _c_sla_f = float(_c_sla)
        _c_name  = _norm(getattr(contract, "batch_name", "") or "")
        if _c_name:
            _bsla_exact.setdefault(_c_name, (_c_sla_f, getattr(contract, "batch_name", _c_name)))
            _tok = frozenset(t for t in re.split(r"[_\s]+", _c_name) if len(t) >= 2)
            if _tok:
                _bsla_tokens.append((_tok, _c_sla_f, getattr(contract, "batch_name", _c_name)))
        for _anchor_attr in ("first_job", "last_job"):
            _anchor_val = _norm(getattr(contract, _anchor_attr, "") or "")
            if _anchor_val and _anchor_val not in ("UNKNOWN", ""):
                _bsla_by_job.setdefault(_anchor_val, (_c_sla_f, "sla_intelligence_anchor"))

    # Full-row lookup: normalized workflow key → complete BSLA row dict.
    # Used in workflow_summary to retrieve first_job / last_job anchors and
    # sla_end_time for precision runtime measurement (reference script Tier 2 logic).
    _bsla_full: dict[str, dict] = {}
    for _brow in _batch_sla_rows:
        _bwf = _brow.get("workflow") or ""
        try:
            from services.sla_merger import _all_normalized_forms as _anf3
            for _bwf_n in _anf3(_bwf):
                if _bwf_n and _bwf_n not in _bsla_full:
                    _bsla_full[_bwf_n] = _brow
        except Exception:
            _bk = _norm(_bwf)
            if _bk:
                _bsla_full[_bk] = _brow

    def _bulk_lookup_bsla(job: str, sub_app: str) -> tuple[float, str, str] | None:
        """Fast normalized lookup in BatchSLA XLSX.

        Returns (sla_hours, source_label, matched_workflow_name) or None.
        Both per-job and workflow-summary paths call this function so the
        matching logic is always identical.

        Resolution order (highest priority first):
          A. Exact normalized match on sub_app / job name vs XLSX workflow names
          B. Substring containment (norm_sub_app ⊂ wf_norm or vice-versa)
          C. Direct first_job/last_job anchor match (handles missing Sub_Application)
          D. Token overlap ≥ 2 tokens (generic partial-name match)
        """
        j_norm = _norm(job)
        # Guard: if sub_app is a placeholder, treat as empty
        s_raw  = str(sub_app or "").strip()
        s_norm = _norm(s_raw) if s_raw.upper() not in ("UNKNOWN", "—", "") else ""

        # Pass A — exact match on normalized sub_app / job_name vs XLSX workflow name
        if s_norm and s_norm in _bsla_exact:
            sla_h, wf_raw = _bsla_exact[s_norm]
            return sla_h, "batch_sla_xlsx_exact", wf_raw
        if j_norm and j_norm in _bsla_exact:
            sla_h, wf_raw = _bsla_exact[j_norm]
            return sla_h, "batch_sla_xlsx_exact", wf_raw

        # Pass B — substring containment
        for wf_norm, (sla_h, wf_raw) in _bsla_exact.items():
            if wf_norm and s_norm and (wf_norm in s_norm or s_norm in wf_norm):
                return sla_h, "batch_sla_xlsx_substr", wf_raw
            if wf_norm and j_norm and (wf_norm in j_norm or j_norm in wf_norm):
                return sla_h, "batch_sla_xlsx_substr", wf_raw

        # Pass C — first_job / last_job anchor (critical when Sub_Application missing)
        # XLSX row stores the exact first and last job names in each workflow.
        # If the Ctrl-M export lacks a Sub_Application column (→ "UNKNOWN"),
        # we can still match each job against these anchors.
        for key in ([j_norm] if j_norm else []) + ([s_norm] if s_norm else []):
            if key and key in _bsla_by_job:
                sla_h, _ = _bsla_by_job[key]
                return sla_h, "batch_sla_xlsx_anchor", "(anchor: first/last job)"

        # Pass D — token overlap ≥ 2 shared non-trivial tokens
        s_tok = frozenset(t for t in re.split(r"[_\s]+", s_norm) if len(t) >= 2) if s_norm else frozenset()
        j_tok = frozenset(t for t in re.split(r"[_\s]+", j_norm) if len(t) >= 2)
        best: tuple[float, str, str] | None = None
        best_score = 0
        for (wf_tok, sla_h, wf_raw) in _bsla_tokens:
            s_overlap = len(wf_tok & s_tok)
            j_overlap = len(wf_tok & j_tok)
            score = max(s_overlap, j_overlap)
            if score > best_score:
                best_score = score
                best = (sla_h, "batch_sla_xlsx_tokens", wf_raw)
        if best_score >= 2:   # require ≥ 2 tokens to avoid noise
            return best
        return None

    def _resolve_job_sla(job_name: str, sub_app: str) -> tuple[float, str]:
        """Get the effective SLA ceiling + source label for a specific job.

        Priority (highest → lowest):
          sla_matrix     — exact per-job contract match in uploaded SLA file
          batch_sla_xlsx — Tier 1: bulk-normalized lookup in BatchSLA_info.xlsx
          sow_extracted  — Tier 2: SOW batch-type ceiling
          assumed        — no file match; global ceiling (user-selected mode)
          global         — no SLA intel loaded at all
        """
        # Tier 0: existing engine (per-job SLA matrix file) — only exact contract matches
        if has_per_job_sla:
            try:
                resolved = resolve_sla(job_name, sub_app, contracts, ceilings)
                if resolved.sla_hrs and resolved.sla_hrs > 0 and resolved.source == "sla_matrix":
                    return resolved.sla_hrs, "sla_matrix"
            except Exception:
                pass

        # Tier 1: fast bulk-normalized lookup in BatchSLA XLSX
        if _bsla_exact or _bsla_tokens:
            hit = _bulk_lookup_bsla(job_name, sub_app)
            if hit:
                return hit[0], hit[1]   # (sla_h, source_label) — drop matched_wf_name

        # Tier 2 / 3: SOW + global via the full resolve_sla_tier fallback
        if _sow_windows:
            try:
                from services.sla_merger import resolve_sla_tier
                merged = resolve_sla_tier(job_name, sub_app, [], _sow_windows)
                if merged["source"] == "EXCLUDED":
                    return 0.0, "excluded"
                if merged["source"] == "SOW_EXTRACTED":
                    return float(merged["limit_hours"]), "sow_extracted"
            except Exception:
                pass

        if not has_per_job_sla and not _batch_sla_rows and not _sow_windows:
            return global_sla_hrs, "global"

        # No contract match — use batch-type-aware global default (NOT the UI mode ceiling).
        # Prevents a "Daily (5h)" UI selection from bleeing into WEEKLY jobs that
        # have no XLSX match — WEEKLY should fall to 8h, not 5h.
        try:
            from services.sla_merger import detect_batch_type
            from services import pe_config as _pc
            _GLOBAL_TYPE_DEFAULTS: dict[str, float] = {
                "DAILY": _pc.SLA_DAILY_HRS, "WEEKLY": _pc.SLA_WEEKLY_HRS,
                "BIWEEKLY": _pc.SLA_BIWEEKLY_HRS, "MONTHLY": _pc.SLA_MONTHLY_HRS,
            }
            detected = detect_batch_type(sub_app, "") or detect_batch_type(job_name, "")
            if detected and detected in _GLOBAL_TYPE_DEFAULTS:
                return _GLOBAL_TYPE_DEFAULTS[detected], "global"
        except Exception:
            pass
        return global_sla_hrs, "assumed"

    # ── AT_RISK / LONG_JOB thresholds ───────────────────────────────────
    # Generic classification tiers (apply to ALL customers, ANY SLA):
    #   buffer_pct > LONGJOB_PCT  → OK
    #   AT_RISK_PCT < buffer ≤ LONGJOB_PCT → LONG_JOB
    #   0% < buffer ≤ AT_RISK_PCT  → AT_RISK
    #   buffer ≤ 0%               → BREACH
    #   CompletionStatus = NOT OK/ABEND/ERROR → FAILED (not subject to SLA)
    # Thresholds read from pe_config — single canonical source, customer-configurable
    # via config_store keys: sla_atrisk_pct, sla_longjob_pct
    AT_RISK_PCT   = pe_config.SLA_ATRISK_PCT  / 100   # fraction, e.g. 0.15
    LONG_JOB_PCT  = pe_config.SLA_LONGJOB_PCT / 100   # fraction, e.g. 0.40

    # Ensure run_time_hrs column
    if "run_time_hrs" not in df.columns:
        df["run_time_hrs"] = df.get("Run_Sec", 0) / 3600.0

    import math as _math

    results: list[dict] = []

    for _, row in df.iterrows():
        _raw_hrs = row.get("run_time_hrs", 0)
        try:
            hrs = float(_raw_hrs)
        except (TypeError, ValueError):
            hrs = 0.0
        # Guard: NaN / Inf from pandas propagation must be treated as 0
        # (NaN is truthy in Python, so `float(NaN) or 0` silently returns NaN)
        if _math.isnan(hrs) or _math.isinf(hrs):
            hrs = 0.0
        # Fallback: compute from Start/End timestamps when Run_Sec is missing / 0
        # This happens when: Ctrl-M export has no "Run Time (Sec.)" column, or
        # when batch_calculator's mask narrowly filtered by status=="OK".
        if hrs == 0:
            try:
                _st = pd.Timestamp(row.get("Start_Time", pd.NaT))
                _et = pd.Timestamp(row.get("End_Time",   pd.NaT))
                if pd.notna(_st) and pd.notna(_et) and _et > _st:
                    hrs = round((_et - _st).total_seconds() / 3600, 6)
            except Exception:
                pass
        job     = str(row.get("Job_Name", "?"))
        sub_app = str(row.get("Sub_Application", "—"))
        raw_status = str(row.get("Status", "")).strip().upper()

        # Per-job SLA resolution — returns (hours, source_label)
        sla_hrs, sla_source = _resolve_job_sla(job, sub_app)

        # Skip excluded batch types (CYCLIC, OUTBOUND, …)
        if sla_source == "excluded":
            continue

        run_date  = ""
        start_str = ""
        end_str   = ""
        try:
            st = pd.Timestamp(row.get("Start_Time", pd.NaT))
            run_date  = st.strftime("%Y-%m-%d") if pd.notna(st) else ""
            start_str = st.strftime("%H:%M") if pd.notna(st) else ""
        except Exception:
            pass
        try:
            et = pd.Timestamp(row.get("End_Time", pd.NaT))
            end_str = et.strftime("%H:%M") if pd.notna(et) else ""
        except Exception:
            pass

        margin_raw = hrs - sla_hrs
        margin = round(margin_raw, 4) if not (_math.isnan(margin_raw) or _math.isinf(margin_raw)) else 0.0

        # ── Generic mathematical buffer formula ─────────────────────────
        # buffer_pct = (SLA_h − runtime_h) / SLA_h × 100
        # Guard: if SLA_h = 0 or SLA_h = None → reason_code = SLA_MISSING
        # Guard: if runtime_h = 0 AND status=FAILED → reason_code = FAILED
        from services.pe_utils import SUCCESS_STATUSES
        is_failure = bool(raw_status) and raw_status not in SUCCESS_STATUSES

        if is_failure and hrs == 0:
            reason_code = "FAILED"
            buffer_pct_val = None
            st_label = "FAILED"
        elif not is_failure and hrs == 0:
            # RUNTIME_ZERO: job completed OK but has no measurable runtime.
            # Treat as a data-quality gap — buffer cannot be computed.
            # Do NOT show 100% buffer (misleading) — show None with reason_code.
            reason_code = "RUNTIME_ZERO"
            buffer_pct_val = None
            st_label = "OK"   # job didn't fail; we just have no timing data
        elif sla_hrs <= 0:
            reason_code = "SLA_MISSING"
            buffer_pct_val = None
            st_label = "FAILED" if is_failure else "OK"  # cannot classify without SLA
        else:
            reason_code = None
            buffer_pct_val = round((sla_hrs - hrs) / sla_hrs * 100, 2)
            # 4-tier generic status classification
            if is_failure:
                st_label = "FAILED"
            elif buffer_pct_val <= 0:
                st_label = "BREACH"
            elif buffer_pct_val <= AT_RISK_PCT * 100:
                st_label = "AT_RISK"
            elif buffer_pct_val <= LONG_JOB_PCT * 100:
                st_label = "LONG_JOB"
            else:
                st_label = "OK"

        results.append({
            "job_name":          job,
            "sub_application":   sub_app,
            "run_date":          run_date,
            "start_time":        start_str,
            "end_time":          end_str,
            "run_hrs":           round(hrs, 4),
            "sla_limit_hrs":     round(sla_hrs, 4),
            "breach_margin_hrs": margin,
            "buffer_pct":        buffer_pct_val,     # None when SLA_MISSING or FAILED
            "reason_code":       reason_code,        # None | SLA_MISSING | FAILED | RUNTIME_ZERO
            "status":            st_label,
            "sla_source":        sla_source,         # sla_matrix | batch_sla_xlsx | sow_extracted | assumed | global
        })

    rdf = pd.DataFrame(results) if results else pd.DataFrame()

    # FAILED runs are isolated — not counted toward SLA compliance/breach
    total_runs     = len(rdf)
    failed_count   = int((rdf["status"] == "FAILED").sum())   if not rdf.empty else 0
    eligible_runs  = total_runs - failed_count
    breach_count   = int((rdf["status"] == "BREACH").sum())   if not rdf.empty else 0
    atrisk_count   = int((rdf["status"] == "AT_RISK").sum())  if not rdf.empty else 0
    longjob_count  = int((rdf["status"] == "LONG_JOB").sum()) if not rdf.empty else 0
    ok_count       = int((rdf["status"] == "OK").sum())       if not rdf.empty else 0
    # Compliance: runs that completed within SLA = OK + LONG_JOB.
    # AT_RISK means buffer ≤ 15% — the run is dangerously close to breach.
    # Counting AT_RISK as compliant masks critical capacity risk.
    # BREACH + AT_RISK are both SLA violations for compliance purposes.
    if eligible_runs > 0:
        compliance = round((ok_count + longjob_count) / eligible_runs * 100, 2)
    elif total_runs == 0:
        compliance = 100.0
    else:
        compliance = 0.0
    breach_rate   = round(breach_count / eligible_runs * 100, 2) if eligible_runs else 0.0

    # Filter breaches + at-risk + long_job for detail table
    # JSON round-trip converts numpy types (int64, float64, NaN → null)
    import json as _json
    detail: list[dict] = []
    if not rdf.empty:
        _detail_df = (rdf[rdf["status"].isin(["BREACH", "AT_RISK", "LONG_JOB"])]
                      .sort_values("run_hrs", ascending=False)
                      .head(200))
        detail = _json.loads(_detail_df.to_json(orient="records", default_handler=str))

    # Per-job rollup — includes buffer_pct from per-row values
    if not rdf.empty:
        sla_rdf = rdf[rdf["status"] != "FAILED"]
        if sla_rdf.empty:
            sla_rdf = rdf
        job_grp = sla_rdf.groupby("job_name").agg(
            runs            = ("run_hrs", "count"),
            peak_hrs        = ("run_hrs", "max"),
            avg_hrs         = ("run_hrs", "mean"),
            sla_limit       = ("sla_limit_hrs", "min"),   # min = tightest SLA (most conservative)
            sla_source      = ("sla_source", "first"),
            sub_application = ("sub_application", "first"),  # pass-through for JS rollup
            breach_runs     = ("status", lambda x: (x == "BREACH").sum()),
            atrisk_runs     = ("status", lambda x: (x == "AT_RISK").sum()),
            longjob_runs    = ("status", lambda x: (x == "LONG_JOB").sum()),
        ).reset_index()
        # ── Generic buffer formula: (SLA_h − peak_h) / SLA_h × 100 ──
        # Null-safe: SLA_MISSING reason when sla_limit = 0
        def _buf(r):
            try:
                sl = float(r.sla_limit or 0)
                pk = float(r.peak_hrs  or 0)
            except (TypeError, ValueError):
                return None
            if sl <= 0 or _math.isnan(sl) or _math.isnan(pk):
                return None   # SLA_MISSING — never show a misleading 0%
            val = round((sl - pk) / sl * 100, 2)
            return None if (_math.isnan(val) or _math.isinf(val)) else val
        job_grp["buffer_pct"]  = job_grp.apply(_buf, axis=1)
        job_grp["reason_code"] = job_grp.apply(
            lambda r: "SLA_MISSING" if r.sla_limit <= 0 else None, axis=1
        )
        def _brate(r):
            try:
                runs = float(r.runs or 0)
                return round(float(r.breach_runs) / runs * 100, 1) if runs > 0 else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                return 0.0
        job_grp["breach_rate"] = job_grp.apply(_brate, axis=1)
        fail_grp = rdf[rdf["status"] == "FAILED"].groupby("job_name").size().reset_index(name="fail_count")
        job_grp = job_grp.merge(fail_grp, on="job_name", how="left")
        job_grp["fail_count"] = job_grp["fail_count"].fillna(0).astype(int)
        job_grp = job_grp.sort_values("peak_hrs", ascending=False)
        # JSON round-trip: converts numpy types (int64, float64) and NaN → null
        import json as _json
        job_summary = _json.loads(job_grp.to_json(orient="records", default_handler=str))
    else:
        job_summary = []

    # ── Canonical workflow-level resolved dataframe ──────────────────────
    # Groups Ctrl-M runs by Sub_Application (= workflow), computes elapsed
    # wall-clock time (max(End_Time) − min(Start_Time)), then resolves the
    # workflow SLA with full Tier 1→2→3 priority.
    #
    # This is the CORRECT metric for workflow SLA: a workflow has an SLA
    # window (e.g. 1.5h) from when the first job starts to when the last
    # job ends — NOT the runtime of any individual job within it.
    #
    # Stored in session_cache["workflow_sla_summary"] so Batch Review,
    # SLA Matrix, PE Findings, and the narrative all read the same numbers.
    workflow_summary: list[dict] = []
    try:
        tdf = df.copy()
        tdf["_start"] = pd.to_datetime(tdf.get("Start_Time"), errors="coerce")
        tdf["_end"]   = pd.to_datetime(tdf.get("End_Time"),   errors="coerce")
        tdf["_sub"]   = tdf.get("Sub_Application", pd.Series(["UNKNOWN"] * len(tdf), index=tdf.index)).astype(str)
        # run_date: group jobs that belong to the same workflow execution
        tdf["_run_date"] = tdf["_start"].dt.strftime("%Y-%m-%d").fillna("unknown")

        for sub_app_raw, grp in tdf.groupby("_sub"):
            sub_app = str(sub_app_raw or "").strip()
            sub_app_is_unknown = not sub_app or sub_app.upper() in ("UNKNOWN", "NAN", "NONE", "—", "")

            # Fix #12: instead of skipping UNKNOWN sub_app entirely, try anchor-job
            # fallback. When the Ctrl-M export has no Sub_Application column, all rows
            # have sub_app="UNKNOWN". The _bsla_by_job dict (indexed by first_job/last_job)
            # was designed exactly for this case.
            if sub_app_is_unknown:
                # Try to synthesize a workflow name from anchor-job matching.
                # Look for any job in this group that matches a known first_job anchor.
                _synth_wf: str | None = None
                _synth_sla: tuple | None = None
                if "Job_Name" in grp.columns:
                    for _jn in grp["Job_Name"].dropna().str.upper().unique():
                        _jn_norm = _norm(_jn)
                        if _jn_norm in _bsla_by_job:
                            _synth_sla = _bsla_by_job[_jn_norm]
                            _synth_wf = _jn_norm
                            break
                if _synth_wf is None:
                    continue  # no anchor found → truly unknown, skip
                # Proceed with synthetic workflow name from the anchor job
                sub_app = _synth_wf
                # Don't override sub_app_is_unknown — continue processing below

            norm_sub = _norm(sub_app)

            # ── Pre-resolve BSLA hit BEFORE per-run loop (reference script approach) ──
            # We need first_job / last_job anchors to narrow the Ctrl-M rows that
            # define each workflow's actual start and end time, exactly as the
            # reference audit script does (first_job min(Start_Time) →
            # last_job max(End_Time) per run date).
            _bsla_pre = _bulk_lookup_bsla(sub_app, sub_app)
            _anchor_row: dict = {}
            if _bsla_pre:
                _pre_wf_raw = _bsla_pre[2]
                _anchor_row = (
                    _bsla_full.get(_norm(_pre_wf_raw))
                    or _bsla_full.get(norm_sub)
                    or {}
                )
            _first_anchor = (_anchor_row.get("first_job") or "").strip().upper()
            _last_anchor  = (_anchor_row.get("last_job")  or "").strip().upper()

            # ── Per-run elapsed window (wall-clock, anchor-aware) ─────────
            # Group by run_date so each daily/weekly execution is measured
            # independently. Within each run, narrow to first_job start →
            # last_job end when XLSX anchor names are available (reference
            # script first_job/last_job matching logic). Fall back to
            # min(all starts) → max(all ends) when no anchors exist.
            per_run_elapsed: list[float] = []
            per_run_windows: list[tuple] = []  # (start_ts, end_ts)
            per_run_dates:   list[str]   = []  # run date strings
            per_run_end_clk: list[int | None] = []  # end minute-of-day for clock-SLA check

            for _rd, rg in grp.groupby("_run_date"):
                if _rd == "unknown":
                    continue
                rg_starts = rg["_start"].dropna()
                rg_ends   = rg["_end"].dropna()

                # Anchor narrowing — try EXACT match first, then substring fallback.
                # str.contains() was matching "PROCESS" in "PRE_PROCESS_VALIDATE" etc.,
                # pulling in unrelated jobs and inflating the elapsed window.
                if _first_anchor and "Job_Name" in rg.columns:
                    _jnames = rg["Job_Name"].str.upper()
                    _exact_first = _jnames == _first_anchor
                    _fm = _exact_first if _exact_first.any() else _jnames.str.contains(
                        _first_anchor, na=False, regex=False
                    )
                    if _fm.any():
                        rg_starts = rg.loc[_fm, "_start"].dropna()

                if _last_anchor and "Job_Name" in rg.columns:
                    _jnames = rg["Job_Name"].str.upper()
                    _exact_last = _jnames == _last_anchor
                    _lm = _exact_last if _exact_last.any() else _jnames.str.contains(
                        _last_anchor, na=False, regex=False
                    )
                    if _lm.any():
                        rg_ends = rg.loc[_lm, "_end"].dropna()

                if not rg_starts.empty and not rg_ends.empty:
                    _rs = rg_starts.min()
                    _re = rg_ends.max()
                    elapsed = (_re - _rs).total_seconds() / 3600

                    # ── Run-cluster guard (#21): if elapsed > 4× expected SLA,
                    # the date group likely contains two independent executions
                    # (e.g. month-end run + manual retry run). Cluster by job-chain
                    # continuity (gap > 2h between consecutive jobs = new cluster).
                    # Take the worst (longest) cluster, not the full span.
                    if elapsed > 12 and not rg_starts.empty and not rg_ends.empty:
                        try:
                            _cluster_starts = rg["_start"].dropna().sort_values()
                            _cluster_ends   = rg["_end"].dropna()
                            _sorted_jobs    = rg.dropna(subset=["_start"]).sort_values("_start")
                            # Build clusters: new cluster when gap from last end to next start > 2h
                            _clusters: list[tuple] = []
                            _c_start = _sorted_jobs.iloc[0]["_start"]
                            _c_end   = _sorted_jobs.iloc[0]["_end"] if pd.notna(_sorted_jobs.iloc[0]["_end"]) else _sorted_jobs.iloc[0]["_start"]
                            for _idx in range(1, len(_sorted_jobs)):
                                _row = _sorted_jobs.iloc[_idx]
                                _gap_h = (_row["_start"] - _c_end).total_seconds() / 3600 if pd.notna(_row["_start"]) and pd.notna(_c_end) else 0
                                if _gap_h > 2.0:  # new cluster
                                    _clusters.append((_c_start, _c_end))
                                    _c_start = _row["_start"]
                                _c_end = max(
                                    _c_end if pd.notna(_c_end) else _row["_start"],
                                    _row["_end"] if pd.notna(_row["_end"]) else _c_end or _row["_start"],
                                )
                            _clusters.append((_c_start, _c_end))
                            # Worst cluster = longest duration
                            _best_elapsed = max(
                                ((_ce - _cs).total_seconds() / 3600 for _cs, _ce in _clusters if pd.notna(_cs) and pd.notna(_ce)),
                                default=elapsed,
                            )
                            if 0 <= _best_elapsed <= elapsed:
                                elapsed = _best_elapsed
                                # Recalculate _rs / _re from the worst cluster
                                _worst_c = max(_clusters, key=lambda c: (c[1] - c[0]).total_seconds() if pd.notna(c[0]) and pd.notna(c[1]) else 0)
                                _rs, _re = _worst_c
                        except Exception:
                            pass  # keep original elapsed on any cluster-detection error

                    # Batch-type-aware sanity cap (fix #7):
                    # DAILY ≤ 48h, WEEKLY ≤ 200h, MONTHLY/BIWEEKLY ≤ 400h.
                    # Old fixed 48h cap silently dropped valid 50h+ monthly batches,
                    # reporting RUNTIME_MISSING instead of an actionable AT_RISK finding.
                    # Detect inline — batch_type_wf is assigned later in this loop, so
                    # referencing it here would be unbound (first sub_app) or stale.
                    try:
                        from services.sla_merger import detect_batch_type as _dbt_cap
                        _bt_cap = _dbt_cap(sub_app, "") or "DAILY"
                    except Exception:
                        _bt_cap = "DAILY"
                    _MAX_ELAPSED: dict[str, float] = {
                        "DAILY": 48.0, "WEEKLY": 200.0,
                        "BIWEEKLY": 400.0, "MONTHLY": 400.0,
                    }
                    _cap = _MAX_ELAPSED.get(_bt_cap, 48.0)
                    if 0 <= elapsed <= _cap:
                        per_run_elapsed.append(elapsed)
                        per_run_windows.append((_rs, _re))
                        per_run_dates.append(str(_rd))
                        per_run_end_clk.append(
                            _re.hour * 60 + _re.minute if pd.notna(_re) else None
                        )
                    elif elapsed > _cap:
                        # Include but tag as anomalous — do not silently discard
                        per_run_elapsed.append(elapsed)
                        per_run_windows.append((_rs, _re))
                        per_run_dates.append(str(_rd))
                        per_run_end_clk.append(
                            _re.hour * 60 + _re.minute if pd.notna(_re) else None
                        )

            if per_run_elapsed:
                # Worst-case (max elapsed) run as representative runtime
                max_idx   = per_run_elapsed.index(max(per_run_elapsed))
                runtime_h = round(per_run_elapsed[max_idx], 4)
                wf_start  = per_run_windows[max_idx][0]
                wf_end    = per_run_windows[max_idx][1]
                wf_start_s = wf_start.strftime("%Y-%m-%d %H:%M") if pd.notna(wf_start) else None
                wf_end_s   = wf_end.strftime("%Y-%m-%d %H:%M")   if pd.notna(wf_end)   else None
                anchor_tag = "anchored" if (_first_anchor or _last_anchor) else "all_jobs"
                runtime_src = f"per_run_max_elapsed/{anchor_tag} ({len(per_run_elapsed)} runs)"
            elif "run_time_hrs" in grp.columns:
                runtime_h    = round(float(grp["run_time_hrs"].fillna(0).max()), 4)
                wf_start_s   = wf_end_s = None
                runtime_src  = "max_run_hrs"
            else:
                runtime_h    = 0.0
                wf_start_s   = wf_end_s = None
                runtime_src  = "none"

            # ── SLA resolution — single unified resolver, all tiers ──────
            sla_h_wf:          float | None = None
            raw_batch_name_wf: str   | None = None
            join_hit  = False
            sla_src_wf = "none"

            # Tier 1 — use the pre-computed hit (already resolved above for anchors).
            # Reuse avoids a second pass through _bulk_lookup_bsla.
            if _bsla_pre:
                sla_h_wf, sla_src_wf, raw_batch_name_wf = _bsla_pre
                join_hit = True

            # Tier 2 — SOW-extracted batch-type ceiling
            if sla_h_wf is None and _sow_windows:
                try:
                    from services.sla_merger import detect_batch_type as _dbt
                    _bt2 = _dbt(sub_app, "")
                    if _bt2 and _bt2 in _sow_windows:
                        _e2 = _sow_windows[_bt2]
                        _c2 = float(_e2.get("limit_hours", 0)) if isinstance(_e2, dict) else float(_e2)
                        if _c2 > 0:
                            sla_h_wf   = _c2
                            sla_src_wf = "sow_extracted"
                except Exception:
                    pass

            # Tier 3 — batch-type-aware global default (UI mode must NOT override)
            if sla_h_wf is None:
                _WF_DEFAULTS: dict[str, float] = {
                    "DAILY": 6.0, "WEEKLY": 8.0, "BIWEEKLY": 12.0, "MONTHLY": 10.0,
                }
                try:
                    from services.sla_merger import detect_batch_type as _dbt3
                    _bt3 = _dbt3(sub_app, "")
                    sla_h_wf   = _WF_DEFAULTS.get(_bt3, global_sla_hrs)
                    sla_src_wf = f"global_default_{_bt3}" if _bt3 else "global_fallback"
                except Exception:
                    sla_h_wf   = global_sla_hrs
                    sla_src_wf = "global_fallback"

            # Detect batch type
            try:
                from services.sla_merger import detect_batch_type as _dbt_wf
                batch_type_wf = _dbt_wf(sub_app, "")
            except Exception:
                batch_type_wf = "UNKNOWN"

            # ── Buffer formula (workflow level) ───────────────────────
            if runtime_h <= 0:
                buf_wf    = None
                status_wf = "RUNTIME_MISSING"
                buf_rsn   = "No valid Start_Time/End_Time in Ctrl-M export for this workflow"
            elif not sla_h_wf or sla_h_wf <= 0:
                buf_wf    = None
                status_wf = "SLA_MISSING"
                buf_rsn   = "No SLA resolved from Tier 1 (XLSX) / Tier 2 (SOW) / Tier 3 (defaults)"
            else:
                buf_wf  = round((sla_h_wf - runtime_h) / sla_h_wf * 100, 2)
                buf_rsn = (f"(SLA {sla_h_wf}h − runtime {runtime_h:.3f}h) / "
                           f"SLA {sla_h_wf}h × 100")
                _at  = pe_config.SLA_ATRISK_PCT
                _lj  = pe_config.SLA_LONGJOB_PCT
                if buf_wf <= 0:
                    status_wf = "BREACH"
                elif buf_wf <= _at:
                    status_wf = "AT_RISK"
                elif buf_wf <= _lj:
                    status_wf = "LONG_JOB"
                else:
                    status_wf = "OK"

            # ── Per-run breach dates (matches reference script runs[] list) ──
            # Identify which run dates individually exceeded the SLA window.
            breach_run_dates: list[str] = []
            if sla_h_wf and sla_h_wf > 0:
                for _i_r, (_elap_r, _rd_r) in enumerate(
                    zip(per_run_elapsed, per_run_dates)
                ):
                    if _elap_r > sla_h_wf:
                        breach_run_dates.append(_rd_r)

            # ── Failed job count per workflow (reference script any_failed) ──
            # Counts Ctrl-M jobs in this workflow whose status is NOT a success.
            failed_job_count = 0
            try:
                from services.pe_utils import SUCCESS_STATUSES as _SS
                if "Status" in grp.columns:
                    failed_job_count = int(
                        (~grp["Status"].str.strip().str.upper().isin(_SS)).sum()
                    )
            except Exception:
                pass

            # ── Clock-time SLA buffer (reference script midnight_diff logic) ──
            # When the XLSX stored a fixed SLA deadline (e.g. "07:00"), compute
            # how many minutes before/after that deadline the batch actually ended.
            # Positive = within SLA; negative = breach.
            clock_sla_end_m:   int | None = None
            clock_buffer_mins: int | None = None
            clock_sla_status:  str | None = None
            _sla_end_t = _anchor_row.get("sla_end_time")
            if _sla_end_t:
                try:
                    _cet = pd.Timestamp(str(_sla_end_t))
                    clock_sla_end_m = _cet.hour * 60 + _cet.minute
                    _valid_clks = [c for c in per_run_end_clk if c is not None]
                    if _valid_clks:
                        # Worst = furthest past the deadline (reference: midnight_diff)
                        def _mdiff(actual_m: int) -> int:
                            d = clock_sla_end_m - actual_m
                            if d > 720:  d -= 1440
                            if d < -720: d += 1440
                            return d
                        clock_buffer_mins = min(_mdiff(m) for m in _valid_clks)
                        clock_sla_status = "OK" if clock_buffer_mins >= 0 else "BREACH"
                except Exception:
                    pass

            # ── SOW tier check (reference script sow_check dict) ──────────
            # When SOW is the SLA source, also surface the raw SOW window and
            # average actual duration so the UI can show e.g.
            # "SOW says 8h · avg actual 6.3h · +1.7h buffer".
            sow_window_hrs:      float | None = None
            sow_avg_runtime_hrs: float | None = None
            sow_buffer_hrs:      float | None = None
            sow_status_wf:       str   | None = None
            if sla_src_wf == "sow_extracted" and _sow_windows:
                try:
                    _bt_sw = batch_type_wf or "DAILY"
                    _sw_e  = _sow_windows.get(_bt_sw) or {}
                    _sw_h  = float(
                        _sw_e.get("limit_hours", 0) if isinstance(_sw_e, dict) else _sw_e
                    ) or None
                    if _sw_h and per_run_elapsed:
                        sow_window_hrs      = _sw_h
                        sow_avg_runtime_hrs = round(
                            sum(per_run_elapsed) / len(per_run_elapsed), 3
                        )
                        sow_buffer_hrs = round(sow_window_hrs - sow_avg_runtime_hrs, 3)
                        sow_status_wf  = "OK" if sow_buffer_hrs >= 0 else "BREACH"
                except Exception:
                    pass

            workflow_summary.append({
                # ── Canonical columns ──
                "workflow_key":    norm_sub,
                "workflow_name":   raw_batch_name_wf or sub_app,
                "sub_application": sub_app,
                "batch_type":      batch_type_wf or "UNKNOWN",
                "workflow_start":  wf_start_s,
                "workflow_end":    wf_end_s,
                "runtime_h":       runtime_h,
                "sla_h":           round(float(sla_h_wf), 4) if sla_h_wf else None,
                "sla_source":      sla_src_wf,
                "buffer_pct":      buf_wf,
                "status":          status_wf,
                "job_count":       int(len(grp)),
                # ── Multi-run analysis (matches reference script three-tier logic) ──
                "total_runs":       len(per_run_elapsed),
                "breach_run_count": len(breach_run_dates),
                "breach_run_dates": breach_run_dates,
                "failed_job_count": failed_job_count,
                # ── Anchor metadata ──────────────────────────────────────────
                "first_job_anchor": _first_anchor or None,
                "last_job_anchor":  _last_anchor  or None,
                "anchor_used":      bool(_first_anchor or _last_anchor),
                # ── Clock-time SLA (reference midnight_diff check) ───────────
                "clock_sla_end_m":   clock_sla_end_m,
                "clock_buffer_mins": clock_buffer_mins,
                "clock_sla_status":  clock_sla_status,
                # ── SOW tier check ────────────────────────────────────────────
                "sow_window_hrs":      sow_window_hrs,
                "sow_avg_runtime_hrs": sow_avg_runtime_hrs,
                "sow_buffer_hrs":      sow_buffer_hrs,
                "sow_status":          sow_status_wf,
                # ── Debug columns (always included — hidden in UI by default) ──
                "debug_raw_subapp":        sub_app,
                "debug_raw_batch_name":    raw_batch_name_wf,
                "debug_normalized_subapp": norm_sub,
                "debug_normalized_batch":  _norm(raw_batch_name_wf) if raw_batch_name_wf else None,
                "debug_join_hit":          join_hit,
                "debug_sla_source":        sla_src_wf,
                "debug_runtime_source":    runtime_src,
                "debug_buffer_reason":     buf_rsn,
            })
    except Exception as _wf_exc:
        import logging as _log_wf
        _log_wf.getLogger("pe_dashboard.sla_matrix").warning(
            "workflow_summary failed: %s", _wf_exc, exc_info=True
        )
        workflow_summary = []

    # Worst job
    worst_job     = ""
    worst_hrs_val = 0.0
    worst_margin  = 0.0
    if detail:
        worst = max(detail, key=lambda r: r["run_hrs"])
        worst_job    = worst["job_name"]
        worst_hrs_val = worst["run_hrs"]
        worst_margin = worst["breach_margin_hrs"]

    total_jobs = int(rdf["job_name"].nunique()) if not rdf.empty else 0

    # Update label to reflect actual source — always preserve the mode label so the
    # user can see their selection. Append SLA file context as a suffix.
    if explicit_sla_matrix:
        sla_label = f"Per-Job SLA file + {sla_label} fallback"
    elif has_per_job_sla:
        sla_label = f"Schedule ceilings + {sla_label} fallback"
    # else: keep the mode-only label (e.g. "Daily SLA (4.0h)")

    # ── Window-level compliance (daily elapsed wall-clock vs SLA) ────────
    # This is the same metric the Executive Dashboard uses — it measures
    # whether the entire batch completed within its SLA window each day,
    # NOT whether individual job runtimes are under the ceiling.
    window_comp_pct = None
    w_total_days = None
    w_breach_days = None
    w_detail_list = None
    window_warnings: list[str] = []
    try:
        has_end = "End_Time" in df.columns and df["End_Time"].notna().sum() > 0
        if has_end:
            wdf = df.copy()
            wdf["Start_Time"] = pd.to_datetime(wdf["Start_Time"], errors="coerce")
            wdf["End_Time"]   = pd.to_datetime(wdf["End_Time"],   errors="coerce")
            wdf = wdf.dropna(subset=["Start_Time", "End_Time"])
            if "run_date" not in wdf.columns:
                wdf["run_date"] = wdf["Start_Time"].dt.strftime("%Y-%m-%d")

            # Fix #3: use per-Sub_Application resolved SLA for the breach comparison.
            # build_ceiling_map() is the SHARED source of truth — same function used by
            # batch_calculator — so Batch Review and SLA Matrix can never diverge.
            _sub_apps_all = wdf["Sub_Application"].dropna().unique().tolist() if "Sub_Application" in wdf.columns else []
            try:
                from services import compliance_engine as _ce_slm
                from services import config_store as _cs_slm
                from services import pe_config as _pc_slm
                _sub_sla_lookup: dict[str, float] = _ce_slm.build_ceiling_map(
                    sub_applications=_sub_apps_all,
                    xlsx_config=_cs_slm.get("_batch_sla_xlsx") or None,
                    pe_config_ref=_pc_slm,
                )
            except Exception:
                # Safe fallback — use global SLA for all sub_apps
                _sub_sla_lookup = {str(_sa): global_sla_hrs for _sa in _sub_apps_all}

            # Canonical window: first job start → last job end per
            # (Sub_Application, run_date). Each sub-app is judged against ITS OWN
            # resolved ceiling via the shared engine — same rule as Batch Review.
            _has_sub = "Sub_Application" in wdf.columns and wdf["Sub_Application"].notna().any()
            _grp_keys = ["run_date", "Sub_Application"] if _has_sub else ["run_date"]
            wgrp = (
                wdf.groupby(_grp_keys)
                .agg(first_start=("Start_Time", "min"),
                     last_end=("End_Time", "max"),
                     job_count=("Job_Name", "nunique"))
                .reset_index()
                .dropna(subset=["first_start", "last_end"])
            )

            # Resolve per-row schedule type for exclusion of cyclic/outbound etc.
            def _slm_sched(_sa: str) -> str:
                try:
                    from services.sla_engine import classify_schedule as _csl
                    return str(_csl(str(_sa))).upper()
                except Exception:
                    return ""

            if not wgrp.empty:
                wgrp["elapsed_hrs"] = (
                    (wgrp["last_end"] - wgrp["first_start"]).dt.total_seconds() / 3600.0
                ).clip(lower=0).round(3)
                if _has_sub:
                    wgrp["sla_hrs"] = wgrp["Sub_Application"].apply(
                        lambda _sa: float(_sub_sla_lookup.get(str(_sa).upper(), global_sla_hrs))
                    )
                    wgrp["schedule_type"] = wgrp["Sub_Application"].apply(_slm_sched)
                else:
                    wgrp["sla_hrs"] = global_sla_hrs
                    wgrp["schedule_type"] = ""
                wgrp["breach"] = wgrp["elapsed_hrs"] > wgrp["sla_hrs"]

            if not wgrp.empty:
                w_total_days = int(wgrp["run_date"].nunique())
                w_breach_days = int(wgrp.loc[wgrp["breach"], "run_date"].nunique())
                # PROMPT 3: Do NOT compute window_comp_pct from local formula.
                # compliance_engine is the sole authoritative source. If it fails,
                # window_comp_pct stays None → headline shows "—" (not a wrong number).
                w_detail_list = [
                    {"run_date": str(r["run_date"]),
                     "sub_app": str(r["Sub_Application"]) if _has_sub else "",
                     "elapsed_hrs": round(float(r["elapsed_hrs"]), 3),
                     "job_count": int(r.get("job_count", 0)),
                     "breach": bool(r["breach"]),
                     "schedule_type": str(r.get("schedule_type", "")),
                     "sla_ceil": float(r.get("sla_hrs", global_sla_hrs)),
                     "sla_hrs": float(r.get("sla_hrs", global_sla_hrs))}
                    for _, r in wgrp.iterrows()
                ]

                # ── Canonical window compliance via SHARED engine (sole path) ─
                # Pass the real per-sub-app ceiling map (NOT {}). w_detail_list
                # already carries per-row sla_ceil + schedule_type, so the engine
                # judges each (sub_app, date) on its own contracted ceiling.
                try:
                    from services import compliance_engine as _ce
                    _wc = _ce.compute_window_compliance(w_detail_list, _sub_sla_lookup)
                    window_comp_pct = _wc["compliance_pct"]
                    w_total_days    = _wc.get("total_days", _wc["total_windows"])
                    w_breach_days   = _wc.get("breach_days", _wc["breach_count"])
                    window_warnings = _wc.get("warnings") or []
                except Exception:
                    pass
                file_days = int(wdf["run_date"].nunique()) if "run_date" in wdf.columns else len(w_detail_list)
                if w_total_days is not None and file_days and w_total_days != file_days:
                    window_warnings.append(
                        f"Window denominator mismatch: {file_days} unique date(s) loaded but {w_total_days} day(s) were analyzed."
                    )
    except Exception:
        pass

    # When window compliance is available, use it as the headline compliance
    # to match the Executive Dashboard — prevents contradictory stories.
    headline_compliance = window_comp_pct if window_comp_pct is not None else compliance

    # Detect aggregated format — when all job names are blank/NO_JOBNAME/Sub_Application totals
    _named_jobs = [
        j for j in job_summary
        if j.get("job_name", "").upper() not in ("", "NO_JOBNAME", "AGGREGATED", "TOTAL", "UNKNOWN")
    ]
    data_format = "per_job" if _named_jobs else "aggregated"

    resp = SlaMatrixResponse(
        sla_mode=sla_mode, sla_limit_hrs=global_sla_hrs, sla_label=sla_label,
        total_runs=total_runs, total_jobs=total_jobs,
        breaching_runs=breach_count, at_risk_runs=atrisk_count,
        long_job_runs=longjob_count, failed_runs=failed_count,
        ok_runs=ok_count,
        compliance_pct=headline_compliance, breach_rate_pct=breach_rate,
        worst_job=worst_job if worst_job else None,
        worst_hrs=worst_hrs_val, worst_margin_hrs=worst_margin,
        breaches=[SlaBreach(**{k: r[k] for k in SlaBreach.model_fields if k in r}) for r in detail],
        job_summary=job_summary,
        window_compliance_pct=window_comp_pct,
        window_total_days=w_total_days,
        window_breach_days=w_breach_days,
        window_detail=w_detail_list,
        window_warnings=window_warnings or None,
        explicit_sla_matrix=explicit_sla_matrix,
        data_format=data_format,
        workflow_summary=workflow_summary or None,
    )

    # ── Adaptive per-job baselines + resource correlation ──────────
    # Compute even when no servers/heatmap are loaded — the baseline alone
    # surfaces job outliers that the global SLA doesn't catch.
    try:
        from services.job_baselines import (
            compute_job_baselines,
            enrich_runs_with_baselines,
            correlate_with_resource_hours,
        )
        baselines = compute_job_baselines(df)
        if baselines:
            resp.job_baselines = baselines
            enriched_runs = enrich_runs_with_baselines(df, baselines, sla_hrs=sla_hrs)
            outliers = [
                r for r in enriched_runs
                if r["is_job_outlier"] and not r["is_sla_breach"]
            ][:200]
            resp.outliers = outliers or None

            # Resource-link breaches + outliers when we have server context.
            from services import session_cache
            ad_resource = session_cache.get("last_resource", {}) or {}
            servers = (ad_resource.get("servers") or []) if isinstance(ad_resource, dict) else []
            hour_heatmap = session_cache.get("last_hour_heatmap")
            linked = correlate_with_resource_hours(
                enriched_runs, hour_heatmap, servers,
            )
            resp.resource_linked = linked[:200] or None
    except Exception as _exc:  # noqa: BLE001
        import logging
        logging.getLogger("pe_dashboard.sla_matrix").debug(
            "adaptive baselines skipped: %s", _exc,
        )
    # Cache the latest SLA matrix so agent tools can read it
    try:
        from services import session_cache
        mx_dict = resp.model_dump()
        session_cache.set("last_sla_matrix", mx_dict)
        # ── Audit context: E1 sla_resolved, job_summary, adaptive_sla ────────
        session_cache.ac_set("sla_resolved",    mx_dict.get("breaches")      or [])
        session_cache.ac_set("sla_job_summary", mx_dict.get("job_summary")   or [])
        session_cache.ac_set("job_summary",     mx_dict.get("job_summary")   or [])
        session_cache.ac_set("adaptive_sla",    mx_dict.get("job_baselines") or [])
        session_cache.ac_set("sla_matrix_kpis", {
            "compliance_pct":          mx_dict.get("compliance_pct"),
            "run_sla_compliance_pct":  mx_dict.get("compliance_pct"),
            "breaching_runs":          mx_dict.get("breaching_runs"),
            "at_risk_runs":            mx_dict.get("at_risk_runs"),
            "long_job_runs":           mx_dict.get("long_job_runs"),
            "failed_runs":             mx_dict.get("failed_runs"),
            "ok_runs":                 mx_dict.get("ok_runs"),
            "total_runs":              mx_dict.get("total_runs"),
            "sla_limit_hrs":           mx_dict.get("sla_limit_hrs"),
            "window_breach_days":      mx_dict.get("window_breach_days"),
            "window_total_days":       mx_dict.get("window_total_days"),
        })
        # ── Canonical per-workflow resolved dataframe (single source of truth) ──
        # Written here so Batch Review, PE Findings, PE Narrative, and the
        # SLA commitments panel all read the same numbers with no re-derivation.
        session_cache.ac_set("workflow_sla_summary", mx_dict.get("workflow_summary") or [])
    except Exception:
        pass

    # ── Smartness gate (display layer) ─────────────────────────────
    # Filter parser-noise + magnitude-noise rows just before the
    # payload is shipped to the UI, and attach an audit block.
    try:
        from services.display_gate import gate
        cleaned = gate(resp.model_dump(), kind="sla_matrix")
        # Re-hydrate the response from the cleaned payload (fields the
        # gate touches: outliers, resource_linked, _gate audit).
        resp.outliers        = cleaned.get("outliers")
        resp.resource_linked = cleaned.get("resource_linked")
        # Stash the audit block on the response so the UI can show it.
        try:
            resp.gate_audit = cleaned.get("_gate")
        except Exception:
            pass
    except Exception:
        pass

    return resp


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/sla-matrix", response_model=SlaMatrixResponse)
async def sla_matrix_upload(
    file:     UploadFile = File(...),
    sla_mode: str        = Form("daily"),
    sla_hrs:  float      = Form(0.0),
) -> SlaMatrixResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    try:
        df = load_ctrlm_bytes(raw, file.filename or "")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot parse Ctrl-M file: {exc}") from exc

    custom = float(sla_hrs) if sla_hrs and sla_hrs > 0 else config_store.get("custom_sla_hrs", 6.0)
    return _compute_sla_matrix(df, sla_mode, custom)


@router.post("/sla-matrix/json", response_model=SlaMatrixResponse)
def sla_matrix_json(body: JsonSlaRequest) -> SlaMatrixResponse:
    """Accept pre-parsed Ctrl-M rows (same format as /api/process-batch JSON).

    Prefers the full-run dataframe stored in session_cache (job_runs_df) over
    the truncated body.rows sample, so compliance numbers reflect ALL runs.
    """
    import pandas as pd

    # Prefer the full-dataset stored at upload time over the truncated body.rows
    _full_rows = None
    try:
        from services import session_cache as _sc_jrd
        _full_rows = _sc_jrd.get("job_runs_df")
    except Exception:
        pass

    if _full_rows:
        df = pd.DataFrame(_full_rows)
    elif body.rows:
        df = pd.DataFrame(body.rows)
    else:
        return SlaMatrixResponse(
            sla_mode="daily", sla_limit_hrs=6.0, sla_label="Daily SLA",
            total_runs=0, total_jobs=0, breaching_runs=0, at_risk_runs=0, ok_runs=0,
            compliance_pct=100.0, breach_rate_pct=0.0,
            worst_job=None, worst_hrs=0.0, worst_margin_hrs=0.0,
            breaches=[], job_summary=[],
        )

    # Ensure run_time_hrs
    if "run_time_hrs" not in df.columns and "Run_Sec" in df.columns:
        df["run_time_hrs"] = pd.to_numeric(df["Run_Sec"], errors="coerce").fillna(0) / 3600.0
    elif "run_time_hrs" not in df.columns and "peak_hrs" in df.columns:
        df["run_time_hrs"] = pd.to_numeric(df["peak_hrs"], errors="coerce").fillna(0)

    custom = body.sla_hrs or config_store.get("custom_sla_hrs", 6.0)
    # Use auto-detected mode as default when no explicit override in request
    try:
        from services import session_cache as _sc_sm
        _detected = (_sc_sm.ac_get("sla_detected_mode") or "").lower()
    except Exception:
        _detected = ""
    return _compute_sla_matrix(df, body.sla_mode or _detected or "daily", custom)


@router.get(
    "/sla-debug",
    summary="Canonical per-workflow resolved SLA dataframe with full debug columns",
)
def sla_debug() -> Dict[str, Any]:
    """Return the canonical workflow_sla_summary from session cache.

    Includes all debug columns (join_hit, runtime_source, buffer_reason, etc.)
    so the UI can show exactly why each workflow got the SLA and buffer it did.
    Also shows where each metric came from (Tier 1 XLSX / Tier 2 SOW / Tier 3 default).
    """
    try:
        from services import session_cache
        wf_df = session_cache.ac_get("workflow_sla_summary") or []
    except Exception:
        wf_df = []

    breaches  = [r for r in wf_df if r.get("status") == "BREACH"]
    mismatches = [r for r in wf_df if not r.get("debug_join_hit")]
    tier_counts: Dict[str, int] = {}
    for r in wf_df:
        src = r.get("sla_source") or "unknown"
        tier_counts[src] = tier_counts.get(src, 0) + 1

    return {
        "workflow_count":    len(wf_df),
        "breach_count":      len(breaches),
        "no_join_count":     len(mismatches),
        "tier_distribution": tier_counts,
        "workflows":         wf_df,
        # Diagnostic: workflows where join FAILED (these will use Tier 2/3 fallbacks)
        "join_misses":       [
            {
                "sub_application":         r.get("sub_application"),
                "debug_normalized_subapp": r.get("debug_normalized_subapp"),
                "sla_source":              r.get("sla_source"),
                "sla_h":                   r.get("sla_h"),
                "runtime_h":               r.get("runtime_h"),
                "buffer_pct":              r.get("buffer_pct"),
                "status":                  r.get("status"),
                "debug_buffer_reason":     r.get("debug_buffer_reason"),
            }
            for r in mismatches
        ],
    }


# ── SLA Commitments AI Interpretation ────────────────────────────────────────

@router.post(
    "/sla-commitments/interpret",
    summary="LLM interpretation of the active SLA commitments panel — PE-grade analysis",
)
async def sla_commitments_interpret() -> Dict[str, Any]:
    """Build a precision PE prompt from live workflow_summary and run it through
    the AI engine. Returns { text, model }.

    The prompt explicitly gives the LLM:
      - The exact buffer formula and threshold definitions
      - Per-workflow numbers (runtime, SLA, buffer %, status, source tier)
      - Velocity of risk: how much slowdown triggers a breach for each workflow
      - Data provenance (XLSX snapshot vs live Ctrl-M worst-case)
    So the LLM cannot hallucinate values — everything is grounded in the actual data.
    """
    from services import session_cache
    from services.ai_engine import chat as _ai_chat, is_ready as _ai_status
    from services import pe_config as _pc

    # ── Guard: AI must be configured ─────────────────────────────────────
    ai_state = _ai_status()
    if not ai_state.get("nvidia_key") and not ai_state.get("gemini_key"):
        return {
            "text":  "No AI key configured. Add an NVIDIA NIM key or Gemini key in Settings → AI Engine.",
            "model": "none",
        }

    # ── Pull live workflow data from canonical session cache ──────────────
    wf_rows: list[dict] = session_cache.ac_get("workflow_sla_summary") or []

    # Fallback: use batchSlaInfo workflows from config if no Ctrl-M run yet
    # (these have last_run_hours_xlsx as runtime — labelled XLSX snapshot)
    if not wf_rows:
        try:
            from services import config_store as _cs
            bsla = _cs.get("_batch_sla_xlsx") or {}
            raw_wfs = bsla.get("workflows") or []
            for w in raw_wfs:
                rt = w.get("last_run_hours_xlsx")
                sla_h = w.get("sla_hours")
                buf = round((sla_h - rt) / sla_h * 100, 2) if rt and sla_h and sla_h > 0 else None
                from services.sla_merger import compliance_label
                wf_rows.append({
                    "workflow_name":   w.get("workflow", "?"),
                    "batch_type":      w.get("batch_type", "?"),
                    "runtime_h":       rt,
                    "sla_h":           sla_h,
                    "buffer_pct":      buf,
                    "status":          compliance_label(rt, sla_h) if rt else "RUNTIME_MISSING",
                    "sla_source":      "batch_sla_xlsx",
                    "debug_runtime_source": "xlsx_last_run",
                })
        except Exception:
            pass

    if not wf_rows:
        return {
            "text":  "No workflow data available. Upload BatchSLA_info.xlsx and/or run the SLA Matrix first.",
            "model": "none",
        }

    # ── Build precision prompt ────────────────────────────────────────────
    _at = _pc.SLA_ATRISK_PCT
    _lj = _pc.SLA_LONGJOB_PCT

    # Per-workflow lines with full math
    wf_lines = []
    for w in wf_rows:
        name    = w.get("workflow_name") or w.get("sub_application") or "?"
        rt      = w.get("runtime_h")
        sla_h   = w.get("sla_h")
        buf     = w.get("buffer_pct")
        status  = w.get("status", "?")
        src     = w.get("debug_runtime_source") or w.get("sla_source") or "?"
        btype   = w.get("batch_type", "?")

        # Compute breach velocity: how many extra minutes before SLA exceeded
        if rt is not None and sla_h and sla_h > 0 and buf is not None:
            headroom_min = round((sla_h - rt) * 60, 1)
            slowdown_pct = round(buf, 1)  # buffer IS the % slowdown that triggers breach
            formula = (f"buffer = ({sla_h}h SLA − {rt:.3f}h runtime) / {sla_h}h × 100 = {buf:.1f}%  "
                       f"→ {headroom_min} min headroom, "
                       f"needs {slowdown_pct}% further slowdown to breach")
        elif buf is None:
            formula = f"buffer = {status} (no runtime or SLA available)"
        else:
            formula = f"buffer = {buf:.1f}%"

        rt_note = "(XLSX last-run snapshot — not worst-case)" if "xlsx" in src else "(Ctrl-M worst-case per-run)"

        wf_lines.append(
            f"  • {name}  [{btype}]  runtime={rt:.3f}h {rt_note}  SLA={sla_h}h  "
            f"buffer={buf:.1f}%  status={status}  sla_source={w.get('sla_source','?')}\n"
            f"    {formula}"
        )

    wf_block = "\n".join(wf_lines)

    # Counts
    statuses = [w.get("status", "") for w in wf_rows]
    n_breach  = statuses.count("BREACH")
    n_atrisk  = statuses.count("AT_RISK")
    n_longjob = statuses.count("LONG_JOB")
    n_ok      = statuses.count("OK")
    n_miss    = sum(1 for s in statuses if s in ("SLA_MISSING", "RUNTIME_MISSING"))

    prompt = f"""SLA COMMITMENTS DATA ({len(wf_rows)} workflows):
{wf_block}

BREACH={n_breach} | AT_RISK={n_atrisk} | LONG_JOB={n_longjob} | OK={n_ok} | MISSING={n_miss}

Write exactly 5 sentences:
1. One sentence stating what buffer % represents for these workflows (remaining SLA headroom, not a generic definition).
2. One sentence per workflow stating its SLA, snapshot runtime, buffer %, and status classification — be factual, name the workflow.
3. One sentence noting that all values are XLSX snapshot only and do not represent worst-case historical runtime.
4. One sentence identifying the workflow with the tighter operating margin and why it should be prioritized.
5. One sentence giving the single most important concrete action for the highest-risk workflow.
Do not explain the formula. Do not use bullet points. Do not add headers. Output only the 5 sentences."""

    try:
        _system = (
            "You are a Senior Performance Engineering consultant writing a brief assessment. "
            "Output only your final conclusions — no reasoning, no working, no preamble. "
            "Be direct. Quote exact numbers. 150 words maximum."
        )
        text, model = _ai_chat(prompt, system=_system, max_tokens=400, temperature=0.2)
        # Strip any chain-of-thought leakage (lines starting with "We need to", "Let me", "I need to", etc.)
        import re as _re
        lines = text.strip().splitlines()
        cleaned = []
        skip_prefixes = ("we need to", "let me", "i need to", "i will", "the user wants",
                         "we want to", "we should", "first,", "to answer", "i'll")
        for line in lines:
            if line.strip().lower().startswith(skip_prefixes):
                continue
            cleaned.append(line)
        text = "\n".join(cleaned).strip()
        return {"text": text, "model": model}
    except Exception as exc:
        return {
            "text":  f"AI engine error: {str(exc)[:200]}. Check Settings → AI Engine.",
            "model": "error",
        }
