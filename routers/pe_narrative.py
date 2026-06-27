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

from services import judgment_engine

log = logging.getLogger("pe_dashboard.pe_narrative")
router = APIRouter()


class PeNarrativeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    batch:         Optional[Dict[str, Any]] = None
    resource:      Optional[Dict[str, Any]] = None
    sla_matrix:    Optional[Dict[str, Any]] = None
    sla_triage:    Optional[Dict[str, Any]] = None
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
            "Average + peak CPU / memory / disk per server role, each shown against "
            "the role-aware governing threshold (APP/DB/SRE differ; DB memory 80-92% "
            "is the expected SGA/PGA band). Flag any host whose peak exceeds its role ceiling."
        ),
        "default_table": {
            "headers": ["Resource Type", "Avg", "Peak", "Governing Threshold", "Status"],
            "rows":    [["NA", "NA", "NA", "NA", "NA"]],
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


def _fmt_pct(v: Any) -> str:
    n = _num(v)
    return "NA" if n is None else f"{n:.1f}%"


def _fmt_hrs(v: Any) -> str:
    n = _num(v)
    return "NA" if n is None else f"{n:.2f}h"


def _build_evidence_facts(digest: Dict[str, Any]) -> Dict[str, Any]:
    """Named evidence facts passed to AI and reused for deterministic reasons."""
    bk = (digest.get("batch") or {}).get("kpis") or {}
    slak = (digest.get("sla_matrix") or {}).get("kpis") or {}
    _res_digest = digest.get("resource") or {}
    rk = _res_digest.get("kpis") or _res_digest or {}
    rf_sum = (digest.get("red_flags") or {}).get("summary") or {}
    rule_f = digest.get("rule_findings") or {}
    bm = digest.get("benchmark") or {}
    sf = digest.get("smart_findings") or {}

    def _first(*vals):
        for val in vals:
            if val is not None:
                return val
        return None

    dc = (digest.get("batch") or {}).get("deadline_compliance") or bk.get("deadline_compliance") or {}
    # Day-level window compliance DERIVED from the same breach/total days emitted below,
    # so the verdict reason's % reconciles exactly with its "{breach}/{total}" fraction.
    _wbd_fact = _first(bk.get("window_breach_days"), slak.get("window_breach_days"))
    _wtd_fact = _first(bk.get("window_total_days"), slak.get("window_total_days"))
    _day_comp_fact = None
    try:
        _wtd_i = int(_wtd_fact) if _wtd_fact is not None else 0
        _wbd_i = int(_wbd_fact) if _wbd_fact is not None else 0
        if _wtd_i > 0:
            _day_comp_fact = round((_wtd_i - _wbd_i) / _wtd_i * 100, 1)
    except (TypeError, ValueError):
        _day_comp_fact = None
    facts: Dict[str, Any] = {
        "batch_window_compliance_pct": _first(
            _day_comp_fact,
            bk.get("window_day_compliance_pct"),
            slak.get("window_day_compliance_pct"),
            bk.get("window_compliance_pct"),
            bk.get("batch_window_compliance"),
            slak.get("window_compliance_pct"),
            slak.get("batch_window_compliance"),
        ),
        # Pair-level ((sub_app × day) window) compliance — secondary detail only, kept
        # distinct so it is never confused with the canonical day-level headline above.
        "window_pair_compliance_pct": _first(
            bk.get("window_compliance_pct"),
            bk.get("batch_window_compliance"),
            slak.get("window_compliance_pct"),
            slak.get("batch_window_compliance"),
        ),
        "window_breach_days": _wbd_fact,
        "window_total_days": _wtd_fact,
        "job_sla_compliance_pct": _first(
            bk.get("job_sla_compliance_pct"),
            bk.get("job_sla_compliance"),
            bk.get("compliance_pct"),
            slak.get("run_sla_compliance_pct"),
            slak.get("compliance_pct"),
        ),
        "fleet_grade": rk.get("fleet_grade"),
        "fleet_score": rk.get("fleet_score"),
        "peak_mem_pct": _first(rk.get("peak_mem_pct"), rk.get("max_mem_pct"), rk.get("mem_peak_pct")),
        "avg_cpu": _first(rk.get("avg_cpu"), rk.get("avg_cpu_pct"), rk.get("cpu_avg")),
        "n_critical_findings": (
            _int(rf_sum.get("critical") or rf_sum.get("CRITICAL"))
            + len(rule_f.get("critical") or [])
        ),
        "regression_count": _first(
            bk.get("regression_count"),
            bk.get("runtime_regression_count"),
            sf.get("regression_count"),
        ),
        "net_runtime_delta": _first(
            bk.get("net_runtime_delta"),
            bk.get("net_runtime_delta_pct"),
            sf.get("net_runtime_delta"),
        ),
        "benchmark_breach_count": len([
            r for r in (bm.get("rows") or []) if str(r.get("status")) in ("BREACH", "RED")
        ]),
    }
    if isinstance(dc, dict) and dc.get("has_deadlines"):
        facts.update({
            "deadline_compliance_pct": dc.get("compliance_pct"),
            "deadline_breach_days": dc.get("breach_days") or dc.get("breach_windows"),
            "worst_deadline_overrun_hrs": dc.get("worst_overrun_hrs"),
            "deadline_assessable_windows": dc.get("assessable_windows"),
        })
    return {k: v for k, v in facts.items() if v is not None}


def _evidence_facts_block(facts: Dict[str, Any]) -> str:
    if not facts:
        return "evidence_available: false"
    lines = []
    for key in sorted(facts):
        lines.append(f"{key}: {facts[key]}")
    return "\n".join(lines)


def _build_verdict_reason(verdict: str, facts: Dict[str, Any]) -> str:
    verdict_txt = (verdict or "CONDITIONAL").upper()
    drivers: List[str] = []

    window_comp = _num(facts.get("batch_window_compliance_pct"))
    window_breach_days = _int(facts.get("window_breach_days"), 0)
    window_total_days = _int(facts.get("window_total_days"), 0)
    if window_comp is not None and window_comp < 90:
        # window_comp is the DAY-LEVEL figure, so the clean/total day fraction below
        # reconciles arithmetically with it (e.g. 2/28 days == 7.1%).
        if window_total_days:
            clean_days = window_total_days - window_breach_days
            fact = (
                f"batch finished within its SLA window on only {clean_days}/{window_total_days} "
                f"day(s) ({window_comp:.1f}%, < 90% floor)"
            )
        else:
            fact = f"batch-window SLA compliance {window_comp:.1f}% (< 90% floor)"
        drivers.append(fact)

    job_comp = _num(facts.get("job_sla_compliance_pct"))
    if job_comp is not None and job_comp < 90:
        drivers.append(f"job-level SLA compliance {job_comp:.1f}% (< 90% floor)")

    deadline_comp = _num(facts.get("deadline_compliance_pct"))
    deadline_breach_days = _int(facts.get("deadline_breach_days"), 0)
    if deadline_comp is not None and deadline_breach_days > 0:
        drivers.append(
            f"wall-clock deadline compliance {deadline_comp:.1f}% "
            f"({deadline_breach_days} breach days, worst overrun "
            f"{_fmt_hrs(facts.get('worst_deadline_overrun_hrs'))})"
        )

    n_critical = _int(facts.get("n_critical_findings"), 0)
    if n_critical > 0:
        drivers.append(f"{n_critical} critical finding(s)")

    bench_breach = _int(facts.get("benchmark_breach_count"), 0)
    if bench_breach > 0:
        drivers.append(f"{bench_breach} UAT benchmark breach transaction(s)")

    if not drivers:
        if verdict_txt == "APPROVED":
            drivers.append("batch, resource, findings, and UAT evidence within approval thresholds")
        else:
            drivers.append("approval criteria not fully met across loaded evidence")

    despite_parts: List[str] = []
    if facts.get("peak_mem_pct") is not None:
        despite_parts.append(f"DB mem peak {_fmt_pct(facts.get('peak_mem_pct'))}")
    if facts.get("avg_cpu") is not None:
        despite_parts.append(f"avg CPU {_fmt_pct(facts.get('avg_cpu'))}")
    if facts.get("fleet_grade"):
        fleet_txt = f"fleet grade {facts.get('fleet_grade')}"
        if facts.get("fleet_score") is not None:
            fleet_txt += f" ({_num(facts.get('fleet_score')):.1f})"
        despite_parts.append(fleet_txt)
    despite = ""
    fleet_score_for_despite = _num(facts.get("fleet_score"))
    if verdict_txt == "BLOCKED" and despite_parts and (
        fleet_score_for_despite is None or fleet_score_for_despite >= 70
    ):
        despite = f", despite healthy resource metrics ({', '.join(despite_parts)})"

    return f"{verdict_txt} — driven by {' and '.join(drivers)}{despite}"


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
            "deadline_compliance": b.get("deadline_compliance") or bk.get("deadline_compliance"),
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

    triage = payload.get("sla_triage") or {}
    if triage:
        out["sla_triage"] = triage

    # -- Red flags / benchmark ---------------------------------------------
    # Pass the FULL benchmark object (not just summary) so the UAT section can
    # render transaction-level evidence. Fall back to session_cache last_benchmark.
    bm = payload.get("benchmark") or session_cache.get("last_benchmark") or {}
    if bm and isinstance(bm, dict):
        out["benchmark"] = {
            "filename":           bm.get("filename"),
            "total_transactions": bm.get("total_transactions"),
            "degraded":           bm.get("degraded"),
            "improved":           bm.get("improved"),
            "sla_breaches":       bm.get("sla_breaches"),
            "avg_delta_pct":      bm.get("avg_delta_pct"),
            "threshold_pct":      bm.get("threshold_pct"),
            "summary":            bm.get("summary"),
            "rows": [
                {k: r.get(k) for k in (
                    "transaction", "action", "current_sec", "baseline_sec",
                    "sla_sec", "delta_pct", "status", "records", "concurrent_users",
                ) if r.get(k) is not None}
                for r in (bm.get("rows") or [])[:40]
            ],
            "evidence_sentences": (bm.get("evidence_sentences") or [])[:10],
            "coverage_summary":   bm.get("coverage_summary"),
            # Batch-runtime regression rollup (regressions / comparable / improvements)
            # so the narrative can build a clean "X of Y slower, Z improved" line
            # instead of slicing raw finding titles mid-word.
            "batch_perf_summary": bm.get("batch_perf_summary"),
        }
    rf = payload.get("red_flags") or session_cache.get("last_red_flags") or {}
    if rf:
        out["red_flags"] = {"summary": rf.get("summary") or rf}

    # -- Rule-engine findings (deterministic criticality evidence) ----------
    # The /api/generate-findings response — its critical/warning counts must
    # influence the narrative verdict so both panels agree.
    fnd = payload.get("findings") or {}
    if isinstance(fnd, dict) and fnd.get("findings"):
        _fl = fnd["findings"]
        out["rule_findings"] = {
            "critical": [
                {"text": f.get("text"), "source": f.get("source")}
                for f in _fl if str(f.get("severity", "")).lower() == "critical"
            ][:10],
            "warning_count": len([f for f in _fl if str(f.get("severity", "")).lower() == "warning"]),
            "total": len(_fl),
        }

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
    triage = digest.get("sla_triage") or {}
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
    # Canonical headline compliance = DAY-LEVEL window compliance: the share of
    # calendar days the batch finished inside its SLA window. This is the strictest,
    # most honest PE sign-off figure and it reconciles arithmetically with the
    # "{breach}/{total} day(s)" fraction shown beside it. Pair-level ((sub_app × day)
    # window) compliance is kept ONLY as a clearly-labeled secondary detail.
    # Canonical headline = DAY-LEVEL window compliance, DERIVED from the breach/total
    # days so it ALWAYS reconciles with the "{breach}/{total} day(s)" fraction shown
    # beside it — even after the SLA Matrix overwrites those counts. Falls back to the
    # pre-computed field only when day counts are unavailable.
    _wbd0 = _int(_bk_get("window_breach_days"), 0)
    _wtd0 = _int(_bk_get("window_total_days"), 0)
    if _wtd0 > 0:
        compliance = round((_wtd0 - _wbd0) / _wtd0 * 100, 1)
    else:
        compliance = _bk_get("window_day_compliance_pct")
    # Pair-level window compliance — secondary detail, NEVER the headline. Matches the
    # SLA Matrix tab's (sub_app, date) granularity; typically reads higher than the
    # day-level figure because one clean sub-app on a breached day still counts.
    window_pair_compliance  = _bk_get("window_compliance_pct", "batch_window_compliance")
    # Job-day compliance tracked separately — per-job-day SLA adherence, distinct from
    # window compliance and must NEVER overwrite it (usually 10-20pts more optimistic).
    job_sla_comp = _bk_get("job_sla_compliance_pct", "job_sla_compliance", "compliance_pct")
    # Fallbacks when no day-level window data exists: prefer pair-level (still a window
    # metric), then job-day (flagged as estimated so the reader knows the context).
    _window_compliance_is_estimated = False
    if compliance is None:
        if window_pair_compliance is not None:
            compliance = window_pair_compliance
        elif job_sla_comp is not None:
            compliance = job_sla_comp
            _window_compliance_is_estimated = True
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
        # Shape A (canonical): {"metrics": [{key,label,sow,actual,pct,status}, ...]}
        # — produced by /api/sow/compare and the manual SOW entry form.
        _sw_metrics = sw.get("metrics")
        if isinstance(_sw_metrics, list):
            for m in _sw_metrics:
                if not isinstance(m, dict):
                    continue
                label  = m.get("label") or m.get("key") or "Metric"
                sow_v  = m.get("sow")
                act_v  = m.get("actual")
                pct    = m.get("pct")
                status = m.get("status") or m.get("zone")
                if pct is None:
                    try:
                        _sow_f = float(sow_v or 0)
                        _act_f = float(act_v) if act_v is not None else -1.0
                        if _sow_f > 0 and _act_f >= 0:
                            pct = round(_act_f / _sow_f * 100, 1)
                    except (TypeError, ValueError):
                        pct = None
                if status is None and pct is not None:
                    status = ("HIGH" if pct > 110
                              else "OPTIMAL" if pct >= 90
                              else "ACCEPTABLE" if pct >= 70
                              else "LOW")
                util_s = (f"{pct:.1f}% ({status})" if pct is not None and status
                          else str(status) if status else "Target only — no actual")
                _fmt_n = lambda v: f"{float(v):,.0f}" if isinstance(v, (int, float)) else str(v if v is not None else "NA")
                dv_rows.append([str(label), _fmt_n(sow_v), _fmt_n(act_v) if act_v is not None else "—", util_s])
        else:
            # Shape B (legacy): {dim: {sow, actual, ...}}
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
        _m = sw.get("metrics") if isinstance(sw.get("metrics"), list) else None
        if _m:
            dims = [str(x.get("label") or x.get("key") or "?") for x in _m]
            with_actuals = [x for x in _m if x.get("actual") not in (None, 0)]
        else:
            dims = [str(d) for d in sw.keys()]
            with_actuals = [d for d in dims if isinstance(sw.get(d), dict) and (sw[d] or {}).get("actual") not in (None, 0)]
        dv_prose = (
            f"Volume data loaded from manual inputs: {', '.join(dims[:5])}. "
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

    # Provenance flag — is the SOW/volume data parsed from an uploaded contract
    # PDF (contractual, higher trust) or typed in by hand (manual, unverified)?
    # Surfaced as a visible badge so a reviewer knows at a glance which numbers to
    # trust before sign-off — a manual figure can carry a units error or a rounded
    # placeholder typed over a precise value. Mirrors the evidence_class provenance
    # concept the findings engine already uses, applied to the Data Volume panel.
    if sow_c:
        _dv_prov = {
            "source": "sow_pdf",
            "label": "SOW PDF — contractual",
            "tone": "ok",
            "note": ("Volume targets parsed from the uploaded SOW contract — "
                     "contractual and traceable to the source document."),
        }
    elif sw:
        _dv_prov = {
            "source": "manual",
            "label": "Manual input — unverified",
            "tone": "warn",
            "note": ("Volume targets were entered by hand, not parsed from a source "
                     "document. Confirm against the signed SOW before sign-off — a "
                     "manual value can carry a units or typo error."),
        }
    else:
        _dv_prov = {
            "source": "none",
            "label": "Not loaded",
            "tone": "muted",
            "note": "No SOW contract or manual volume figures loaded yet.",
        }

    sections.append({
        "id": "data_volume", "title": "Data Volume Analysis",
        "prose": dv_prose,
        "provenance": _dv_prov,
        "table": {"headers": ["Dimension", "SOW Target", "Actual", "Status"], "rows": dv_rows},
    })

    # -- 2. Batch & SLA ----------------------------------------------------
    sla_rows: List[List[str]] = []
    # Header + caption travel with the rows so the frontend labels the table for
    # whichever evidence source actually populated it (what-failed vs reference).
    sla_headers: List[str] = ["Job / Workflow", "Peak Runtime", "SLA Ceiling", "Buffer"]
    table_caption: str = ""

    # Priority 0: WINDOW-BREACH DAYS — the evidence that matches the binding
    # (window-day) compliance metric. A panel about a failing batch must lead with
    # the days the whole batch missed its wall-clock window, not the healthiest
    # jobs. Overrun is kept honest/positive by only listing days whose runtime
    # actually exceeded the ceiling.
    _win_recs = b.get("window") or []
    if sla_limit:
        _ceil = float(sla_limit)
        _breach_rows = []
        for w in _win_recs:
            if not w.get("breach"):
                continue
            _rt = _num(w.get("elapsed_hrs")) or 0.0
            if _rt <= 0:
                _rt = _num(w.get("total_hrs")) or 0.0
            if _rt <= _ceil:
                continue
            _breach_rows.append((w, _rt, _rt - _ceil))
        _breach_rows.sort(key=lambda t: t[2], reverse=True)
        if _breach_rows:
            for w, _rt, _ov in _breach_rows[:6]:
                _fc = _int(w.get("fail_count"), 0)
                _lbl = str(w.get("run_date", "NA")) + (f"  ·  {_fc} job fail" if _fc else "")
                sla_rows.append([_lbl, f"{_rt:.2f} hrs", f"{_ceil:.1f} hrs", f"+{_ov:.2f} hrs"])
            sla_headers = ["Breach Date", "Window Runtime", "SLA Ceiling", "Overrun"]
            table_caption = (
                f"Days the batch missed its SLA window — worst overrun first "
                f"({len(_breach_rows)} day(s) breached)."
            )

    # Priority order for the remaining SLA table rows:
    # 1. Production Ctrl-M breach data (top_breaches) — authoritative measured runtime
    # 2. SLA XLSX snapshot (contracts) — static test-env data, may be stale
    # Production data wins because it reflects what actually happened, not what
    # was configured in the test environment SLA spreadsheet.
    _prod_breaches = (b.get("top_breaches") or [])
    _has_prod_breach_data = any(
        _num(j.get("peak_hrs") or j.get("elapsed_hrs")) is not None
        for j in _prod_breaches
    )

    if not sla_rows and _has_prod_breach_data:
        # Priority 1: Production Ctrl-M breach records (worst job-ceiling breaches)
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
        if sla_rows:
            table_caption = table_caption or "Worst job-ceiling breaches — biggest overrun first."
    elif not sla_rows:
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
        if sla_rows:
            # These are the longest-running jobs, NOT breaches — label them so the
            # table can never be misread as "all green = compliant" when the window
            # may still be failing (that verdict lives in the panel above).
            sla_headers = ["Longest Jobs (reference)", "Peak Runtime", "SLA Ceiling", "Buffer"]
            table_caption = (
                "No window or job-ceiling breaches in scope — showing the longest-running "
                "jobs for reference; all are within their ceiling."
            )

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
        # The `compliance` headline above already states the day-level window % — here
        # we add the breach-day fraction (which reconciles with it) plus the pair-level
        # figure as an explicitly labeled secondary, never juxtaposed as one number.
        _pair_n = _num(window_pair_compliance)
        _pair_detail = ""
        if _pair_n is not None and compliance is not None and abs(_pair_n - float(compliance)) > 0.1:
            _pair_detail = f" (per sub-app \u00d7 day window: {_pair_n:.1f}%)"
        window_note = (
            f" Batch made its window on {wtd - wbd}/{wtd} day(s)"
            + (f"; exceeded on {wbd}/{wtd}" if wbd else "")
            + f".{_pair_detail}"
        )

    sf_findings     = sf.get("findings") or []
    regression_note = ""
    priority_note   = ""
    critical_count  = 0

    # Structured batch-runtime regression counts (regressed / comparable / improved)
    # from the Batch-Runtime-Performance rollup — used for a clean, never-truncated
    # regression line and for the panel's "Runtime Regressions" KPI.
    _bm_d = digest.get("benchmark") or {}
    _bp_d = (_bm_d.get("batch_perf_summary") if isinstance(_bm_d, dict) else None) or {}
    _reg_jobs = _int(_bp_d.get("regressions"))  if _bp_d.get("regressions")  is not None else None
    _reg_comp = _int(_bp_d.get("comparable"))   if _bp_d.get("comparable")   is not None else None
    _reg_impr = _int(_bp_d.get("improvements")) if _bp_d.get("improvements") is not None else None

    if sf_findings:
        regressions    = [f for f in sf_findings if "RUNTIME_REGRESSION" in str(f.get("root_cause", ""))]
        critical_count = len([f for f in sf_findings if str(f.get("severity", "")).upper() == "CRITICAL"])
        if _reg_jobs is not None and _reg_comp:
            _impr_s = f"; {_reg_impr:,} improved" if _reg_impr is not None else ""
            regression_note = (
                f" Runtime regressions: {_reg_jobs:,} of {_reg_comp:,} comparable job(s) "
                f"slower vs baseline{_impr_s}."
            )
        elif regressions:
            # Fall back to the FULL finding text — never slice mid-word (the old
            # `[:50]` produced garbled fragments like "(>3σ above ba," / "791 imp.").
            _reg_txt = "; ".join(
                str(f.get("finding") or f.get("title") or "").strip()
                for f in regressions[:2]
            )
            regression_note = f" Runtime regression detected: {_reg_txt}."
    # When smart-findings carry no critical tally, fall back to the rule-engine count
    # so the panel's "critical findings block sign-off" matches the findings table.
    if not critical_count:
        critical_count = len((digest.get("rule_findings") or {}).get("critical") or [])

    if isinstance(triage, dict):
        low_buf_jobs = triage.get("low_buffer_jobs") or []
        unexplained  = triage.get("unexplained_breaches") or []
        priority_src = low_buf_jobs or unexplained
        if priority_src:
            if low_buf_jobs:
                sorted_low = sorted(
                    low_buf_jobs,
                    key=lambda j: (_num(j.get("buffer_pct"), 999), -_num(j.get("breach_rate"), 0)),
                )
                top_priority = sorted_low[:3]
                names = ", ".join(
                    f"{j.get('job_name') or j.get('Job_Name') or '?'} ({_num(j.get('buffer_pct'), 0):.1f}% buffer)"
                    for j in top_priority
                )
                suffix = "…" if len(sorted_low) > 3 else ""
                priority_note = f" Priority jobs needing attention: {names}{suffix}."
            else:
                top_priority = unexplained[:3]
                names = ", ".join(
                    f"{j.get('job_name', '?')} ({_num(j.get('margin_hrs'), 0):+.2f}h over)"
                    for j in top_priority
                )
                suffix = "…" if len(unexplained) > 3 else ""
                priority_note = f" Unexplained breaches needing attention: {names}{suffix}."

    batch_panel = None
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
            + priority_note
            + (f" {critical_count} critical finding(s) require resolution before PE sign-off."
               if critical_count > 0 else "")
        )

        # Conclusive, contradiction-free batch verdict panel — built from the SAME
        # canonical numbers as the prose above, routed through the reasoning engine
        # so it can never disagree with the Final Judgment (window is the binding
        # metric; job-level is a labelled secondary).
        _fail_rate = _bk_get("fail_rate_pct")
        if _fail_rate is None and fail_runs is not None and (total_runs or total_jobs):
            _fail_rate = round(int(fail_runs) / int(total_runs or total_jobs) * 100, 2)
        try:
            batch_panel = judgment_engine.build_batch_panel({
                "window_pct":        compliance,
                "window_estimated":  _window_compliance_is_estimated,
                "job_pct":           job_sla_comp,
                "total_days":        wtd,
                "breach_days":       wbd,
                "sla_breaches":      breaches,
                "exec_failures":     fail_runs,
                "fail_rate_pct":     _fail_rate,
                "total_runs":        total_runs,
                "total_jobs":        total_jobs,
                "reg_count":         _bk_get("regression_count", "runtime_regression_count"),
                "reg_jobs":          _reg_jobs,
                "reg_comparable":    _reg_comp,
                "reg_improved":      _reg_impr,
                "critical_findings": critical_count,
                "sla_limit_hrs":     sla_limit,
            })
        except Exception:
            log.exception("build_batch_panel failed")
            batch_panel = None
    else:
        sf_prose = sf.get("summary") or sf.get("batch_summary") or ""
        if sf_prose:
            prose_b = str(sf_prose)[:500]
        else:
            prose_b = (
                "Batch execution data is not loaded — upload Ctrl-M / batch CSVs "
                "for SLA compliance analysis. Then click Refresh Narrative."
            )

    _batch_section = {
        "id": "batch_sla", "title": "Batch Execution & SLA Compliance",
        "prose": prose_b,
        "table": {"headers": sla_headers, "rows": sla_rows},
    }
    if table_caption:
        _batch_section["table_caption"] = table_caption
    if batch_panel:
        _batch_section["panel"] = batch_panel
    sections.append(_batch_section)

    # -- 3. Infrastructure -------------------------------------------------
    inf_rows: List[List[str]] = []
    servers  = r.get("servers") or []

    type_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for s in servers:
        stype = (s.get("type") or s.get("server_type") or "APP").upper()
        type_buckets.setdefault(stype, []).append(s)

    from services.resource_calculator import (
        role_cpu_thresholds, mem_threshold,
        DB_MEM_EXPECTED_LO, DB_MEM_EXPECTED_HI,
    )
    from services.pe_config import MEM_WARN, MEM_CRIT

    def _cpu_status(peak: float, ok: float, warn: float) -> str:
        if peak <= ok:   return "OK"
        if peak <= warn: return "WATCH"
        return "HIGH"

    def _mem_status(stype: str, peak: float) -> str:
        if stype == "DB":
            return "OK (SGA band)" if peak <= DB_MEM_EXPECTED_HI else "HIGH"
        if peak <= MEM_WARN: return "OK"
        if peak <= MEM_CRIT: return "WATCH"
        return "HIGH"

    def _cpu_thresh_label(ok: float, warn: float) -> str:
        return f"role ceil {ok:.0f}% / warn {warn:.0f}%"

    def _mem_thresh_label(stype: str) -> str:
        if stype == "DB":
            return f"{DB_MEM_EXPECTED_LO:.0f}-{DB_MEM_EXPECTED_HI:.0f}% expected (SGA/PGA)"
        return f"warn {MEM_WARN:.0f}% / crit {MEM_CRIT:.0f}%"

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
        _rct     = role_cpu_thresholds(stype)
        _c_ok, _c_warn = _rct["ok"], _rct["warn"]
        # Each row now carries the governing threshold the grader actually applied
        # for this role + a peak-vs-threshold status, so a reviewer can verify the
        # math in-place instead of trusting a summary sentence that may disagree.
        inf_rows.append([f"{label} CPU",    f"{cpu_avg:.1f}%", f"{cpu_peak:.1f}%",
                         _cpu_thresh_label(_c_ok, _c_warn), _cpu_status(cpu_peak, _c_ok, _c_warn)])
        inf_rows.append([f"{label} Memory", f"{mem_avg:.1f}%", f"{mem_peak:.1f}%",
                         _mem_thresh_label(stype), _mem_status(stype, mem_peak)])

    if not inf_rows:
        inf_rows = [["NA", "NA", "NA", "NA", "NA"]]

    fleet_avg_cpu = _res_cpu(rk) or 0.0
    fleet_avg_mem = _res_mem(rk) or 0.0
    fleet_grade   = rk.get("fleet_grade") or "N/A"
    fleet_score   = _num(rk.get("fleet_score")) or 0.0
    n_crit        = _int(rk.get("n_critical") or rk.get("critical_count"))
    n_warn        = _int(rk.get("n_warning") or rk.get("warning_count"))
    n_dual        = _int(rk.get("n_dual_pressure"))
    # Role-aware hot-host detection — a host is "above ceiling" if its peak CPU
    # exceeds its ROLE ceiling OR its memory exceeds its role governing ceiling.
    # This replaces a flat CPU-only 75% check that ignored memory entirely and
    # made a DB memory peak of 91.7% invisible to the prose.
    role_hot = []
    for s in servers:
        _st = (s.get("type") or s.get("server_type") or "APP").upper()
        _c  = _num(s.get("cpu_pct") or s.get("cpu_utilisation") or s.get("cpu_usage") or 0) or 0.0
        _m  = _num(s.get("mem_pct") or s.get("mem_utilisation") or s.get("memory_pct") or 0) or 0.0
        if _c > role_cpu_thresholds(_st)["ok"] or _m > mem_threshold(_st):
            role_hot.append(s.get("host") or s.get("server") or "?")

    if servers:
        _app_ok = role_cpu_thresholds("APP")["ok"]
        _db_ok  = role_cpu_thresholds("DB")["ok"]
        _sre_ok = role_cpu_thresholds("SRE")["ok"]
        prose_i = (
            f"Average CPU across the fleet is {fleet_avg_cpu:.1f}% and memory {fleet_avg_mem:.1f}%. "
            "Thresholds are role-aware, not a single flat number — "
            f"APP CPU {_app_ok:.0f}%, DB CPU {_db_ok:.0f}%, SRE CPU {_sre_ok:.0f}%; "
            f"DB memory {DB_MEM_EXPECTED_LO:.0f}-{DB_MEM_EXPECTED_HI:.0f}% is the expected "
            "SGA/PGA band and is not alarmed. "
            + (f"{len(role_hot)} host(s) read above their role ceiling on peak "
               f"({', '.join(role_hot[:3])}); "
               if role_hot
               else "No host reads above its role-specific ceiling on peak; ")
            + f"under fleet health scoring this fleet has {n_crit} critical and {n_warn} warning server(s)."
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
        inf_caption = (
            f"Role-aware thresholds — APP CPU {_app_ok:.0f}% / DB CPU {_db_ok:.0f}% / "
            f"SRE CPU {_sre_ok:.0f}%; DB memory {DB_MEM_EXPECTED_LO:.0f}-{DB_MEM_EXPECTED_HI:.0f}% "
            f"expected (SGA/PGA), other memory crit {MEM_CRIT:.0f}%. Peak = worst single host in the role."
        )
    else:
        prose_i = (
            "Resource utilisation data is not loaded — upload resource report "
            "(DOCX/PDF) for infrastructure health analysis."
        )
        inf_caption = ""

    sections.append({
        "id": "infrastructure", "title": "Infrastructure Utilisation & Resource Health",
        "prose": prose_i,
        **({"table_caption": inf_caption} if inf_caption else {}),
        "table": {"headers": ["Resource Type", "Avg", "Peak", "Governing Threshold", "Status"], "rows": inf_rows},
    })

    # -- 4. UAT ------------------------------------------------------------
    # Priority: explicit UAT artefacts (uat_df) → benchmark / UI performance
    # test results (the UAT-phase evidence in practice) → empty state.
    uat_ac = digest.get("uat") or []
    bench  = digest.get("benchmark") or {}
    bench_rows = bench.get("rows") or []

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
    elif bench_rows:
        # Build UAT evidence from the performance benchmark (OK/WATCH/BREACH)
        def _sev(r):
            return {"BREACH": 0, "RED": 0, "WATCH": 1, "AMBER": 1}.get(str(r.get("status", "")), 2)
        sorted_rows = sorted(bench_rows, key=_sev)
        uat_rows = []
        for r in sorted_rows[:8]:
            cur = r.get("current_sec")
            base = r.get("baseline_sec")
            sla = r.get("sla_sec")
            ref = (f"SLA {sla:.0f}s" if isinstance(sla, (int, float)) and sla
                   else f"baseline {base:.1f}s" if isinstance(base, (int, float)) and base
                   else "—")
            uat_rows.append([
                str(r.get("transaction", "?"))[:60],
                str(r.get("action") or "—"),
                f"{cur:.1f}s" if isinstance(cur, (int, float)) else "NA",
                ref,
                str(r.get("status", "N/A")),
            ])

        n_total  = _int(bench.get("total_transactions") or len(bench_rows))
        n_breach = len([r for r in bench_rows if str(r.get("status")) in ("BREACH", "RED")])
        n_watch  = len([r for r in bench_rows if str(r.get("status")) in ("WATCH", "AMBER")])
        n_ok     = n_total - n_breach - n_watch
        pass_pct = round(n_ok / n_total * 100, 1) if n_total else 0.0
        cov      = bench.get("coverage_summary") or {}
        max_cc   = (cov.get("concurrency") or {}).get("max") if isinstance(cov.get("concurrency"), dict) else None

        uat_prose = (
            f"UAT performance validation from benchmark '{bench.get('filename', '')}': "
            f"{n_total} transaction(s) tested · {pass_pct:.0f}% pass rate "
            f"({n_ok} OK · {n_watch} WATCH · {n_breach} BREACH)."
            + (f" Max concurrency tested: {max_cc} user(s)." if max_cc else "")
            + (f" {n_breach} transaction(s) exceed SLA or regression threshold — "
               "resolution or formal customer acknowledgment required before sign-off."
               if n_breach else " All flows within agreed performance limits.")
        )
        uat_tbl = {
            "headers": ["Transaction", "Action", "Current", "Reference", "Status"],
            "rows":    uat_rows,
        }
    else:
        uat_prose = (
            "UAT validation artefacts were not provided in this audit run. "
            "Upload the performance benchmark / UAT test results for sign-off coverage analysis."
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
    # Rule-engine criticals + benchmark breaches now feed the verdict so the
    # narrative agrees with the PE Findings table (same evidence, same call).
    verdict  = "CONDITIONAL"
    evidence_facts = _build_evidence_facts(digest)
    rf_crit  = _int(rf_sum.get("critical") or rf_sum.get("CRITICAL"))
    _comp_n  = _num(compliance)
    _window_pair_n = _num(window_pair_compliance)
    _deadline_comp = _num(evidence_facts.get("deadline_compliance_pct"))
    _deadline_breach_days = _int(evidence_facts.get("deadline_breach_days"), 0)

    rule_f      = digest.get("rule_findings") or {}
    rule_crit_n = len(rule_f.get("critical") or [])
    bench_breach_n = len([r for r in bench_rows if str(r.get("status")) in ("BREACH", "RED")])

    if (_comp_n is not None and _comp_n >= 98 and rf_crit == 0
            and rule_crit_n == 0 and bench_breach_n == 0):
        verdict = "APPROVED"
    elif (
        rf_crit > 0
        or rule_crit_n > 0
        or (_comp_n is not None and _comp_n < 90)
        or (_deadline_comp is not None and _deadline_breach_days > 0 and _deadline_comp < 90)
    ):
        verdict = "BLOCKED"

    try:
        sf_verdict = (sf.get("verdict") or {}).get("decision", "")
        _vrank = {"BLOCKED": 2, "CONDITIONAL": 1, "APPROVED": 0, "PENDING": -1}
        if _vrank.get(sf_verdict, -1) > _vrank.get(verdict, -1):
            verdict = sf_verdict
    except Exception:
        pass

    verdict_reason = _build_verdict_reason(verdict, evidence_facts)

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
    # Batch-level breach signals beyond per-job ceilings. A batch that misses its
    # wall-clock window / deadline on calendar days is NOT "healthy" even when every
    # individual job had per-job buffer headroom. Folding these in is what lets the
    # diagnosis point at scheduling instead of falsely declaring "all clear".
    _cp_window_breach   = _int(bk.get("window_breach_days") or 0)
    _cp_window_total    = _int(bk.get("window_total_days") or 0)
    _cp_deadline_breach = _int(
        bk.get("deadline_breach_days")
        or ((bk.get("deadline_compliance") or {}).get("breach_days"))
        or 0
    )
    _cp_batch_unhealthy = (_cp_breach > 0) or (_cp_window_breach > 0) or (_cp_deadline_breach > 0)

    def _cp_batch_breach_phrase() -> str:
        # Cite the actual driver — window days first (the canonical PE sign-off signal)
        # so the scenario text reconciles with the headline window-compliance figure.
        if _cp_window_breach > 0 and _cp_window_total:
            base = f"batch missed its SLA window on {_cp_window_breach}/{_cp_window_total} day(s)"
            if _cp_breach == 0:
                base += " despite every job clearing its individual ceiling"
            return base
        if _cp_breach > 0:
            return f"{_cp_breach} SLA breach(es)"
        if _cp_deadline_breach > 0:
            return f"batch missed its wall-clock deadline on {_cp_deadline_breach} day(s)"
        return "a batch SLA issue"

    # suppress the scenario when all resource metrics are 0 (image-only docx)
    _cp_all_zero_res    = all(
        _num(s.get("cpu_pct") or s.get("cpu_utilisation") or 0) == 0
        and _num(s.get("mem_pct") or s.get("mem_utilisation") or 0) == 0
        for s in servers
    ) if servers else True
    _cp_diagnosis: str = ""

    if _cp_batch_loaded and _cp_resource_loaded and not _cp_all_zero_res:
        if not _cp_batch_unhealthy and _cp_crit_srv == 0:
            _cp_diagnosis = (
                "Scenario: BATCH + RESOURCE — both pillars healthy. "
                "All jobs within SLA, the batch made its window every day, and no "
                "critical infrastructure pressure was detected."
            )
        elif _cp_batch_unhealthy and _cp_crit_srv == 0:
            _warn_note = (
                f" Note: {_cp_warn_srv} server(s) in a warning state — monitor, but they "
                f"are not on the batch's critical path."
                if _cp_warn_srv > 1 else ""
            )
            _cp_diagnosis = (
                f"Scenario: SCHEDULING ISSUE — {_cp_batch_breach_phrase()} with an "
                f"otherwise healthy fleet (grade {fleet_grade}, 0 critical servers). "
                "Root cause is scheduling logic, batch sequencing, or SQL regression — "
                "not hardware capacity." + _warn_note
            )
        elif not _cp_batch_unhealthy and _cp_crit_srv > 0:
            _cp_diagnosis = (
                f"Scenario: HIDDEN INFRA RISK — batch currently within SLA but "
                f"{_cp_crit_srv} server(s) are at critical state (grade {fleet_grade}). "
                "Infrastructure must be remediated before sign-off."
            )
        elif _cp_batch_unhealthy and _cp_crit_srv > 0:
            _cp_diagnosis = (
                f"Scenario: COMPOUND RISK — {_cp_batch_breach_phrase()} AND "
                f"{_cp_crit_srv} critical server(s) simultaneously. "
                "Two parallel workstreams required: infrastructure scale-up + job optimisation."
            )
    elif _cp_batch_loaded and not _cp_resource_loaded:
        if _cp_batch_unhealthy:
            _cp_diagnosis = (
                f"Scenario: BATCH-ONLY — {_cp_batch_breach_phrase()} with no resource data available. "
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
    # Headline = DAY-LEVEL window compliance, paired with the breach-day fraction it
    # reconciles with. Pair-level ((sub_app × day)) shown only as a labeled secondary.
    if wtd:
        _clean_days = wtd - wbd
        _day_comp = _comp_n if _comp_n is not None else round(_clean_days / wtd * 100, 1)
        _win_line = (
            f"Batch window compliance: {_day_comp:.1f}% "
            f"— made its window on {_clean_days}/{wtd} day(s)"
            + (f", exceeded on {wbd}/{wtd}" if wbd else "") + "."
        )
        if _window_pair_n is not None and abs(_window_pair_n - _day_comp) > 0.1:
            _win_line += f" (per sub-app \u00d7 day window: {_window_pair_n:.1f}%.)"
        parts.append(_win_line)
    elif _comp_n is not None:
        parts.append(f"Batch SLA compliance: {_comp_n:.1f}%.")
    if fleet_grade and fleet_grade != "N/A":
        parts.append(
            f"Fleet grade: {fleet_grade}"
            + (f" ({n_crit} critical server(s))" if n_crit else "") + "."
        )
    if _cp_diagnosis:
        parts.append(_cp_diagnosis)
    if rule_crit_n:
        _crit_srcs = sorted({str(c.get("source") or "?") for c in (rule_f.get("critical") or [])})
        parts.append(
            f"{rule_crit_n} critical PE finding(s) open"
            + (f" (sources: {', '.join(_crit_srcs[:4])})" if _crit_srcs else "")
            + " — must be resolved or formally acknowledged before sign-off."
        )
    if bench_breach_n:
        parts.append(f"{bench_breach_n} UAT benchmark transaction(s) in BREACH.")
    if rf_crit:
        parts.append(
            f"{rf_crit} critical finding(s) require immediate attention before PE sign-off."
        )
    parts.append(
        f"Overall verdict: {verdict}. {verdict_reason}. Sections below contain the full evidence breakdown."
    )

    return {
        "verdict":  verdict,
        "verdict_reason": verdict_reason,
        "evidence_facts": evidence_facts,
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
        _facts = out.get("evidence_facts") or _build_evidence_facts(digest)
        out["evidence_facts"] = _facts
        out["verdict_reason"] = _build_verdict_reason(out["verdict"], _facts)
    else:
        _vrank = {"BLOCKED": 2, "CONDITIONAL": 1, "APPROVED": 0}
        out["verdict"] = (
            ai_verdict
            if _vrank.get(ai_verdict, -1) >= _vrank.get(det_verdict, -1)
            else det_verdict
        )
        out["verdict_reason"] = out.get("verdict_reason") or _build_verdict_reason(
            out["verdict"], out.get("evidence_facts") or {}
        )

    if isinstance(ai_payload.get("summary"), str) and ai_payload["summary"].strip():
        out["summary"] = ai_payload["summary"].strip()
    if out.get("verdict_reason") and out["verdict_reason"] not in out.get("summary", ""):
        out["summary"] = (out.get("summary") or "").rstrip() + " " + out["verdict_reason"] + "."

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
                # Deterministic, single-source extras the AI must NOT override or drop:
                # the verdict panel + table caption always come from the fallback so the
                # batch conclusion can never be reworded into a contradiction by the LLM,
                # and the provenance badge always reflects the real data source.
                **({"panel": fb_sec["panel"]} if (fb_sec and fb_sec.get("panel")) else {}),
                **({"table_caption": fb_sec["table_caption"]} if (fb_sec and fb_sec.get("table_caption")) else {}),
                **({"provenance": fb_sec["provenance"]} if (fb_sec and fb_sec.get("provenance")) else {}),
            })
        elif fb_sec:
            merged.append(fb_sec)

    out["sections"] = merged
    return out


def _bare_fallback(customer: str) -> Dict[str, Any]:
    return {
        "verdict": "CONDITIONAL",
        "verdict_reason": "CONDITIONAL — driven by insufficient evidence loaded",
        "evidence_facts": {},
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

    # ── AI enhancement pass (optional) ───────────────────────────────────────
    # The deterministic fallback already carries all numbers + tables. The AI
    # layer only rewrites the PROSE (summary + per-section narrative) for a more
    # consultant-grade read. _validate_and_merge keeps deterministic tables and
    # never lets the AI downgrade a verdict. If no key is configured, the model
    # errors, or returns junk → we return the clean fallback untouched.
    try:
        from services import ai_engine as _ai
        ready = _ai.is_ready()
        if ready.get("nvidia_key") or ready.get("gemini_key"):
            _section_ids = [s["id"] for s in _SECTIONS]
            evidence_facts = fallback.get("evidence_facts") or _build_evidence_facts(digest)
            verdict_reason = fallback.get("verdict_reason") or _build_verdict_reason(
                fallback.get("verdict", "CONDITIONAL"), evidence_facts
            )
            ai_prompt = (
                "You are a senior Performance Engineering reviewer writing the "
                f"PE Review narrative for customer '{customer or 'the account'}'.\n"
                "Make the verdict using ONLY the named EVIDENCE FACTS below. "
                "Your verdict MUST cite which evidence facts caused the "
                "BLOCKED/APPROVED/CONDITIONAL decision. Do not invent additional context.\n"
                "Return a strict JSON object:\n"
                '{\n'
                '  "verdict": "APPROVED" | "CONDITIONAL" | "BLOCKED",\n'
                '  "summary": "<3-4 sentence executive summary, lead with hard numbers>",\n'
                '  "sections": [ {"id": "<section id>", "prose": "<2-4 factual sentences>"} ]\n'
                '}\n'
                f"Valid section ids (use each at most once): {_section_ids}.\n"
                f"Canonical deterministic verdict reason to cite exactly where applicable: {verdict_reason}.\n"
                "Rules: be direct and factual, no hedging, lead with numbers, do NOT "
                "invent values absent from the evidence facts or digest, do NOT output tables (numbers "
                "are supplied separately). Choose the verdict strictly from the data: "
                "any SLA breach / critical finding / sub-85% compliance => not APPROVED."
            )
            try:
                ai_body = json.dumps(digest, default=str)
            except Exception:
                ai_body = str(digest)
            if len(ai_body) > 16000:
                ai_body = ai_body[:16000] + " …<truncated>"
            ai_payload, ai_model = _ai.chat_json(
                (
                    f"{ai_prompt}\n\nEVIDENCE FACTS:\n{_evidence_facts_block(evidence_facts)}"
                    f"\n\nDIGEST (supporting detail only):\n{ai_body}"
                ),
                max_tokens=1400, temperature=0.25,
            )
            merged = _validate_and_merge(ai_payload, fallback, digest)
            if isinstance(ai_model, str) and ai_model:
                merged["model"] = ai_model
            return merged
    except Exception as exc:  # noqa: BLE001
        log.info("pe_narrative: AI enhancement skipped (%s)", exc)

    return fallback
