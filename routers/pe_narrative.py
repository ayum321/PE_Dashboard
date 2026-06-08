"""
PE Narrative API — Fixed version.

Root-cause fixes applied:
  Bug 1  _digest() now reads last_smart_findings as batch fallback
  Bug 2  _pe_narrative_inner() hydration no-op when sc_batch=={} fixed
  Bug 3  _deterministic_fallback() prose guard removed; builds from smart_findings
  Bug 4  SLA matrix flat-field extraction runs from ac_snapshot, not only payload
  Bug 5  Resource field-name normalisation (avg_cpu / avg_cpu_pct / cpu_avg all handled)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("pe_dashboard.pe_narrative")
router = APIRouter()


class PeNarrativeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    batch:         Optional[Dict[str, Any]] = None
    resource:      Optional[Dict[str, Any]] = None
    sla_matrix:    Optional[Dict[str, Any]] = None
    sla_intel:     Optional[Dict[str, Any]] = None
    sow_compare:   Optional[Dict[str, Any]] = None
    benchmark:     Optional[Dict[str, Any]] = None
    red_flags:     Optional[Dict[str, Any]] = None
    findings:      Optional[Dict[str, Any]] = None
    customer_name: Optional[str]            = None
    deep_dive:     Optional[Dict[str, Any]] = None


_SECTIONS = [
    {
        "id":    "data_volume",
        "title": "Data Volume Analysis",
        "guide": (
            "Compare SOW-promised volumes (DFU, SKU, transactions, jobs) "
            "vs actual measured volumes. Compute utilisation % and headroom."
        ),
        "default_table": {
            "headers": ["Dimension", "SOW", "Actual", "Utilisation"],
            "rows":    [["NA", "NA", "NA", "NA"]],
        },
    },
    {
        "id":    "batch_sla",
        "title": "Batch Execution & SLA Compliance",
        "guide": (
            "Summarise total runs, SLA window, longest job runtime + buffer, "
            "weekly batches, success rate, failure breakdown."
        ),
        "default_table": {
            "headers": ["Workflow", "Max Runtime", "SLA Window", "Buffer"],
            "rows":    [["NA", "NA", "NA", "NA"]],
        },
    },
    {
        "id":    "infrastructure",
        "title": "Infrastructure Utilisation & Resource Health",
        "guide": (
            "Average + peak CPU / memory / disk per server role. "
            "Highlight any host >75% utilisation."
        ),
        "default_table": {
            "headers": ["Resource Type", "Avg Utilisation", "Peak Utilisation"],
            "rows":    [["NA", "NA", "NA"]],
        },
    },
    {
        "id":    "uat",
        "title": "User Acceptance Testing (UAT) Validation",
        "guide": "Summarise pass rate per category; NA if no UAT artefacts.",
        "default_table": {
            "headers": ["Test Category", "Test Cases"],
            "rows":    [["NA — UAT artefacts not loaded", "NA"]],
        },
    },
]


# ---------------------------------------------------------------------------
# HELPER: safe numeric coerce
# ---------------------------------------------------------------------------

def _num(v, default=None):
    """Return float(v) or default without raising."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# BUG 5 FIX: Resource field normalisation
# ---------------------------------------------------------------------------

def _res_cpu(kpis: dict):
    """Return average CPU % from resource KPIs, tolerating any field name."""
    for k in ("avg_cpu", "avg_cpu_pct", "cpu_avg", "fleet_avg_cpu", "cpu_utilisation"):
        v = _num(kpis.get(k))
        if v is not None:
            return v
    return None


def _res_mem(kpis: dict):
    for k in ("avg_mem", "avg_mem_pct", "mem_avg", "fleet_avg_mem", "mem_utilisation"):
        v = _num(kpis.get(k))
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# BUG 1 + 4 FIX: _digest() -- add smart_findings path + fix SLA flat extraction
# ---------------------------------------------------------------------------

def _build_narrative_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a NarrativeContext dict from session_cache + request payload.
    Shape: services.digest_schemas.NarrativeContext

    Priority order for each slot:
      audit_context (ac_snapshot) -> payload fields -> last_smart_findings fallback
    """
    from services import session_cache

    ac  = session_cache.ac_snapshot()
    out: Dict[str, Any] = {}

    # -- Batch -------------------------------------------------------------
    b      = payload.get("batch") or {}
    bk     = ac.get("batch_kpis") or b.get("kpis") or {}
    job_sum = ac.get("job_summary") or b.get("top_jobs") or []
    wf_roll = ac.get("daily_window_series") or b.get("window") or []
    anom    = ac.get("regression_df") or b.get("anomalies") or []

    # BUG 1 FIX: if batch_kpis still empty, bridge from last_smart_findings
    if not bk:
        sf = session_cache.get("last_smart_findings") or {}
        sf_kpis = sf.get("kpis") or sf.get("batch_kpis") or {}
        if sf_kpis:
            bk = sf_kpis
            log.info("_digest: batch_kpis bridged from last_smart_findings")

        # Also try to extract from smart findings summary metrics
        if not bk and sf:
            raw_sum = sf.get("summary") or {}
            extracted = {
                k: sf.get(k) for k in (
                    "total_runs", "total_jobs", "compliance_pct", "jobs_breach",
                    "fail_runs", "ok_runs", "daily_limit_hrs", "weekly_limit_hrs",
                    "batch_window_compliance", "window_breach_days", "window_total_days",
                ) if sf.get(k) is not None
            }
            if not extracted and isinstance(raw_sum, dict):
                extracted = {
                    k: raw_sum.get(k) for k in (
                        "total_runs", "total_jobs", "compliance_pct", "fail_runs",
                    ) if raw_sum.get(k) is not None
                }
            if extracted:
                bk = extracted

    if bk or job_sum or wf_roll:
        out["batch"] = {
            "kpis":          bk,
            "top_jobs":      job_sum[:10],
            "top_breaches":  (b.get("top_breaches") or [])[:10],
            "sub_stats":     (b.get("sub_stats") or [])[:8],
            "window":        wf_roll[:30],
            "anomalies":     anom[:8],
            "data_coverage": b.get("data_coverage"),
        }

    # -- SLA matrix --------------------------------------------------------
    # BUG 4 FIX: also try extracting from ac_snapshot flat fields, not only payload
    slak             = ac.get("sla_matrix_kpis") or {}
    _sla_resolved_raw = ac.get("sla_resolved") or []
    sla_resolved      = list(_sla_resolved_raw.values()) if isinstance(_sla_resolved_raw, dict) else _sla_resolved_raw
    _adaptive_raw     = ac.get("adaptive_sla") or []
    adaptive          = list(_adaptive_raw.values()) if isinstance(_adaptive_raw, dict) else _adaptive_raw
    sm           = payload.get("sla_matrix") or {}
    sc_sla       = session_cache.get("last_sla_matrix") or {}

    _flat_fields = (
        "compliance_pct", "breaching_runs", "at_risk_runs", "long_job_runs",
        "failed_runs", "ok_runs", "total_runs", "total_jobs",
        "sla_limit_hrs", "window_breach_days", "window_total_days",
    )
    if not slak:
        for src in (sm, sc_sla):
            if src:
                _ex = {k: src[k] for k in _flat_fields if src.get(k) is not None}
                if _ex:
                    slak = _ex
                    break

    if slak or sla_resolved or sm or sc_sla:
        out["sla_matrix"] = {
            "kpis":         slak,
            "job_summary":  (ac.get("job_summary") or sm.get("job_summary") or sc_sla.get("job_summary") or [])[:15],
            "breaches":     sla_resolved[:10],
            "adaptive_sla": adaptive[:8],
            "outliers":     (sm.get("outliers") or sc_sla.get("outliers") or [])[:8],
        }

    # -- Resource ----------------------------------------------------------
    res_ac = ac.get("resource_summary") or {}
    r      = payload.get("resource") or {}
    sc_res = session_cache.get("last_resource") or {}
    r_merged = r or res_ac or sc_res

    if r_merged:
        srv = r.get("servers") or res_ac.get("servers") or sc_res.get("servers") or []
        out["resource"] = {
            "kpis": r.get("kpis") or res_ac.get("kpis") or sc_res.get("kpis"),
            "servers": [
                {k: s.get(k) for k in (
                    "host", "type", "cpu_pct", "mem_pct",
                    "disk_pct", "status", "dual_pressure", "agg_trap",
                ) if s.get(k) is not None}
                for s in srv[:15]
            ],
        }

    # -- SOW / volume ------------------------------------------------------
    # sow_contract and volume_vs_sow are never persisted across restarts (removed
    # from _PERSIST_AC_SLOTS) so they are always blank until a SOW is uploaded.
    # last_sow_compare is intentionally NOT used as a fallback here — we only
    # display what was explicitly provided in this request or uploaded this session.
    sow_ac = ac.get("sow_contract")  or {}
    vvs_ac = ac.get("volume_vs_sow") or {}
    sw     = payload.get("sow_compare") or {}
    if sow_ac or vvs_ac or sw:
        out["sow_compare"]   = sw
        out["sow_contract"]  = sow_ac
        out["volume_vs_sow"] = vvs_ac

    # -- UAT ---------------------------------------------------------------
    uat_ac = ac.get("uat_df") or []
    if uat_ac:
        out["uat"] = uat_ac[:50]

    # -- SLA intel ---------------------------------------------------------
    si = payload.get("sla_intel") or {}
    if si:
        out["sla_intel"] = {
            "ceilings":   si.get("ceilings"),
            "valid_rows": si.get("valid_rows"),
            "contracts":  [
                {k: c.get(k) for k in (
                    "batch_name", "sla_window_hrs", "actual_window_hrs",
                    "buffer_hrs", "buffer_pct", "health_status",
                ) if c.get(k) is not None}
                for c in (si.get("contracts") or [])[:20]
            ],
        }

    # -- Smart findings (explicit slot for fallback access downstream) -----
    sf = session_cache.get("last_smart_findings") or {}
    if sf:
        out["smart_findings"] = sf

    # -- Red flags / benchmark ---------------------------------------------
    bm = payload.get("benchmark") or {}
    if bm:
        out["benchmark"] = {"summary": bm.get("summary")}
    rf = payload.get("red_flags") or session_cache.get("last_red_flags") or {}
    if rf:
        out["red_flags"] = {"summary": rf.get("summary") or rf}

    # -- Deep dive time-series evidence ------------------------------------
    dd = payload.get("deep_dive") or {}
    if dd.get("total_critical", 0) > 0:
        out["deep_dive"] = {
            "hours_back":     dd.get("hours_back", 24),
            "total_critical": dd.get("total_critical", 0),
            "affected_vms":   dd.get("affected_vms", 0),
            "per_vm": [
                {k: v.get(k) for k in ("vm", "role", "spike_count", "mem_used_max", "cpu_max", "trend") if v.get(k) is not None}
                for v in (dd.get("per_vm") or [])[:5]
            ],
        }

    out["customer_name"] = ac.get("customer_name") or payload.get("customer_name") or ""
    return out


# ---------------------------------------------------------------------------
# BUG 3 FIX: _deterministic_fallback() -- build rich prose from smart_findings
# ---------------------------------------------------------------------------

def _deterministic_fallback(digest: Dict[str, Any], customer: str) -> Dict[str, Any]:
    """
    Build the 4-section narrative from real data wherever available.
    Never shows 'NA' when data exists -- uses smart_findings as final fallback.
    """
    sections: List[Dict[str, Any]] = []

    b      = digest.get("batch")        or {}
    bk     = b.get("kpis")             or {}
    slak   = (digest.get("sla_matrix") or {}).get("kpis") or {}
    r      = digest.get("resource")    or {}
    rk     = r.get("kpis")            or {}
    sf     = digest.get("smart_findings") or {}
    rf_sum = (digest.get("red_flags") or {}).get("summary") or {}
    si     = digest.get("sla_intel")   or {}
    sow_c  = digest.get("sow_contract")  or {}
    vvs    = digest.get("volume_vs_sow") or {}
    sw     = digest.get("sow_compare")   or {}

    # BUG 3 FIX: cascade through all sources for every KPI
    def _bk_get(key, *fallback_keys):
        for src in (bk, slak, sf):
            for k in (key, *fallback_keys):
                v = src.get(k)
                if v is not None:
                    return v
        return None

    total_jobs   = _bk_get("total_jobs")                           # unique jobs
    total_runs   = _bk_get("total_runs") or total_jobs              # execution count
    total_ok     = _bk_get("ok_runs")
    fail_runs    = _bk_get("fail_runs", "failed_runs", "jobs_breach")
    # Canonical compliance = ONLY window-level (wall-clock batch window vs SLA ceiling).
    # NEVER fall back to compliance_pct (job-day metric) here — job-day typically
    # runs 10-20pts higher than window compliance and would give a misleadingly
    # optimistic headline figure.  If no window data is available, leave as None.
    compliance   = _bk_get("window_compliance_pct", "batch_window_compliance")
    # Job-day compliance tracked separately — this is the per-job-day SLA adherence
    # figure that is distinct from window compliance and must NEVER overwrite it.
    job_sla_comp = _bk_get("job_sla_compliance_pct", "job_sla_compliance", "compliance_pct")
    # If no window compliance is available, fall back to job-day compliance as the
    # best available figure — but flag it clearly so the reader knows the context.
    _window_compliance_is_estimated = compliance is None and job_sla_comp is not None
    if _window_compliance_is_estimated:
        compliance = job_sla_comp  # use job-day as fallback only
    # Run-level compliance from SLA matrix (if available)
    run_sla_comp = slak.get("run_sla_compliance_pct") or slak.get("compliance_pct")
    breaches     = _bk_get("breaching_runs", "jobs_breach")
    sla_limit    = _bk_get("sla_limit_hrs", "daily_limit_hrs")
    wbd          = _int(_bk_get("window_breach_days"), 0)
    wtd          = _int(_bk_get("window_total_days"), 0)

    # -- 1. Data Volume ----------------------------------------------------
    dv_rows: List[List[str]] = []

    vol_by_year = sow_c.get("volume_by_year") or vvs.get("volume_by_year") or {}
    for yr, vol in sorted(vol_by_year.items()):
        if isinstance(vol, (int, float)) and vol > 0:
            dv_rows.append([f"Item-Locations {yr}", str(int(vol)), "—", "Planned"])
    max_il = sow_c.get("max_item_locations") or vvs.get("max_item_locations")
    if max_il:
        dv_rows.append(["Max Item-Locations (SOW)", str(int(max_il)), "—", "Contractual ceiling"])

    if sow_c.get("total_dfus"):
        dv_rows.append(["Total DFUs (SOW)", f"{int(sow_c['total_dfus']):,}", "—", "Contractual"])
    if sow_c.get("modelled_dfus"):
        dv_rows.append(["Modelled DFUs", f"{int(sow_c['modelled_dfus']):,}", "—", "Contractual"])
    if sow_c.get("total_skus"):
        dv_rows.append(["Total SKUs (SOW)", f"{int(sow_c['total_skus']):,}", "—", "Contractual"])
    if sow_c.get("planned_skus"):
        dv_rows.append(["Planned SKUs", f"{int(sow_c['planned_skus']):,}", "—", "Contractual"])

    if sw and isinstance(sw, dict):
        for dim, vals in sw.items():
            if not isinstance(vals, dict):
                continue
            sow_v = vals.get("sow") or vals.get("promised") or "NA"
            act_v = vals.get("actual") or vals.get("measured") or "NA"
            # util_pct may come from the compare endpoint, or we compute it on-the-fly
            util = vals.get("utilisation") or vals.get("utilization") or vals.get("util_pct") or vals.get("zone")
            if util is None:
                # sow_compare from manual inputs only carries {sow, actual} — compute %
                try:
                    _sow_f = float(sow_v) if sow_v not in ("NA", None) else 0.0
                    _act_f = float(act_v) if act_v not in ("NA", None) else -1.0
                    if _sow_f > 0 and _act_f >= 0:
                        _pct = round(_act_f / _sow_f * 100, 1)
                        zone = ("EXCEEDS" if _pct > 110
                                else "OPTIMAL" if _pct >= 90
                                else "AT RISK" if _pct >= 70
                                else "LOW")
                        util = f"{_pct:.1f}% ({zone})"
                except (TypeError, ValueError):
                    pass
            util_s = util if isinstance(util, str) else (f"{util:.1f}%" if isinstance(util, (int, float)) else "NA")
            dv_rows.append([str(dim), str(sow_v), str(act_v), util_s])

    if not dv_rows and sf:
        sf_vol = sf.get("volume") or sf.get("data_volume") or {}
        if isinstance(sf_vol, dict):
            for dim, vals in sf_vol.items():
                if isinstance(vals, dict):
                    dv_rows.append([
                        str(dim),
                        str(vals.get("sow", "NA")),
                        str(vals.get("actual", "NA")),
                        str(vals.get("status", "NA")),
                    ])

    if not dv_rows:
        dv_rows = [["NA — upload SOW PDF or enter DFU/SKU to populate", "NA", "NA", "NA"]]

    try:
        _fee_str = f"{float(sow_c.get('annual_fee')):,.0f}" if sow_c.get("annual_fee") else "?"
    except (TypeError, ValueError):
        _fee_str = str(sow_c.get("annual_fee", "?"))

    if sow_c:
        dv_prose = (
            f"SOW contract loaded for {sow_c.get('customer_name', customer)}, "
            f"{sow_c.get('contract_years', '?')}-year term"
            + (f", EUR {_fee_str}/yr" if _fee_str != "?" else "")
            + f". Volume ramp captured across {len(vol_by_year)} year(s). "
        )
        if sow_c.get("total_dfus"):
            dv_prose += (
                f"Total contracted DFUs: {int(sow_c['total_dfus']):,} "
                f"(Modelled: {int(sow_c.get('modelled_dfus', 0)):,}). "
            )
        if sow_c.get("total_skus"):
            dv_prose += f"Total contracted SKUs: {int(sow_c['total_skus']):,}. "
        loaded_actual = [row for row in dv_rows if row[2] not in ("NA", "—", "")]
        if loaded_actual:
            dv_prose += f"{len(loaded_actual)} dimension(s) have measured actuals for comparison."
        else:
            dv_prose += "Enter DFU/SKU actuals in the Volume Ramp panel to compare against SOW commitments."
    elif sw:
        dims = list(sw.keys())
        with_actuals = [d for d in dims if (sw[d] or {}).get("actual") not in (None, 0)]
        dv_prose = (
            f"Volume data loaded from manual inputs: {', '.join(str(d) for d in dims[:4])}. "
            + (f"{len(with_actuals)} dimension(s) have actuals entered for comparison. "
               if with_actuals else
               "SOW targets captured — enter actuals to complete the comparison. ")
            + "Upload SOW contract PDF for full contract term and volume ramp details."
        )
    else:
        dv_prose = (
            "SOW vs actual volume comparison data is not loaded. "
            "Upload SOW PDF or enter DFU/SKU values in the Volume Ramp panel to populate this section."
        )

    sections.append({
        "id": "data_volume", "title": "Data Volume Analysis",
        "prose": dv_prose,
        "table": {"headers": ["Dimension", "SOW Target", "Actual", "Status"], "rows": dv_rows},
    })

    # -- 2. Batch & SLA ----------------------------------------------------
    sla_rows: List[List[str]] = []

    # Priority order for SLA table rows:
    # 1. Production Ctrl-M breach data (top_breaches) — authoritative measured runtime
    # 2. SLA XLSX snapshot (contracts) — static test-env data, may be stale
    # Production data wins because it reflects what actually happened, not what
    # was configured in the test environment SLA spreadsheet.
    _prod_breaches = (b.get("top_breaches") or [])
    _has_prod_breach_data = any(
        _num(j.get("peak_hrs") or j.get("elapsed_hrs")) is not None
        for j in _prod_breaches
    )

    if _has_prod_breach_data:
        # Priority 1: Production Ctrl-M breach records
        for j in _prod_breaches[:6]:
            _j_peak = _num(j.get("peak_hrs") or j.get("elapsed_hrs"))
            _j_sla  = _num(j.get("sla_hrs") or j.get("sla_limit")) or _num(sla_limit)
            _j_buf  = _num(j.get("buffer_hrs"))
            if _j_buf is None and _j_peak is not None and _j_sla:
                _j_buf_s = f"{_j_sla - _j_peak:+.2f} hrs"
            elif _j_buf is not None:
                _j_buf_s = f"{_j_buf:+.2f} hrs"
            else:
                _j_buf_s = "NA"
            sla_rows.append([
                str(j.get("Job_Name") or j.get("job", "NA")),
                f"{_j_peak:.3f} hrs" if _j_peak is not None else "NA",
                f"{_j_sla:.1f} hrs" if _j_sla else "NA",
                _j_buf_s,
            ])
    else:
        # Priority 2: XLSX contracts (only when no production breach data is available)
        for c in (si.get("contracts") or [])[:6]:
            _act = _num(c.get("actual_window_hrs"))
            _sla = _num(c.get("sla_window_hrs"))
            _buf = _num(c.get("buffer_hrs"))
            # Compute buffer on-the-fly when actual runtime is known but buffer wasn't stored
            if _buf is None and _act is not None and _sla is not None:
                _buf = round(_sla - _act, 3)
            sla_rows.append([
                str(c.get("batch_name", "NA")),
                f"{_act:.3f} hrs" if _act is not None else "NA",
                f"{_sla:.2f} hrs" if _sla is not None else "NA",
                f"{_buf:+.3f} hrs" if _buf is not None else "NA",
            ])

    if not sla_rows:
        job_src = (
            (digest.get("sla_matrix") or {}).get("job_summary") or
            b.get("top_jobs") or []
        )
        for j in job_src[:6]:
            jname = j.get("Job_Name") or j.get("job_name") or "NA"
            peak  = _num(j.get("peak_hrs"))
            sla_h = _num(j.get("sla_hrs") or j.get("sla_limit")) or _num(sla_limit)
            buf   = _num(j.get("buffer_hrs"))
            if buf is None and peak is not None and sla_h:
                buf_s = f"+{sla_h - peak:.2f} hrs"
            elif buf is not None:
                buf_s = f"{float(buf):+.2f} hrs"
            else:
                buf_s = "NA"
            sla_rows.append([
                str(jname),
                f"{peak:.3f} hrs" if peak is not None else "NA",
                f"{sla_h:.1f} hrs" if sla_h else "NA",
                buf_s,
            ])

    # BUG 3 FIX: Priority 4 -- smart_findings top jobs
    if not sla_rows and sf:
        sf_jobs = sf.get("top_jobs") or sf.get("job_summary") or []
        for j in sf_jobs[:6]:
            jname = j.get("Job_Name") or j.get("job_name") or "NA"
            peak  = _num(j.get("peak_hrs") or j.get("elapsed_hrs"))
            sla_h = _num(j.get("sla_hrs") or j.get("sla_limit")) or _num(sla_limit)
            sla_rows.append([
                str(jname),
                f"{peak:.3f} hrs" if peak is not None else "NA",
                f"{sla_h:.1f} hrs" if sla_h else "NA",
                "NA",
            ])

    if not sla_rows:
        sla_rows = [["NA — upload batch CSV to populate", "NA", "NA", "NA"]]

    window_note = ""
    if wtd:
        pct_ok = round((wtd - wbd) / wtd * 100, 1) if wtd else 0
        window_note = (
            f" Batch Window Compliance: {pct_ok}% — "
            f"SLA {'met' if wbd == 0 else f'exceeded on {wbd}/{wtd}'} day(s)."
        )

    sf_findings     = sf.get("findings") or []
    regression_note = ""
    critical_count  = 0
    if sf_findings:
        regressions    = [f for f in sf_findings if "RUNTIME_REGRESSION" in str(f.get("root_cause", ""))]
        critical_count = len([f for f in sf_findings if str(f.get("severity", "")).upper() == "CRITICAL"])
        if regressions:
            reg_jobs = ", ".join(
                str(f.get("finding") or f.get("title") or "")[:50]
                for f in regressions[:2]
            )
            regression_note = f" Runtime regression detected: {reg_jobs}."

    if (total_runs or total_jobs) is not None:
        _runs_n  = int(total_runs or total_jobs or 0)
        _jobs_n  = int(total_jobs or 0)
        _fail_s  = f"{_int(fail_runs)}" if fail_runs is not None else "—"
        _comp_s  = f"{float(compliance):.1f}%" if compliance is not None else "NA"
        _jsc_s   = f"{float(job_sla_comp):.1f}%" if job_sla_comp is not None else None
        _sla_s   = f"{float(sla_limit):.1f}h" if sla_limit is not None else "not defined"
        # Show window compliance as headline (the PE sign-off metric).
        # When only job-day compliance is available, label it clearly so the reader
        # knows it is an estimate and not the authoritative window figure.
        if _window_compliance_is_estimated:
            comp_parts = f"SLA compliance (job-day estimate \u2014 no window data): {_comp_s}"
        else:
            comp_parts = f"Window SLA compliance: {_comp_s}"
            if _jsc_s and _jsc_s != _comp_s:
                comp_parts += f" \u00b7 Job-day compliance: {_jsc_s}"
        prose_b = (
            f"Batch execution analysis: {_runs_n:,} total runs · "
            f"{_jobs_n:,} unique jobs · "
            f"{comp_parts} · "
            f"{_int(breaches)} SLA breach(es) · "
            f"{_fail_s} execution failure(s) · "
            f"SLA ceiling: {_sla_s}."
            + window_note
            + regression_note
            + (f" {critical_count} critical finding(s) require resolution before PE sign-off."
               if critical_count > 0 else "")
        )
    else:
        sf_prose = sf.get("summary") or sf.get("batch_summary") or ""
        if sf_prose:
            prose_b = str(sf_prose)[:500]
        else:
            prose_b = (
                "Batch execution data is not loaded — upload Ctrl-M / batch CSVs "
                "for SLA compliance analysis. Then click Refresh Narrative."
            )

    sections.append({
        "id": "batch_sla", "title": "Batch Execution & SLA Compliance",
        "prose": prose_b,
        "table": {"headers": ["Job / Workflow", "Peak Runtime", "SLA Ceiling", "Buffer"], "rows": sla_rows},
    })

    # -- 3. Infrastructure -------------------------------------------------
    inf_rows: List[List[str]] = []
    servers  = r.get("servers") or []

    type_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for s in servers:
        stype = (s.get("type") or s.get("server_type") or "APP").upper()
        type_buckets.setdefault(stype, []).append(s)

    for stype, items in type_buckets.items():
        def _pick(srv, *fields):
            for f in fields:
                v = _num(srv.get(f))
                if v is not None:
                    return v
            return 0.0

        cpu_vals = [_pick(s, "cpu_pct", "cpu_utilisation", "cpu_usage") for s in items]
        mem_vals = [_pick(s, "mem_pct", "mem_utilisation", "mem_usage", "memory_pct") for s in items]
        cpu_avg  = sum(cpu_vals) / len(cpu_vals) if cpu_vals else 0
        cpu_peak = max(cpu_vals, default=0)
        mem_avg  = sum(mem_vals) / len(mem_vals) if mem_vals else 0
        mem_peak = max(mem_vals, default=0)
        label    = {"APP": "Application", "DB": "Database", "SRE": "SRE/Batch"}.get(stype, stype)
        inf_rows.append([f"{label} CPU",    f"{cpu_avg:.1f}%",  f"{cpu_peak:.1f}%"])
        inf_rows.append([f"{label} Memory", f"{mem_avg:.1f}%",  f"{mem_peak:.1f}%"])

    if not inf_rows:
        inf_rows = [["NA", "NA", "NA"]]

    fleet_avg_cpu = _res_cpu(rk) or 0.0
    fleet_avg_mem = _res_mem(rk) or 0.0
    fleet_grade   = rk.get("fleet_grade") or "N/A"
    fleet_score   = _num(rk.get("fleet_score")) or 0.0
    n_crit        = _int(rk.get("n_critical") or rk.get("critical_count"))
    n_warn        = _int(rk.get("n_warning") or rk.get("warning_count"))
    n_dual        = _int(rk.get("n_dual_pressure"))
    cpu_thresh    = 75.0
    hot_hosts     = [
        s.get("host") or s.get("server") or "?"
        for s in servers
        if _num(s.get("cpu_pct") or s.get("cpu_utilisation") or 0) > cpu_thresh
    ]

    if servers:
        prose_i = (
            f"Average CPU utilisation across the fleet is {fleet_avg_cpu:.1f}% "
            f"and memory is {fleet_avg_mem:.1f}%, with "
            + (f"{len(hot_hosts)} host(s) exceeding the {cpu_thresh:.0f}% threshold "
               f"({', '.join(hot_hosts[:3])})."
               if hot_hosts
               else f"no host exceeding the {cpu_thresh:.0f}% utilisation threshold.")
            + f" Fleet has {n_crit} critical and {n_warn} warning server(s)."
            + (f" {n_dual} host(s) show simultaneous CPU + memory pressure." if n_dual else "")
            + (f" Fleet grade: {fleet_grade} (score: {fleet_score:.1f})." if fleet_grade != "N/A" else "")
            + (
                # PE sign-off implication: explain what the grade means for audit approval
                f" Grade {fleet_grade} ({fleet_score:.1f}/100) is below the PE approval threshold of 70 — "
                "infrastructure must be remediated or the customer must formally acknowledge and "
                "sign off the risk before PE can proceed to go-live approval."
                if fleet_grade not in ("N/A", None) and 0 < fleet_score < 70
                else (
                    f" Grade {fleet_grade} ({fleet_score:.1f}/100) meets the PE approval threshold of 70 — "
                    "infrastructure health is acceptable for sign-off."
                    if fleet_grade not in ("N/A", None) and fleet_score >= 70
                    else ""
                )
            )
        )
    else:
        prose_i = (
            "Resource utilisation data is not loaded — upload resource report "
            "(DOCX/PDF) for infrastructure health analysis."
        )

    sections.append({
        "id": "infrastructure", "title": "Infrastructure Utilisation & Resource Health",
        "prose": prose_i,
        "table": {"headers": ["Resource Type", "Avg Utilisation", "Peak Utilisation"], "rows": inf_rows},
    })

    # -- 4. UAT ------------------------------------------------------------
    uat_ac = digest.get("uat") or []
    if uat_ac:
        uat_rows: List[List[str]] = []
        uat_by_cat: Dict[str, List] = {}
        for item in uat_ac:
            cat = item.get("category") or item.get("test_category") or "General"
            uat_by_cat.setdefault(cat, []).append(item)
        for cat, items in uat_by_cat.items():
            total  = len(items)
            passed = len([i for i in items if str(i.get("status", "")).upper() in ("PASS", "OK", "PASSED")])
            uat_rows.append([cat, f"{passed}/{total} passed"])
        uat_prose = (
            f"UAT validation: {len(uat_ac)} test case(s) across {len(uat_by_cat)} category(s). "
            + "Pass rate: "
            + ", ".join(f"{row[0]}: {row[1]}" for row in uat_rows[:4])
            + "."
        )
        uat_tbl: Dict[str, Any] = {"headers": ["Test Category", "Test Cases"], "rows": uat_rows}
    else:
        uat_prose = (
            "UAT validation artefacts were not provided in this audit run. "
            "Upload UAT test results for sign-off coverage analysis."
        )
        uat_tbl = {
            "headers": ["Test Category", "Test Cases"],
            "rows":    [["NA — UAT artefacts not loaded", "NA"]],
        }

    sections.append({
        "id": "uat", "title": "User Acceptance Testing (UAT) Validation",
        "prose": uat_prose,
        "table": uat_tbl,
    })

    # -- Verdict -----------------------------------------------------------
    verdict  = "CONDITIONAL"
    rf_crit  = _int(rf_sum.get("critical") or rf_sum.get("CRITICAL"))
    _comp_n  = _num(compliance)

    if _comp_n is not None and _comp_n >= 98 and rf_crit == 0:
        verdict = "APPROVED"
    elif rf_crit > 0 or (_comp_n is not None and _comp_n < 90):
        verdict = "BLOCKED"

    try:
        sf_verdict = (sf.get("verdict") or {}).get("decision", "")
        _vrank = {"BLOCKED": 2, "CONDITIONAL": 1, "APPROVED": 0, "PENDING": -1}
        if _vrank.get(sf_verdict, -1) > _vrank.get(verdict, -1):
            verdict = sf_verdict
    except Exception:
        pass

    # -- Cross-pillar diagnosis -------------------------------------------
    # Determine the audit scenario label and a one-sentence diagnosis.
    # This fires ONLY when both batch and resource data are loaded and is
    # completely data-driven — no hardcoded thresholds.
    _cp_batch_loaded    = bool(bk)
    _cp_resource_loaded = bool(rk and servers)
    _cp_breach          = _int(bk.get("jobs_breach") or bk.get("breaching_runs") or 0)
    _cp_atrisk          = _int(bk.get("jobs_at_risk") or bk.get("at_risk_runs") or 0)
    _cp_crit_srv        = _int(rk.get("n_critical") or 0)
    _cp_warn_srv        = _int(rk.get("n_warning") or 0)
    # suppress the scenario when all resource metrics are 0 (image-only docx)
    _cp_all_zero_res    = all(
        _num(s.get("cpu_pct") or s.get("cpu_utilisation") or 0) == 0
        and _num(s.get("mem_pct") or s.get("mem_utilisation") or 0) == 0
        for s in servers
    ) if servers else True
    _cp_diagnosis: str = ""

    if _cp_batch_loaded and _cp_resource_loaded and not _cp_all_zero_res:
        if _cp_breach == 0 and _cp_crit_srv == 0:
            _cp_diagnosis = (
                "Scenario: BATCH + RESOURCE — both pillars healthy. "
                "All jobs within SLA and no critical infrastructure pressure detected."
            )
        elif _cp_breach > 0 and _cp_crit_srv == 0 and _cp_warn_srv <= 1:
            _cp_diagnosis = (
                f"Scenario: SCHEDULING ISSUE — {_cp_breach} SLA breach(es) with healthy fleet "
                f"(grade {fleet_grade}, 0 critical servers). "
                "Root cause is scheduling logic or SQL regression, not hardware capacity."
            )
        elif _cp_breach == 0 and _cp_crit_srv > 0:
            _cp_diagnosis = (
                f"Scenario: HIDDEN INFRA RISK — batch currently within SLA but "
                f"{_cp_crit_srv} server(s) are at critical state (grade {fleet_grade}). "
                "Infrastructure must be remediated before sign-off."
            )
        elif _cp_breach > 0 and _cp_crit_srv > 0:
            _cp_diagnosis = (
                f"Scenario: COMPOUND RISK — {_cp_breach} SLA breach(es) AND "
                f"{_cp_crit_srv} critical server(s) simultaneously. "
                "Two parallel workstreams required: infrastructure scale-up + job optimisation."
            )
    elif _cp_batch_loaded and not _cp_resource_loaded:
        if _cp_breach > 0:
            _cp_diagnosis = (
                f"Scenario: BATCH-ONLY — {_cp_breach} SLA breach(es) with no resource data available. "
                "Root cause cannot be confirmed without infrastructure evidence."
            )
        else:
            _cp_diagnosis = (
                "Scenario: BATCH-ONLY — no SLA breaches. "
                "Upload resource report to validate infrastructure health."
            )
    elif not _cp_batch_loaded and _cp_resource_loaded:
        _cp_diagnosis = (
            f"Scenario: RESOURCE-ONLY — fleet grade {fleet_grade}. "
            "Upload Ctrl-M batch CSV for SLA compliance analysis."
        )

    # -- Summary -----------------------------------------------------------
    parts = [f"Completed the PE Review for {customer or 'the account'}."]
    if _comp_n is not None:
        parts.append(f"Batch SLA compliance: {_comp_n:.1f}%.")
    if wbd and wtd:
        parts.append(
            f"Batch Window Compliance: {round((wtd - wbd)/wtd*100, 1)}% "
            f"— SLA exceeded on {wbd}/{wtd} day(s)."
        )
    if fleet_grade and fleet_grade != "N/A":
        parts.append(
            f"Fleet grade: {fleet_grade}"
            + (f" ({n_crit} critical server(s))" if n_crit else "") + "."
        )
    if _cp_diagnosis:
        parts.append(_cp_diagnosis)
    if rf_crit:
        parts.append(
            f"{rf_crit} critical finding(s) require immediate attention before PE sign-off."
        )
    parts.append(
        f"Overall verdict: {verdict}. Sections below contain the full evidence breakdown."
    )

    return {
        "verdict":  verdict,
        "summary":  " ".join(parts),
        "sections": sections,
        "model":    "deterministic",
        "customer": customer,
    }


# ---------------------------------------------------------------------------
# _validate_and_merge
# ---------------------------------------------------------------------------

def _validate_and_merge(ai_payload: Any, fallback: Dict[str, Any],
                        digest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(ai_payload, dict):
        return fallback

    out = dict(fallback)
    ai_verdict  = (ai_payload.get("verdict") or "").upper()
    det_verdict = fallback.get("verdict", "CONDITIONAL")

    if digest:
        bk     = (digest.get("batch")      or {}).get("kpis") or {}
        slak   = (digest.get("sla_matrix") or {}).get("kpis") or {}
        rk     = (digest.get("resource")   or {}).get("kpis") or {}
        rf_sum = (digest.get("red_flags")  or {}).get("summary") or {}
        sf     = digest.get("smart_findings") or {}

        comp       = _num(bk.get("compliance_pct") or slak.get("compliance_pct"))
        n_breach   = _int(bk.get("jobs_breach") or slak.get("breaching_runs"))
        rf_crit    = _int(rf_sum.get("critical") or rf_sum.get("CRITICAL"))
        n_crit_srv = _int(rk.get("n_critical"))
        sf_crit    = _int((sf.get("verdict") or {}).get("critical_count"))

        has_blockers = (
            n_breach > 0 or rf_crit > 0 or sf_crit > 0
            or n_crit_srv >= 2
            or (comp is not None and comp < 85)
        )
        has_warnings = (
            (comp is not None and comp < 95)
            or _int(rk.get("n_warning")) > 0
        )

        kpi_verdict = "BLOCKED" if has_blockers else ("CONDITIONAL" if has_warnings else "APPROVED")
        _vrank = {"BLOCKED": 2, "CONDITIONAL": 1, "APPROVED": 0}
        final = max(
            [(ai_verdict,  _vrank.get(ai_verdict,  -1)),
             (det_verdict, _vrank.get(det_verdict, -1)),
             (kpi_verdict, _vrank.get(kpi_verdict, -1))],
            key=lambda x: x[1],
        )[0]
        out["verdict"] = final or det_verdict
    else:
        _vrank = {"BLOCKED": 2, "CONDITIONAL": 1, "APPROVED": 0}
        out["verdict"] = (
            ai_verdict
            if _vrank.get(ai_verdict, -1) >= _vrank.get(det_verdict, -1)
            else det_verdict
        )

    if isinstance(ai_payload.get("summary"), str) and ai_payload["summary"].strip():
        out["summary"] = ai_payload["summary"].strip()

    ai_secs = ai_payload.get("sections") or []
    by_id   = {s.get("id"): s for s in ai_secs if isinstance(s, dict)}
    merged: List[Dict[str, Any]] = []
    for proto in _SECTIONS:
        sid    = proto["id"]
        ai_sec = by_id.get(sid)
        fb_sec = next((s for s in fallback["sections"] if s["id"] == sid), None)

        if ai_sec and isinstance(ai_sec, dict):
            tbl     = ai_sec.get("table") or {}
            headers = tbl.get("headers") or proto["default_table"]["headers"]
            rows    = tbl.get("rows")    or proto["default_table"]["rows"]

            if fb_sec:
                fb_rows = (fb_sec.get("table") or {}).get("rows") or []
                fb_has_data = fb_rows and not all(
                    all(str(c).strip().upper() in ("NA", "") for c in row)
                    for row in fb_rows
                )
                if fb_has_data:
                    headers = (fb_sec.get("table") or {}).get("headers") or headers
                    rows    = fb_rows

            merged.append({
                "id":    sid,
                "title": ai_sec.get("title") or proto["title"],
                "prose": ai_sec.get("prose") or (fb_sec.get("prose") if fb_sec else ""),
                "table": {"headers": headers, "rows": rows},
            })
        elif fb_sec:
            merged.append(fb_sec)

    out["sections"] = merged
    return out


def _bare_fallback(customer: str) -> Dict[str, Any]:
    return {
        "verdict": "CONDITIONAL",
        "summary": (
            f"Completed the PE Review for {customer or 'the account'}. "
            "Analysis data could not be fully compiled. "
            "Please verify all data sources are uploaded and recalculate."
        ),
        "sections": [
            {
                "id":    s["id"],
                "title": s["title"],
                "prose": "Data unavailable — please re-upload source files and recalculate.",
                "table": s["default_table"],
            }
            for s in _SECTIONS
        ],
        "model":    "deterministic",
        "customer": customer,
    }


# ---------------------------------------------------------------------------
# BUG 2 FIX: _pe_narrative_inner() -- hydration no-op when sc_batch=={} fixed
# ---------------------------------------------------------------------------

@router.get("/pe-narrative-debug", include_in_schema=False)
async def pe_narrative_debug() -> Dict[str, Any]:
    """Temporary debug endpoint — returns cache snapshot without building prose."""
    from services import session_cache
    ac = session_cache.ac_snapshot()
    return {
        "ac_batch_kpis_present": bool(ac.get("batch_kpis")),
        "ac_resource_summary_present": bool(ac.get("resource_summary")),
        "ac_sla_matrix_kpis_present": bool(ac.get("sla_matrix_kpis")),
        "ac_adaptive_sla_type": type(ac.get("adaptive_sla")).__name__,
        "last_batch_kpis_present": bool((session_cache.get("last_batch") or {}).get("kpis")),
        "last_resource_servers": len((session_cache.get("last_resource") or {}).get("servers") or []),
        "last_sla_matrix_compliance": (session_cache.get("last_sla_matrix") or {}).get("compliance_pct"),
        "customer_name": ac.get("customer_name"),
    }


@router.post(
    "/pe-narrative",
    summary="Structured 4-section PE review narrative (Data Volume / Batch SLA / Infrastructure / UAT)",
)
async def pe_narrative(body: PeNarrativeRequest) -> Dict[str, Any]:
    customer = ""
    try:
        customer = (body.model_dump(exclude_none=True).get("customer_name") or "")
    except Exception:
        pass
    try:
        return await _pe_narrative_inner(body, customer)
    except Exception as exc:
        log.error("pe_narrative: top-level error (%s)", exc, exc_info=True)
        return _bare_fallback(customer)


async def _pe_narrative_inner(body: PeNarrativeRequest, customer: str) -> Dict[str, Any]:
    from services import session_cache

    payload = body.model_dump(exclude_none=True)

    # -- Hydrate from session_cache ----------------------------------------
    try:
        sc_batch    = session_cache.get("last_batch")      or {}
        sc_resource = session_cache.get("last_resource")   or {}
        sc_sla      = session_cache.get("last_sla_matrix") or {}
        sc_rf       = session_cache.get("last_red_flags")  or {}

        # BUG 2 FIX: only override when the source actually has useful kpis
        if sc_batch and (sc_batch.get("kpis") or sc_batch.get("top_jobs")):
            payload["batch"] = sc_batch
        elif not payload.get("batch") and sc_batch:
            payload["batch"] = sc_batch
        # (if both are empty, leave payload["batch"] absent -- _digest will use smart_findings)

        req_res  = payload.get("resource") or {}
        req_srvs = req_res.get("servers") or []
        sc_srvs  = sc_resource.get("servers") or []
        req_has_cpu = any(s.get("cpu_pct") is not None for s in req_srvs[:3])
        sc_has_cpu  = any(s.get("cpu_pct") is not None for s in sc_srvs[:3])
        if sc_has_cpu and not req_has_cpu:
            payload["resource"] = sc_resource
        elif sc_resource and not req_res:
            payload["resource"] = sc_resource
        else:
            payload.setdefault("resource", sc_resource)

        if sc_sla and not payload.get("sla_matrix"):
            payload["sla_matrix"] = sc_sla

        if sc_rf and not payload.get("red_flags"):
            payload["red_flags"] = sc_rf

        if payload.get("sow_compare"):
            session_cache.set("last_sow_compare", payload["sow_compare"])
        elif body.sow_compare is None:
            # Frontend explicitly cleared the field — erase in-memory copy
            session_cache.set("last_sow_compare", None)

    except Exception as exc:
        log.warning("pe_narrative: session_cache hydration failed (%s)", exc)

    try:
        if not payload.get("sla_intel"):
            from services import config_store
            payload["sla_intel"] = config_store.get("_sla_intelligence") or {}
    except Exception as exc:
        log.warning("pe_narrative: sla_intel hydration failed (%s)", exc)

    customer = payload.get("customer_name") or customer

    log.info(
        "pe_narrative: batch_kpis=%s res_servers=%d sla_comp=%s ac_batch=%s sf=%s",
        "present" if (payload.get("batch") or {}).get("kpis") else "MISSING",
        len((payload.get("resource") or {}).get("servers") or []),
        (payload.get("sla_matrix") or {}).get("compliance_pct"),
        "present" if session_cache.ac_get("batch_kpis") else "MISSING",
        "present" if session_cache.get("last_smart_findings") else "MISSING",
    )

    try:
        digest = _build_narrative_context(payload)
    except Exception as exc:
        log.warning("pe_narrative: _digest failed (%s)", exc)
        digest = {"customer_name": customer}

    log.info(
        "pe_narrative digest sections: %s",
        [k for k in ("batch", "sla_matrix", "resource", "sow_compare", "red_flags", "smart_findings")
         if digest.get(k)],
    )

    try:
        fallback = _deterministic_fallback(digest, customer)
    except Exception as exc:
        log.warning("pe_narrative: _deterministic_fallback failed (%s)", exc)
        fallback = _bare_fallback(customer)

    return fallback
