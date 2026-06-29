"""
PE Audit Findings router — rule-based intelligence engine v2.

POST /api/generate-findings
    body: {
      batch_kpis, top_jobs, top_breaches, window, anomalies, sub_stats,
      resource_kpis, servers,
      issues, sla_matrix, benchmark, sow_compare, customer_name,
      sla_ceilings    # optional: from uploaded SLA XLSX
    }
    response: { findings: [{level, icon, text, sub, source}],
                summary: {critical, warning, ok, info, total},
                data_coverage: {batch, resource, sla, benchmark, sow} }

All status/buffer calculations go through services.pe_utils — never inline.
SLA ceilings read from sla_ceilings dict (uploaded XLSX) > pe_config defaults.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("pe_dashboard.findings")

# Top-level imports — avoids import-inside-function anti-pattern
try:
    from services import config_store as _config_store
    from services import session_cache as _session_cache
except ImportError as _ie:
    log.warning("findings: service import failed at load: %s", _ie)
    _config_store = None  # type: ignore[assignment]
    _session_cache = None  # type: ignore[assignment]

from services.pe_utils import (
    STATUS_COLOR,
    buffer_pct,
    coerce_float as _f,
    coerce_int as _i,
    detect_batch_type,
    fmt_pct,
    get_sla_hrs,
    job_status,
    safe_metric,
)

router = APIRouter()


# Environment detection from job / sub-application names. A leading PROD_/TEST_/
# UAT_/DEV_/STG_ prefix is the standard Ctrl-M convention; used to adapt SOW
# volume questions (a partial TEST load is often intentional scope, a PROD run
# below the contracted floor is a forecast / commercial concern).
import re as _re

_RE_ENV_TEST = _re.compile(r"^(?:TEST|UAT|DEV|STG|SIT|QA)[_\-]", _re.IGNORECASE)
_RE_ENV_PROD = _re.compile(r"^PROD[_\-]", _re.IGNORECASE)


def _re_env_test(name: str) -> bool:
    return bool(_RE_ENV_TEST.match(str(name or "").strip()))


def _re_env_prod(name: str) -> bool:
    return bool(_RE_ENV_PROD.match(str(name or "").strip()))


def _get_data_coverage(batch_kpis: dict | None) -> dict | None:
    """Single source of truth for fetching data_coverage — request payload first, session cache fallback."""
    cov = (batch_kpis or {}).get("data_coverage") or None
    if cov:
        return cov
    try:
        if _session_cache:
            lb = _session_cache.get("last_batch") or {}
            return lb.get("data_coverage") or (lb.get("kpis") or {}).get("data_coverage")
    except Exception as _e:
        log.debug("data_coverage session cache fallback failed: %s", _e)
    return None


# ── Request / Response models ──────────────────────────────────────────────────

class FindingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Batch data (full payload from /api/process-batch)
    batch_kpis:    Optional[Dict[str, Any]]       = None
    top_jobs:      Optional[List[Dict[str, Any]]] = None
    top_breaches:  Optional[List[Dict[str, Any]]] = None
    window:        Optional[List[Dict[str, Any]]] = None
    anomalies:     Optional[List[Dict[str, Any]]] = None
    sub_stats:     Optional[List[Dict[str, Any]]] = None

    # Resource data
    resource_kpis: Optional[Dict[str, Any]]       = None
    servers:       Optional[List[Dict[str, Any]]] = None

    # SLA ceilings from uploaded SLA XLSX (RULE 2)
    # {"DAILY": 4.0, "WEEKLY": 6.0, "MONTHLY": 8.0}
    sla_ceilings:  Optional[Dict[str, float]]     = None

    # Additional data sources
    issues:        Optional[List[Dict[str, Any]]] = None
    sla_matrix:    Optional[Dict[str, Any]]       = None
    benchmark:     Optional[Dict[str, Any]]       = None
    sow_compare:   Optional[Dict[str, Any]]       = None

    # Context
    customer_name: Optional[str]  = None
    sow_dfu:       Optional[float] = 0.0
    sow_dfu_base:  Optional[float] = 0.0

    # SLA triage data (from _buildSlaTriage() JS function)
    sla_triage:    Optional[Dict[str, Any]] = None

    # Azure Monitor deep dive time-series evidence (spikes, patterns, trends)
    deep_dive:     Optional[Dict[str, Any]] = None


class Finding(BaseModel):
    level:          str          # critical | warning | info | ok
    icon:           str
    text:           str
    sub:            str = ""
    source:         str = ""     # batch | resource | sla | benchmark | sow | issues
    confidence:     int = 100    # 0-100: how confident we are in this finding
    impact:         str = ""     # business impact statement
    evidence:       str = ""     # source file / metric / calculation reference
    recommendation: str = ""     # suggested next action
    evidence_class: str = ""     # measured | inferred | defaulted | waived | unavailable
    root_cause:     str = ""     # likely root cause category


class FindingsSummary(BaseModel):
    critical: int = 0
    warning:  int = 0
    info:     int = 0
    ok:       int = 0
    total:    int = 0


class DataCoverage(BaseModel):
    batch:     bool = False
    resource:  bool = False
    sla:       bool = False
    benchmark: bool = False
    sow:       bool = False


class AuditCoverage(BaseModel):
    """PE Audit readiness coverage strip — which evidence is loaded."""
    evidence_30day:    str = "missing"   # loaded | partial | missing
    sla_source:        str = "missing"   # customer | default | missing
    waivers:           str = "missing"   # loaded | missing
    ui_signoff:        str = "missing"   # attached | missing
    automation_status: str = "missing"   # loaded | missing
    volume_vs_sow:     str = "missing"   # loaded | missing
    confidence:        int = 0           # 0-100 overall confidence
    confidence_label:  str = "INSUFFICIENT"


class FindingsResponse(BaseModel):
    findings:             List[Finding]
    summary:              FindingsSummary
    data_coverage:        DataCoverage
    audit_coverage:       Optional[AuditCoverage] = None
    penalty_score:        float = 0.0   # 0-100 unified grade input
    findings_grade:       str   = ""    # A/B/C/D/F
    findings_grade_label: str   = ""    # human label
    # Gap A: Sub_Application × run_date failure-density pivot for the PE Findings
    # heatmap card. Computed once in batch_calculator and read from the audit
    # context here so the frontend renders from the findings response directly.
    failure_grid:         Optional[Dict[str, Any]] = None


def _fmt_hrs(v: float) -> str:
    """Format hours with smart decimal precision.

    0.75 → '0.75'   (not '0.8' from banker's rounding)
    1.5  → '1.5'
    6.0  → '6.0'
    """
    if v == 0:
        return "0"
    # If 1-decimal rounding loses >1% precision, use 2 decimals
    r1 = round(v, 1)
    if abs(r1 - v) / max(abs(v), 1e-9) > 0.01:
        return f"{v:.2f}"
    return f"{r1:.1f}"


# ── Core rule engine ───────────────────────────────────────────────────────────

def _generate(req: FindingsRequest) -> tuple[list[Finding], DataCoverage]:
    findings: list[Finding] = []
    cov = DataCoverage()

    bk          = req.batch_kpis    or {}
    rk          = req.resource_kpis or {}
    servers     = req.servers       or []
    issues      = req.issues        or []
    top_jobs    = req.top_jobs      or []
    top_breaches= req.top_breaches  or []
    window_data = req.window        or []
    anomalies   = req.anomalies     or []
    sub_stats   = req.sub_stats     or []
    sla_ceil    = req.sla_ceilings  or {}

    # Early SLA ceiling enrichment — use top-level _config_store (no import-inside-function).
    if not sla_ceil and _config_store:
        try:
            _early_ceil: dict[str, float] = {}
            for _ek, _et in (("daily_sla_hrs", "DAILY"), ("weekly_sla_hrs", "WEEKLY"),
                             ("monthly_sla_hrs", "MONTHLY"), ("custom_sla_hrs", "CUSTOM")):
                _ev = _config_store.get(_ek)
                if _ev is not None and _f(_ev) > 0:
                    _early_ceil[_et] = _f(_ev)
            if _early_ceil:
                sla_ceil = _early_ceil
                req.sla_ceilings = _early_ceil
        except Exception as _e:
            log.warning("findings: SLA ceiling enrichment failed: %s", _e)

    has_batch    = bool(bk) or bool(top_jobs)
    has_resource = bool(rk) or bool(servers)
    cov.batch    = has_batch
    cov.resource = has_resource

    # sla_loaded = True only when a CUSTOMER SLA file has been uploaded and applied.
    # The authoritative signal is the batch KPIs' own sla_source.type: batch_calculator
    # sets it to "sla_matrix" / "batch_sla_xlsx" ONLY when the batch was actually
    # re-solved against an uploaded customer SLA file (config-derived defaults stay
    # "default"). We key off that alone rather than also requiring req.sla_ceilings —
    # the frontend sends sla_ceilings as a redundant convenience field, and depending
    # on it caused findings to falsely read "Assumed / matrix not uploaded" whenever
    # that field was missing even though batch compliance was using customer values.
    _bk_sla_type = (bk.get("sla_source") or {}).get("type", "default") if bk else "default"
    sla_loaded = (
        _bk_sla_type in ("sla_matrix", "batch_sla_xlsx")   # batch resolved against customer SLA file
        or bool(req.sla_matrix)                            # SLA Matrix intelligence tab uploaded
    )

    # Set True once the canonical Batch Window Compliance verdict is emitted (R1b,
    # batch section). The SLA-matrix section defers to this flag so the identical
    # window metric never surfaces as two near-duplicate findings with potentially
    # divergent severity.
    _window_finding_emitted = False

    def add(level, icon, text, sub="", source="", confidence=100,
            impact="", evidence="", recommendation="", evidence_class="measured",
            root_cause=""):
        findings.append(Finding(
            level=level, icon=icon, text=text, sub=sub, source=source,
            confidence=confidence, impact=impact, evidence=evidence,
            recommendation=recommendation, evidence_class=evidence_class,
            root_cause=root_cause,
        ))

    # ─── No data at all ──────────────────────────────────────────────────────
    _has_sow_or_bench = bool(req.sow_compare) or bool(req.benchmark) or bool(issues)
    if not has_batch and not has_resource and not _has_sow_or_bench:
        add("info", "📂",
            "No audit data loaded",
            "Upload a Ctrl-M CSV and/or Resource Utilization report to begin PE audit analysis",
            source="")
        return findings, cov

    # ═══════════════════════════════════════════════════════════════
    # PE AUDIT COVERAGE STRIP
    # ═══════════════════════════════════════════════════════════════
    # Track what evidence has been loaded for audit readiness
    # data_coverage lives as a top-level key in the batch response, but
    # the frontend sends batch_kpis as just the flat KPIs dict.  Try both
    # locations so confidence/date_span are not lost.
    batch_cov = _get_data_coverage(req.batch_kpis)
    if not batch_cov and req.batch_kpis:
        # Final fallback: synthesise minimal coverage from available kpis
        batch_cov = {
            "confidence":       req.batch_kpis.get("data_confidence", 100),
            "confidence_label": "INFERRED",
            "date_span_days":   req.batch_kpis.get("date_span_days", 0),
            "has_end_time":     req.batch_kpis.get("has_end_time", False),
            "sla_source":       "customer" if sla_loaded else "default",
            "warnings":         [],
        }
    bench_loaded  = bool(req.benchmark)
    sow_loaded    = bool(req.sow_compare)
    issues_loaded = bool(issues)

    # Data confidence warning (batch)
    if batch_cov:
        conf = _f(batch_cov.get("confidence"))
        conf_label = batch_cov.get("confidence_label", "")
        if conf < 60:
            add("warning", "📉",
                f"Batch data confidence: {conf:.0f}% ({conf_label})",
                "Incomplete source columns or insufficient date range. "
                "Some metrics may not be fully reliable.",
                source="batch", confidence=int(conf),
                impact="Compliance and SLA calculations may be inaccurate",
                recommendation="Upload complete Ctrl-M export with Start_Time, End_Time, Status, and 30+ days of data")

        # Warn about default SLA — only when no SLA file has been loaded in this session.
        # Both "DEFAULT_SLA" (old code) and "NO_SLA_FILE" (batch_calculator) are handled.
        # Warnings can live in batch_cov["warnings"] OR in batch_kpis.sla_source.warnings.
        sla_src = batch_cov.get("sla_source") if batch_cov else None
        _sla_warnings = list(batch_cov.get("warnings") or [])
        # Also pull from sla_source.warnings in the raw batch_kpis
        _bk_sla_src = (req.batch_kpis or {}).get("sla_source") or {}
        if isinstance(_bk_sla_src, dict):
            for _w in (_bk_sla_src.get("warnings") or []):
                if isinstance(_w, dict) and _w not in _sla_warnings:
                    _sla_warnings.append(_w)
        for w in _sla_warnings:
            code = w.get("code", "")
            if code in ("DEFAULT_SLA", "NO_SLA_FILE") and not sla_loaded:
                add("warning", "📐",
                    "SLA Source: Assumed — no customer SLA matrix uploaded",
                    w.get("text", "") + " · Compliance results cannot be marked green for audit sign-off "
                    "while using assumed default values.",
                    source="batch", evidence_class="defaulted",
                    impact="Compliance based on assumed defaults — audit sign-off blocked",
                    recommendation="Upload a customer-approved SLA XLSX to unlock green compliance status")
            elif code == "NO_END_TIME":
                add("info", "⏳",
                    "Elapsed batch window unavailable — End_Time column missing",
                    w.get("text", ""),
                    source="batch",
                    impact="Cannot distinguish summed runtime from real wall-clock batch window",
                    recommendation="Export Ctrl-M data with both Start_Time and End_Time columns")
            elif code == "SHORT_HISTORY":
                add("warning", "📅",
                    w.get("text", "Insufficient date range for PE audit"),
                    "30-day batch history is standard PE audit evidence requirement.",
                    source="batch",
                    impact="PE sign-off may be challenged with less than 30 days of evidence",
                    recommendation="Upload at least 30 days of Ctrl-M execution history")

    # ═══════════════════════════════════════════════════════════════
    # BATCH / SLA RULES
    # ═══════════════════════════════════════════════════════════════
    if has_batch:
        compliance   = _f(bk.get("compliance_pct"))
        jobs_breach  = _i(bk.get("jobs_breach"))
        jobs_at_risk = _i(bk.get("jobs_at_risk"))
        total_jobs   = _i(bk.get("total_jobs"))
        total_runs   = _i(bk.get("total_runs"))
        fsla         = bk.get("fleet_sla_buffer") or {}

        # Resolve the dominant schedule type for this dataset.
        # batch_calculator exports sla_detected_mode (from _detect_sla_ceiling)
        # plus batch_type.  Trust that explicit detection — only infer from
        # sub_stats when batch_calculator gave us nothing, otherwise a daily batch
        # that merely *contains* a weekly/monthly sub-app would be mislabelled
        # (e.g. "monthly" paired with the 7.5h daily ceiling — internally
        # inconsistent and misleading).
        _raw_sched = (bk.get("sla_detected_mode") or bk.get("batch_type") or "").upper()
        _detected_sched = _raw_sched or "DAILY"
        if not _raw_sched and sub_stats:
            for _ss in sub_stats:
                _ss_name = (_ss.get("Sub_Application") or _ss.get("sub_application") or "")
                _ss_type = detect_batch_type(_ss_name)
                if _ss_type != "DAILY":
                    _detected_sched = _ss_type
                    break
        default_sla = (
            _f(bk.get("sla_ceiling") or 0)
            or _f(bk.get("daily_limit_hrs") or 0)
            or get_sla_hrs(_detected_sched, sla_ceil)
        )
        _sched_label = _detected_sched.lower()  # "daily", "weekly", etc.

        # R0 — Data health check: do we have meaningful batch data?
        if total_runs == 0:
            add("warning", "⚠️",
                "Ctrl-M file uploaded but no runs found",
                "Verify the CSV has Start_Time, Job_Name and Run_Sec columns",
                source="batch")

        # R1 — Job SLA Compliance (individual job peaks vs SLA)
        batch_conf = int(_f((batch_cov or {}).get("confidence", 100)))
        job_sla_comp = _f(bk.get("job_sla_compliance", bk.get("compliance_pct")))
        # Window compliance: DAY-LEVEL is the canonical headline (% of calendar days the
        # batch made its window). DERIVE it from the breach/total days shown beside it so
        # the headline % and the "{breach}/{total} day(s)" fraction always reconcile.
        # Pair-level ((sub_app × day)) is retained only as a labeled secondary detail.
        window_comp_pair = _f(bk.get("batch_window_compliance", 100))
        win_breach   = _i(bk.get("window_breach_days"))
        win_total    = _i(bk.get("window_total_days"))
        if win_total and win_total > 0:
            window_comp = round((win_total - win_breach) / win_total * 100, 1)
        else:
            _day_comp_raw = bk.get("window_day_compliance_pct")
            window_comp = _f(_day_comp_raw) if _day_comp_raw is not None else window_comp_pair

        sla_evidence_class = "measured" if sla_loaded else "defaulted"
        sla_src_label = "From SLA Matrix" if sla_loaded else "Assumed"
        # Two-tier SLA label: classify what kind of SLA we're measuring here.
        # R1/R1b/R2 use the schedule-level ceiling (SOW or assumed default).
        # R4b (below) handles the JOB-SPECIFIC operational matrix separately.
        sla_tier_label = "(SOW / agreed ceiling)" if sla_loaded else "(assumed default ceiling)"

        if jobs_breach > 0:
            add("critical", "🚨",
                f"Job SLA Compliance: {jobs_breach} job(s) breached SLA ceiling {sla_tier_label}",
                f"Peak runtime exceeded {_fmt_hrs(default_sla)}h {_sched_label} limit · SLA source: {sla_src_label}",
                source="batch", confidence=batch_conf,
                impact="PE sign-off blocked until all job-level SLA breaches are resolved",
                evidence=f"Ctrl-M peak runtime vs {_fmt_hrs(default_sla)}h {'customer' if sla_loaded else 'default'} {_sched_label} SLA · Source: {sla_src_label}",
                recommendation="Investigate worst-case jobs, optimise or request SLA waiver",
                evidence_class=sla_evidence_class)
        elif jobs_at_risk > 0:
            add("warning", "⚠️",
                f"Job SLA Compliance: {job_sla_comp:.1f}% — {jobs_at_risk} job(s) at risk {sla_tier_label}",
                f"Approaching SLA boundary (>85% used) · SLA source: {sla_src_label}",
                source="batch", confidence=batch_conf,
                impact="Risk of job-level SLA breach under production load conditions",
                recommendation="Monitor at-risk jobs during next batch cycle",
                evidence_class=sla_evidence_class)
        else:
            add("ok", "✅",
                f"Job SLA Compliance: {job_sla_comp:.1f}% — all individual jobs within SLA",
                f"{total_jobs} jobs · {total_runs} runs · SLA source: {sla_src_label}",
                source="batch", confidence=batch_conf,
                evidence=f"All job peaks below {_fmt_hrs(default_sla)}h {'customer' if sla_loaded else 'default'} {_sched_label} SLA · Source: {sla_src_label}",
                evidence_class=sla_evidence_class)

        # R1b — Batch Window Compliance (aggregate daily total vs SLA).
        # SINGLE canonical window-compliance verdict: the SLA-matrix section below
        # defers to it (via _window_finding_emitted) so the same metric never shows
        # as two near-identical findings. Severity is threshold-based (critical when
        # compliance < 75%, else warning) so a few breach days on an otherwise-healthy
        # window isn't over-escalated — and can never disagree with the SLA section's
        # own < 75% threshold.
        if win_total > 0 and win_breach > 0:
            # Find worst breach day inline for the sub-text.
            # Prefer elapsed_hrs (wall-clock) over total_hrs (summed) because
            # parallel jobs inflate total_hrs beyond the actual window duration.
            _worst_day_detail = ""
            _has_elapsed = any(_f(w.get("elapsed_hrs")) > 0 for w in window_data)
            _hrs_key = "elapsed_hrs" if _has_elapsed else "total_hrs"
            _breach_days_r1b = [w for w in window_data if _f(w.get(_hrs_key)) > default_sla]
            if _breach_days_r1b:
                _worst = max(_breach_days_r1b, key=lambda d: _f(d.get(_hrs_key)))
                _worst_hrs = _f(_worst.get(_hrs_key))
                _worst_day_detail = (f" · Worst day: {_worst.get('run_date','?')} at "
                                    f"{_worst_hrs:.1f}h "
                                    f"(+{_worst_hrs - default_sla:.1f}h overrun)")
            _win_level = "critical" if window_comp < 75 else "warning"
            add(_win_level, "📅",
                f"Batch Window Compliance: {window_comp:.1f}% — SLA exceeded on {win_breach}/{win_total} day(s)",
                f"Aggregate {_sched_label} runtime exceeded {_fmt_hrs(default_sla)}h limit · "
                f"SLA source: {sla_src_label}. "
                f"Individual jobs may each be within SLA, but the total batch window is not. "
                f"(Per sub-app \u00d7 day window: {window_comp_pair:.1f}%.)"
                f"{_worst_day_detail}",
                source="batch", confidence=batch_conf,
                impact=f"Batch window overrun on {win_breach} day(s) blocks PE sign-off",
                evidence=f"{'Elapsed' if _has_elapsed else 'Summed'} batch window vs {_fmt_hrs(default_sla)}h {_sched_label} SLA · Source: {sla_src_label}",
                recommendation="Reschedule overlapping jobs or request extended batch window",
                evidence_class=sla_evidence_class)
            _window_finding_emitted = True
        elif win_total > 0:
            add("ok", "✅",
                f"Batch Window Compliance: {window_comp:.1f}% — aggregate {_sched_label} window within SLA",
                f"All {win_total} day(s) within {_fmt_hrs(default_sla)}h {_sched_label} batch window · SLA source: {sla_src_label}",
                source="batch", confidence=batch_conf,
                evidence_class=sla_evidence_class)
            _window_finding_emitted = True

        # R1c — Wall-clock SLA DEADLINE compliance (absolute clock ceiling).
        # Distinct failure mode from R1b: a batch can use FEWER hours than its
        # window allows yet still FINISH past its contracted clock-time deadline
        # (e.g. a DAILY batch that ends 09:13 against a 06:00 EST ceiling used
        # only 9h but breached by 3h13m). This reads the single canonical
        # deadline_compliance summary so the count here can never disagree with
        # the Batch Review / SLA Matrix tiles.
        _dl = bk.get("deadline_compliance") or {}
        if _dl.get("has_deadlines"):
            _dl_breach_days = _i(_dl.get("breach_days"))
            _dl_assessable  = _i(_dl.get("assessable_windows"))
            _dl_pct         = _dl.get("compliance_pct")
            _dl_worst       = _f(_dl.get("worst_overrun_hrs"))
            _dl_pct_txt     = f"{_f(_dl_pct):.1f}%" if _dl_pct is not None else "n/a"
            if _dl_breach_days > 0:
                _ex = (_dl.get("breaches") or [{}])[0]
                _ex_sa  = _ex.get("sub_app", "?")
                _ex_end = str(_ex.get("last_end_clock") or "")[11:16] or "?"
                _ex_dl  = str(_ex.get("sla_deadline_clock") or "")[11:16] or "?"
                _ex_ovr = _f(_ex.get("overrun_hrs"))
                _dl_level = "critical" if (_dl_pct is not None and _f(_dl_pct) < 75) else "warning"
                add(_dl_level, "⏰",
                    f"SLA Deadline Compliance: {_dl_pct_txt} — clock deadline missed on "
                    f"{_dl_breach_days}/{_dl_assessable} window(s)",
                    f"Batches finished past their contracted wall-clock ceiling · "
                    f"Worst: {_ex_sa} ended {_ex_end} vs {_ex_dl} deadline (+{_ex_ovr:.1f}h overrun). "
                    f"This is separate from duration: a batch within its hour-budget can still "
                    f"miss the absolute clock deadline.",
                    source="batch", confidence=batch_conf,
                    impact=f"Wall-clock SLA deadline breached on {_dl_breach_days} window(s) — "
                           f"downstream consumers receive data late; blocks PE sign-off",
                    evidence=f"Actual last End_Time vs contracted sla_end clock ceiling · "
                             f"worst overrun {_dl_worst:.2f}h · Source: {sla_src_label}",
                    recommendation="Pull batch start earlier or accelerate the late-finishing "
                                   "workflow so completion lands before the contracted deadline",
                    evidence_class=sla_evidence_class,
                    root_cause="SLA_DEADLINE_BREACH")
            else:
                add("ok", "✅",
                    f"SLA Deadline Compliance: {_dl_pct_txt} — all batches finished before clock deadline",
                    f"All {_dl_assessable} assessable window(s) completed within their contracted "
                    f"wall-clock ceiling · Source: {sla_src_label}",
                    source="batch", confidence=batch_conf,
                    evidence_class=sla_evidence_class)

        # R2 — Per-job SLA ceiling check using pe_utils.job_status (RULE 1)
        # Uses composite key Sub_Application + Job_Name (RULE 6)
        ceiling_hits: list[str] = []
        at_risk_jobs: list[str] = []
        caution_jobs: list[str] = []

        for j in (top_jobs or []):
            peak_hrs  = _f(j.get("peak_hrs"))
            j_name    = j.get("Job_Name") or j.get("job_name") or "?"
            sub_app   = j.get("Sub_Application") or j.get("sub_application") or ""
            b_type    = detect_batch_type(j_name)
            j_sla     = get_sla_hrs(b_type, sla_ceil)
            status    = job_status(peak_hrs, j_sla)
            composite = f"{sub_app}:{j_name}" if sub_app else j_name

            if status == "BREACH":
                ceiling_hits.append(f"{composite} ({peak_hrs:.2f}h/{j_sla:.1f}h)")
            elif status == "AT_RISK":
                at_risk_jobs.append(composite)
            elif status == "CAUTION":
                caution_jobs.append(composite)

        if ceiling_hits:
            names = ", ".join(ceiling_hits[:3])
            add("critical", "🔴",
                f"{len(ceiling_hits)} job(s) breaching individual SLA ceiling",
                f"{names}{'…' if len(ceiling_hits) > 3 else ''} — action required before go-live",
                source="batch")

        if at_risk_jobs and len(at_risk_jobs) != jobs_at_risk:
            # Only if different from global count (avoids duplicate)
            names = ", ".join(at_risk_jobs[:3])
            add("warning", "🔶",
                f"{len(at_risk_jobs)} job(s) within 15% of SLA ceiling (AT_RISK)",
                f"Jobs: {names}{'…' if len(at_risk_jobs) > 3 else ''}",
                source="batch")

        # R3 — Worst batch window breach detail — now consolidated into R1b sub-text.
        # Kept as a separate variable for cross-reference but no longer emits a finding.
        sla_default = default_sla  # use the schedule-aware ceiling, not hardcoded DAILY

        # R4 — Fleet SLA buffer tightness
        if fsla and fsla.get("status") in ("CRITICAL", "CAUTION"):
            buf_pct = _f(fsla.get("buffer_pct"))
            buf_hrs = _f(fsla.get("buffer_hrs"))
            tight_name = fsla.get("job_name", "worst job")
            add("warning", "⏱️",
                f"Tightest SLA buffer: {buf_pct:.1f}% headroom ({fsla.get('status','')})",
                f"Worst job has only {buf_hrs:.2f}h of growth capacity — any spike breaches SLA",
                source="batch",
                impact=f"'{tight_name}' has {buf_pct:.1f}% SLA buffer — even a minor data volume increase will cause SLA breach",
                recommendation=f"Profile '{tight_name}' under load; either optimise runtime by 15%+ or request SLA extension",
                root_cause="THIN_SLA_BUFFER")

        # R4b — Job-specific SLA matrix: per-job operational deadline intelligence
        # ─────────────────────────────────────────────────────────────────────────
        # When the uploaded SLA file is a JOB_MATRIX (rows have real job/workflow
        # names, not just DAILY/WEEKLY/MONTHLY groupings), each job has an
        # OPERATIONAL TARGET — distinct from the signed SOW SLA ceiling.
        #
        # Two-tier model:
        #   SOW_SCHEDULE → the signed contract ceiling (e.g. "Daily batch = 6h").
        #                  Breach = contract violation.
        #   JOB_SPECIFIC → individual job completion target (e.g. "Job X by 7 AM").
        #                  Breach = buffer consumed; SOW SLA window at risk.
        #
        # Buffer is the DECISION FACTOR:
        #   If a job-specific SLA is breached today, the engineer must know HOW MUCH
        #   time remains to fix the issue before the SOW SLA ceiling is also violated.
        #   "Job X is 45 min over its target — but you still have 75 min of buffer
        #    before the SOW DAILY 6h ceiling is breached."

        if sla_loaded and top_jobs:
            # Segment jobs by their SLA contract type
            job_specific_jobs  = [j for j in top_jobs
                                   if j.get("sla_contract_type") == "JOB_SPECIFIC"]
            sow_schedule_jobs  = [j for j in top_jobs
                                   if j.get("sla_contract_type") == "SOW_SCHEDULE"]

            if job_specific_jobs:
                # Categorise each job by how much of its individual SLA is consumed
                _job_breach: list   = []  # peak > sla (overrun)
                _job_critical: list = []  # 90–100% consumed
                _job_caution: list  = []  # 75–90% consumed

                for _j in job_specific_jobs:
                    _peak = _f(_j.get("peak_hrs"))
                    _sla  = _f(_j.get("sla_hrs"))
                    if _sla <= 0:
                        continue
                    _buf_hrs  = _sla - _peak
                    _buf_mins = round(_buf_hrs * 60, 0)
                    _buf_pct  = (_buf_hrs / _sla) * 100
                    _j_name   = _j.get("Job_Name") or _j.get("job_name") or "?"
                    _sub_app  = _j.get("Sub_Application") or _j.get("sub_application") or ""
                    _label    = f"{_sub_app}:{_j_name}" if _sub_app else _j_name
                    _over_mins = round(-_buf_mins, 0)   # positive = overrun

                    if _buf_hrs < 0:
                        # Check how much SOW buffer remains (if we know the SOW ceiling)
                        # Use the schedule_type to look up SOW ceiling
                        _sched = _j.get("schedule_type") or "DAILY"
                        _sow_ceiling = get_sla_hrs(_sched, sla_ceil)
                        _sow_remaining_mins = round((_sow_ceiling - _peak) * 60, 0)
                        _sow_risk = (
                            "SOW ceiling ALREADY BREACHED"
                            if _sow_remaining_mins < 0
                            else (
                                f"SOW {_sched} window: {int(_sow_remaining_mins)} min remaining"
                                if _sow_remaining_mins < 60
                                else f"SOW {_sched} window: {_sow_remaining_mins / 60:.1f}h remaining"
                            )
                        )
                        _job_breach.append(
                            (_label, _peak, _sla, int(_over_mins), _sow_risk, _sow_remaining_mins)
                        )
                    elif _buf_pct < 10:
                        _job_critical.append((_label, _peak, _sla, int(_buf_mins)))
                    elif _buf_pct < 25:
                        _job_caution.append((_label, _peak, _sla, int(_buf_mins)))

                if _job_breach:
                    # Sort worst first by overrun magnitude
                    _job_breach.sort(key=lambda x: x[3], reverse=True)
                    _worst = _job_breach[0]
                    _breach_names = "; ".join(
                        f"{x[0]} (+{x[3]}min overrun, {x[4]})"
                        for x in _job_breach[:3]
                    )
                    _sow_risk_any = any(x[4].startswith("SOW ceiling ALREADY") for x in _job_breach)
                    _sow_low_any  = any(isinstance(x[5], (int, float)) and x[5] < 60
                                        for x in _job_breach)
                    _severity = "critical" if (_sow_risk_any or _sow_low_any) else "warning"
                    _icon     = "🚨" if _severity == "critical" else "⏰"
                    add(_severity, _icon,
                        f"{len(_job_breach)} job(s) breached their individual SLA target "
                        f"({'SOW SLA at risk' if _sow_risk_any else 'buffer exhausted'})",
                        f"{_breach_names}"
                        f"{'…' if len(_job_breach) > 3 else ''}. "
                        "These are JOB-SPECIFIC operational targets from the SLA matrix — "
                        "distinct from the SOW-level signed agreement.",
                        source="batch",
                        impact=(
                            "SOW signed agreement is at risk — contractual ceiling breached by one or more jobs"
                            if _sow_risk_any else
                            f"Job '{_worst[0]}' overran its target by {_worst[3]} min. "
                            f"Buffer consumed. {_worst[4]}. "
                            "Sustained overrun will breach the SOW SLA window."
                        ),
                        recommendation=(
                            "Immediately review overrunning jobs. "
                            "Profile for data-volume spike, lock contention, or resource saturation. "
                            "If fix cannot be completed within remaining SOW buffer, "
                            "engage customer to invoke the pre-agreed contingency clause."
                        ),
                        evidence_class="measured",
                        root_cause="JOB_SPECIFIC_SLA_BREACH")

                elif _job_critical:
                    _crit_names = ", ".join(
                        f"{x[0]} ({x[3]}min buffer left)"
                        for x in sorted(_job_critical, key=lambda x: x[3])[:3]
                    )
                    add("warning", "⏰",
                        f"{len(_job_critical)} job(s) critically close to individual SLA target (<10% buffer)",
                        f"Jobs: {_crit_names}{'…' if len(_job_critical) > 3 else ''}. "
                        "Buffer is nearly exhausted — any further delay will breach the job-specific target.",
                        source="batch",
                        impact="Job-specific SLA target almost exhausted. Risk of SOW window overrun if trend continues.",
                        recommendation="Monitor these jobs closely during next batch cycle. "
                                       "Pre-position a fix for the most probable causes "
                                       "(data spike, index fragmentation, lock escalation).",
                        evidence_class="measured",
                        root_cause="JOB_SPECIFIC_SLA_NEAR_BREACH")

                elif _job_caution:
                    _caut_names = ", ".join(
                        f"{x[0]} ({x[3]}min)"
                        for x in sorted(_job_caution, key=lambda x: x[3])[:3]
                    )
                    add("info", "📊",
                        f"{len(_job_caution)} job(s) using >75% of individual SLA target window",
                        f"Jobs: {_caut_names}{'…' if len(_job_caution) > 3 else ''}. "
                        "Still within target but trending toward the limit.",
                        source="batch",
                        evidence_class="measured",
                        root_cause="JOB_SPECIFIC_SLA_CAUTION")

                elif job_specific_jobs:
                    # All job-specific SLA jobs are healthy
                    add("ok", "✅",
                        f"Job-specific SLA targets: all {len(job_specific_jobs)} job(s) within individual targets",
                        "Operational SLA matrix targets are met. Buffer headroom is adequate.",
                        source="batch",
                        evidence_class="measured",
                        root_cause="")

        # R5 — Statistical anomalies (only if regression rules didn't already cover them)
        # The regression rules (later in the pipeline) provide more detail with
        # per-job breakdown, so skip R5 when anomalies are the same data.
        # R5 only fires for anomalies NOT already covered by regression findings.
        _reg_job_names = {
            (r.get("Job_Name") or r.get("job_name") or "?").upper()
            for r in anomalies if _f(r.get("zscore") or r.get("z_score") or 0) > 2.0
        }
        _non_reg_anomalies = [
            a for a in anomalies
            if (a.get("job_name") or a.get("Job_Name") or "?").upper() not in _reg_job_names
        ]
        if _non_reg_anomalies:
            top_a = _non_reg_anomalies[0]
            j_name = top_a.get("job_name") or top_a.get("Job_Name") or "?"
            z      = _f(top_a.get("z_score") or top_a.get("z"))
            add("warning", "📉",
                f"{len(_non_reg_anomalies)} additional statistical anomaly/anomalies in job runtimes",
                f"Top outlier: {j_name} (z={z:.1f}σ) — investigate for runaway jobs",
                source="batch",
                impact=f"'{j_name}' ran {z:.1f} standard deviations above its own historical baseline — potential runaway or data volume spike",
                recommendation=f"Pull Ctrl-M job history for '{j_name}'; check for data volume change, code regression, or lock contention",
                root_cause="JOB_RUNTIME_ANOMALY")

        # R6 — Zero-duration jobs (may be pre-execution failures)
        zero_dur = [
            j for j in top_jobs
            if _f(j.get("peak_hrs")) == 0
            and j.get("buffer_status") not in ("EXCELLENT", "HEALTHY")
        ]
        if zero_dur:
            add("info", "⚡",
                f"{len(zero_dur)} job(s) with zero runtime detected",
                "May indicate pre-execution termination — verify in Ctrl-M console",
                source="batch")

        # R7a — Execution failure rate (ENDED NOT OK / FAILED / ABENDED)
        # Source: fail_rate_pct = fail_runs / total_runs × 100 from batch_calculator.
        # THIS IS NOT SLA compliance.  A job can fail in 0.1s (no SLA impact)
        # or run 12h successfully (SLA breach).  These are completely separate signals.
        exec_fail_pct = _f(bk.get("fail_rate_pct", 0))
        exec_fail_n   = _i(bk.get("failed_runs") or bk.get("fail_runs", 0))

        # R7c — Recurring failure patterns (same job failing on multiple dates)
        # Pre-compute here so we can embed into R7a finding's sub-text.
        recurring_fails: dict[str, int] = {}
        for j in top_jobs:
            fc = _i(j.get("fail_count", 0))
            if fc >= 2:
                j_name = j.get("Job_Name") or j.get("job_name") or "?"
                recurring_fails[j_name] = fc
        _recurring_detail = ""
        if recurring_fails:
            sorted_recurring = sorted(recurring_fails.items(), key=lambda x: -x[1])
            top3 = "; ".join(f"{name} ({cnt}x)" for name, cnt in sorted_recurring[:3])
            _recurring_detail = (
                f" · Repeat offenders: {top3}"
                f"{'…' if len(recurring_fails) > 3 else ''}"
                " — systemic, not one-off."
            )

        if exec_fail_pct > 10:
            add("critical", "💥",
                f"Execution failure rate: {exec_fail_pct:.2f}% — {exec_fail_n} of {total_runs} runs failed",
                f"ENDED NOT OK / FAILED / ABENDED. "
                "Execution failures block downstream dependencies and may not be visible "
                f"in SLA compliance metrics.{_recurring_detail}",
                source="batch",
                impact="High execution failure rate — systemic scheduler, dependency, or infrastructure issue",
                recommendation="Review Ctrl-M logs for all FAILED/ABENDED runs. Check dependency chains, "
                               "resource exhaustion, and code errors. Resolve before PE sign-off.",
                root_cause="EXECUTION_FAILURE_RATE")
        elif exec_fail_pct > 1:
            add("warning", "⚠️",
                f"Execution failure rate: {exec_fail_pct:.2f}% — {exec_fail_n} of {total_runs} runs failed",
                f"{exec_fail_n} failed run(s) detected.{_recurring_detail}",
                source="batch",
                root_cause="EXECUTION_FAILURE_RATE")

        # R7c — standalone recurring failure finding only when failure rate is low
        # (if failure rate already fired as warning/critical, recurring detail is inline)
        if recurring_fails and exec_fail_pct <= 1:
            sorted_recurring = sorted(recurring_fails.items(), key=lambda x: -x[1])
            top3 = "; ".join(f"{name} ({cnt}x)" for name, cnt in sorted_recurring[:3])
            total_recurring = len(recurring_fails)
            add("warning", "🔁",
                f"{total_recurring} job(s) with recurring failures across multiple cycles",
                f"Repeat offenders: {top3}{'…' if total_recurring > 3 else ''}. "
                "These are systemic failures, not one-off glitches — "
                "investigate root cause (FileWatcher timeout, broken dependency, engine start issue).",
                source="batch", evidence_class="measured",
                impact=f"{total_recurring} job(s) failing repeatedly will block PE sign-off and indicate "
                       "unresolved upstream or configuration issues",
                recommendation="For each recurring failure: check Ctrl-M job logs, upstream file delivery, "
                               "engine health status, and dependency chain. Prioritise by failure count.",
                root_cause="RECURRING_FAILURE_PATTERN")

        # R7b — REMOVED: was duplicate of R1b (batch window compliance).
        # R1b already covers batch window overrun with full detail.

        # R8 — Sub-application hotspots
        if sub_stats:
            hot_subs = sorted(
                sub_stats, key=lambda s: _f(s.get("total_hrs")), reverse=True
            )[:3]
            if hot_subs:
                names = ", ".join(
                    s.get("Sub_Application") or s.get("sub_application") or "?"
                    for s in hot_subs
                )
                total_hrs = sum(_f(s.get("total_hrs")) for s in hot_subs)
                top_sub = hot_subs[0]
                top_sub_hrs = _f(top_sub.get("total_hrs"))
                add("info", "📦",
                    f"Top sub-applications by runtime: {names}",
                    f"Combined: {total_hrs:.1f}h — review for parallelisation opportunities",
                    source="batch",
                    impact=f"Top sub-app '{(top_sub.get('Sub_Application') or top_sub.get('sub_application','?'))}' accounts for {top_sub_hrs:.1f}h of batch window — parallelisation could reduce total window by 20-40%",
                    recommendation="Map job dependencies within each sub-app; jobs without shared resources can run concurrently",
                    root_cause="SCHEDULING_INEFFICIENCY")

    # ═══════════════════════════════════════════════════════════════
    # RESOURCE / INFRASTRUCTURE RULES
    # ═══════════════════════════════════════════════════════════════
    if has_resource:
        grade   = rk.get("fleet_grade", "?")
        fscore  = _f(rk.get("fleet_score"))
        crit    = _i(rk.get("n_critical"))
        warn    = _i(rk.get("n_warning"))
        healthy = _i(rk.get("n_healthy"))
        total_s = _i(rk.get("total_servers", len(servers)))
        data_q  = rk.get("data_quality", "")
        r_anoms = rk.get("anomalies") or []
        n_agg   = _i(rk.get("n_agg_trap"))
        n_dual  = _i(rk.get("n_dual_pressure"))

        # Check if all servers have 0.0% metrics (IMAGE_DOCX not yet vision-enriched)
        # Uses effective_cpu (agg-adjusted) first, then cpu_pct; mem_pct for memory
        all_zero = all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        ) if servers else not rk

        # Determine evidence class for resource findings
        # Snapshot-based documents (PDF/DOCX) are "inferred", not time-series "measured"
        res_evidence_class = "inferred" if not all_zero else "unavailable"

        if grade == "N/A" or all_zero:
            try:
                from services import config_store as _cs_vision
                _has_vision_key = bool(_cs_vision.get_gemini_key())
            except Exception:
                _has_vision_key = False
            if _has_vision_key:
                _res_sub = ("Metrics are embedded as images and could not be read from text. "
                            "A Gemini Vision key is configured — retry, or re-upload a text-based report.")
                _res_rec = ("Re-run extraction (Vision key present), or re-upload a resource report "
                            "with selectable text / numeric tables.")
            else:
                _res_sub = ("Metrics are embedded as images — no text values to parse, and no Gemini "
                            "Vision key is configured to read them. Configure a Vision key in Settings "
                            "or re-upload a text-based report.")
                _res_rec = ("Configure a Gemini Vision API key in Settings to extract image-based "
                            "metrics, or re-upload a resource report with selectable text / numeric tables.")
            add("warning", "📡",
                f"Fleet Grade N/A — resource metrics unavailable ({total_s} servers, all 0.0%)",
                _res_sub,
                source="resource", evidence_class="unavailable",
                impact="Cannot perform infrastructure readiness assessment — fleet status unknown",
                recommendation=_res_rec,
                root_cause="MONITORING_GAP")
        elif crit > 0:
            real_crit = crit  # will adjust for agg traps below
            agg_note = ""
            if n_agg > 0:
                agg_note = f" ({n_agg} high-CPU reading(s) identified as aggregation artifacts)"
            crit_hosts = [s.get('host','?').split('.')[0] for s in servers
                          if s.get('state','') == 'CRITICAL' or
                          _f(s.get('effective_cpu') or s.get('cpu_pct') or 0) >= 85 or
                          _f(s.get('mem_pct') or 0) >= 90][:3]
            host_str = f" · Hosts: {', '.join(crit_hosts)}" if crit_hosts else ""
            add("critical", "🖥️",
                f"{crit} server(s) in CRITICAL state — fleet grade {grade}{agg_note}",
                f"CPU/Memory/Disk at or above critical threshold{host_str}. "
                "Document-derived snapshot — escalate before production cutover.",
                source="resource", evidence_class=res_evidence_class,
                impact=f"Fleet grade {grade} — {crit} server(s) at critical threshold will degrade batch throughput and risk job failures under peak load",
                recommendation="Engage infrastructure owner immediately; scale capacity or reduce concurrent job load before cutover",
                root_cause="RESOURCE_SATURATION")
        elif warn > 0:
            add("warning", "🖥️",
                f"{warn} server(s) in WARNING state — fleet grade {grade} (score {fscore:.0f}/100)",
                "Resource utilization approaching thresholds — "
                "snapshot-based assessment, not continuous monitoring",
                source="resource", evidence_class=res_evidence_class,
                impact=f"{warn} server(s) approaching critical threshold — risk of degradation under peak batch load",
                recommendation="Monitor servers during next batch window; engage infra owner if trend continues",
                root_cause="RESOURCE_PRESSURE")
        else:
            add("ok", "🖥️",
                f"Fleet health grade {grade} ({fscore:.0f}/100) — all servers within thresholds",
                f"{healthy}/{total_s} servers healthy"
                + (f" · {n_agg} false alarm(s) filtered" if n_agg else "")
                + " (document-derived snapshot)",
                source="resource", evidence_class=res_evidence_class,
                root_cause="")

        # ── RULE 1: Aggregation Trap Detection (Max vs Avg) ──────
        # When Max CPU is ≥85% but Avg is <20%, the spike is a visual
        # artifact — the server is actually HEALTHY.
        agg_trap_servers = [s for s in servers if s.get("agg_trap")]
        if agg_trap_servers and not all_zero:
            names = ", ".join(
                f"{s.get('host','?').split('.')[0]} (Peak {_f(s.get('cpu_pct')):.0f}%/Eff {_f(s.get('effective_cpu')):.0f}%)"
                for s in agg_trap_servers[:3]
            )
            add("info", "🔬",
                f"{len(agg_trap_servers)} server(s) flagged as Aggregation Trap — FALSE ALARM",
                f"{names}{'…' if len(agg_trap_servers) > 3 else ''} — "
                f"Max CPU is high but Avg is very low (<20%). "
                f"This is a visual aggregation artifact from 7/30-day charts, not sustained pressure. "
                f"Servers are HEALTHY.",
                source="resource", evidence_class=res_evidence_class)

        # ── RULE 2: Role-Specific CPU Thresholds ─────────────────
        # APP servers: alarm at 60%+, DB: expect batch spikes (85%+),
        # SRE: only alarm on sustained 90%+ collision saturation.
        if not all_zero:
            _role_thresholds = {
                "APP": {"ok": 60.0, "warn": 80.0, "label": "should rarely exceed 60%"},
                "DB":  {"ok": 85.0, "warn": 95.0, "label": "batch optimization spikes expected"},
                "SRE": {"ok": 90.0, "warn": 100.0, "label": "watch for concurrent job collisions"},
            }
            for role, thresh in _role_thresholds.items():
                role_servers = [
                    s for s in servers
                    if (s.get("type") or "APP").upper() == role
                    and not s.get("agg_trap")
                    and _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) >= thresh["ok"]
                ]
                if role_servers:
                    names = ", ".join(
                        f"{s.get('host','?').split('.')[0]} ({_f(s.get('effective_cpu') or s.get('cpu_pct') or 0):.0f}%)"
                        for s in role_servers[:3]
                    )
                    level = "critical" if any(
                        _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) >= thresh["warn"]
                        for s in role_servers
                    ) else "warning"
                    add(level, "🏷️",
                        f"{len(role_servers)} {role} server(s) above role-specific CPU threshold",
                        f"{names}{'…' if len(role_servers) > 3 else ''} — "
                        f"{role} servers: {thresh['label']} (snapshot-based, not time-series validated)",
                        source="resource", evidence_class=res_evidence_class,
                        impact=f"{role} server CPU above operational limit — batch jobs on these hosts will compete for CPU and may breach SLA",
                        recommendation=f"Review {role} server scheduling; either reduce concurrent job count or scale CPU capacity",
                        root_cause="CPU_ROLE_BREACH")

        # ── RULE 3: Dual CPU + Memory Pressure (≥80% CPU + ≥85% Mem) ──
        # High CPU alone = working hard.  High CPU + High Memory (>85%)
        # = severe resource exhaustion, swapping, or undersized server.
        dual_hot = [s for s in servers if s.get("dual_pressure")]
        if dual_hot and not all_zero:
            names = ", ".join(s.get("host", "?").split(".")[0] for s in dual_hot[:2])
            add("critical", "⚡",
                f"{len(dual_hot)} server(s) under DUAL CPU+Memory pressure (CPU≥80% + Mem≥85%)",
                f"Hosts: {names}{'…' if len(dual_hot) > 2 else ''} — "
                f"severe resource exhaustion detected. Likely swapping or undersized server. "
                f"Must be resolved pre-cutover.",
                source="resource", evidence_class=res_evidence_class,
                impact="Dual CPU+Memory pressure causes OS memory swapping — batch jobs will stall, fail, or produce timeout SLA breaches",
                recommendation="Immediately size-up affected servers or migrate heavy jobs to alternate hosts; this is a pre-cutover blocker",
                root_cause="DUAL_PRESSURE")

        # CPU saturation — individual server rules (>= 90%, excluding agg traps)
        cpu_hot = [
            s for s in servers
            if _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) >= 90
            and not s.get("agg_trap")
        ]
        if cpu_hot and not all_zero:
            names = ", ".join(s.get("host", "?") for s in cpu_hot[:3])
            add("critical", "🔥",
                f"{len(cpu_hot)} server(s) at CPU saturation (≥ 90%)",
                f"Hosts: {names}{'…' if len(cpu_hot) > 3 else ''} — batch scheduling will be impacted "
                f"(snapshot-based, not time-series validated)",
                source="resource", evidence_class=res_evidence_class,
                impact="CPU saturation will cause job queue build-up and extend batch window beyond SLA",
                recommendation="Add CPU capacity or spread jobs across more nodes; review job concurrency settings",
                root_cause="CPU_SATURATION")
        elif not all_zero:
            cpu_warn = [
                s for s in servers
                if 75 <= _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) < 90
                and not s.get("agg_trap")
            ]
            if cpu_warn:
                names = ", ".join(s.get("host", "?") for s in cpu_warn[:3])
                add("warning", "🔶",
                    f"{len(cpu_warn)} server(s) with CPU warning (75–89%)",
                    f"Hosts: {names}{'…' if len(cpu_warn) > 3 else ''} — watch during peak batch window",
                    source="resource", evidence_class=res_evidence_class,
                    impact="Elevated CPU may cause job slowdowns and SLA risk during concurrent batch peaks",
                    recommendation="Monitor these hosts during the next batch window; set alerts at 85% sustained CPU",
                    root_cause="CPU_PRESSURE")

        # Memory pressure — >= 80%, but exclude DB servers in expected SGA/PGA band
        # (80–92% used = 8–20% available is normal for Oracle/SQL DB workloads).
        # DB servers above 92% used (< 8% available) are still flagged.
        mem_hot = [
            s for s in servers
            if _f(s.get("mem_pct")) >= 80
            and not (
                (s.get("type") or "APP").upper() == "DB"
                and _f(s.get("mem_pct")) <= 92.0
            )
        ]
        if mem_hot and not all_zero:
            names = ", ".join(s.get("host", "?") for s in mem_hot[:3])
            add("warning", "💾",
                f"{len(mem_hot)} server(s) under memory pressure (≥ 80%)",
                f"Hosts: {names}{'…' if len(mem_hot) > 3 else ''} — risk of OOM during batch peak",
                source="resource", evidence_class=res_evidence_class,
                impact="High memory consumption increases OOM kill risk — batch jobs may fail mid-run and not be retried",
                recommendation="Tune JVM heap / process memory limits; review in-memory caching strategies for batch jobs",
                root_cause="MEMORY_PRESSURE")

        # DB memory band alert — DB servers above expected range (> 92% used / < 8% available)
        db_mem_high = [
            s for s in servers
            if (s.get("type") or "APP").upper() == "DB"
            and _f(s.get("mem_pct")) > 92.0
        ]
        if db_mem_high and not all_zero:
            names = ", ".join(
                f"{s.get('host','?').split('.')[0]} ({_f(s.get('mem_pct')):.0f}% used)"
                for s in db_mem_high[:3]
            )
            add("critical", "💾",
                f"{len(db_mem_high)} DB server(s) above expected memory band (> 92% used / < 8% available)",
                f"Hosts: {names}{'…' if len(db_mem_high) > 3 else ''}. "
                f"DB expected band is 80–92% used (SGA/PGA allocation). "
                f"Above 92% indicates possible memory leak, PGA over-allocation, or VM under-provisioning.",
                source="resource", evidence_class=res_evidence_class,
                impact="DB server RAM exhausted beyond SGA/PGA expected range — risk of paging, OOM, and batch job failure",
                recommendation="Review PGA_AGGREGATE_LIMIT, check for memory leaks; consider VM right-sizing",
                root_cause="DB_MEM_OVERRUN")

        # Disk usage — >= 85%
        disk_hot = [s for s in servers if _f(s.get("disk_used_max")) >= 85]
        if disk_hot and not all_zero:
            names = ", ".join(s.get("host", "?") for s in disk_hot[:3])
            add("critical", "💿",
                f"{len(disk_hot)} server(s) with critical disk usage (≥ 85%)",
                f"Hosts: {names}{'…' if len(disk_hot) > 3 else ''} — batch spool jobs may fail",
                source="resource", evidence_class=res_evidence_class)

        # ── Disk monitoring gap ───────────────────────────────────────────────
        # Servers with no disk I/O metric at all cannot be assessed for disk
        # health. Flag as a DATA_GAP finding so the PE audit is not silent.
        disk_gap = [
            s for s in servers
            if not s.get("disk_available") and not s.get("image_only")
        ]
        if disk_gap and not all_zero:
            gap_names = ", ".join(s.get("host", "?").split(".")[0] for s in disk_gap[:3])
            gap_more  = f" +{len(disk_gap) - 3} more" if len(disk_gap) > 3 else ""
            add("warning", "💿",
                f"Disk I/O data missing for {len(disk_gap)} server(s) — disk health cannot be assessed",
                f"Servers: {gap_names}{gap_more}. "
                f"Azure Monitor is not collecting 'OS Disk Bandwidth Consumed Percentage' for these hosts.",
                source="resource", evidence_class=res_evidence_class,
                impact=f"{len(disk_gap)} server(s) cannot be audited for disk pressure — PE assessment is incomplete for the disk pillar",
                recommendation=(
                    "Configure Azure Monitor to collect 'OS Disk Bandwidth Consumed Percentage' "
                    "for all in-scope servers. In Azure Portal: Monitor → Metrics → select VM → "
                    "OS Disk Bandwidth Consumed Percentage."
                ),
                root_cause="DISK_MONITORING_GAP")

        # Infrastructure anomalies
        for ia in (r_anoms or [])[:2]:
            add("info", "📊",
                f"Infra anomaly: {ia.get('host','?')} — {ia.get('metric','?')} "
                f"at {fmt_pct(_f(ia.get('value')))}",
                f"z-score {_f(ia.get('z')):.1f}σ above fleet average",
                source="resource")

        # Data quality warning
        if data_q == "INSUFFICIENT":
            known_pct = _f(rk.get("known_pct"))
            add("info", "ℹ️",
                f"Resource data quality: INSUFFICIENT ({known_pct:.0f}% of servers have real metrics)",
                "Fleet grade and correlation analysis may be unreliable until more data is extracted",
                source="resource")

        # ── Azure evidence upgrade: if Azure Monitor data is loaded alongside
        # document-based resource data, upgrade evidence class to "measured" and
        # add confidence note — the document snapshot is now corroborated ──
        if req.deep_dive and (req.deep_dive.get("vm_count") or 0) > 0 and not all_zero:
            _dd_vmc = req.deep_dive.get("vm_count", 0)
            _dd_hrs = req.deep_dive.get("hours_back", 24)
            _dd_bl  = (req.deep_dive.get("baseline") or {}).get("days_observed", 0)
            for f in findings:
                if f.source == "resource" and f.evidence_class == "inferred":
                    f.evidence_class = "measured"
                    f.confidence = max(f.confidence, 90)
            if _dd_bl >= 15:
                add("ok", "🔗",
                    f"Resource evidence upgraded: {_dd_vmc} VMs with {_dd_bl:.0f}-day Azure Monitor baseline",
                    f"Document-derived resource findings are now corroborated by {_dd_hrs}h of "
                    f"Azure Monitor time-series data across {_dd_vmc} VMs. Evidence class upgraded "
                    f"from 'inferred' to 'measured'. PE findings carry full audit confidence.",
                    source="resource", confidence=98, evidence_class="measured",
                    root_cause="")

    # ═══════════════════════════════════════════════════════════════
    # AZURE MONITOR DEEP DIVE — TIME-SERIES EVIDENCE RULES
    # ═══════════════════════════════════════════════════════════════
    dd = req.deep_dive or {}
    dd_vms = dd.get("per_vm") or []
    dd_total = dd.get("total_critical", 0)
    dd_affected = dd.get("affected_vms", 0)
    dd_hours = dd.get("hours_back", 24)

    if dd_total > 0 and dd_vms:
        # Upgrade evidence class from "inferred" to "measured" for resource findings
        # when time-series data corroborates snapshot values

        # RULE DD1: Fleet-wide spike summary
        add("critical" if dd_total >= 5 else "warning", "📡",
            f"{dd_total} critical anomal{'ies' if dd_total > 1 else 'y'} detected across "
            f"{dd_affected} VM{'s' if dd_affected > 1 else ''} in last {dd_hours}h "
            f"(Azure Monitor time-series, z-score ≥ 3σ)",
            "Time-series evidence confirms resource pressure is real, not a snapshot artifact. "
            "Review spike timing against batch schedule to isolate root cause.",
            source="resource",
            confidence=95,
            evidence_class="measured")

        # RULE DD2: Per-VM critical servers with rising trend
        for vm in dd_vms[:5]:
            vm_name = vm.get("vm", "?")
            role = vm.get("role", "SERVER")
            sc = vm.get("spike_count", 0)
            trend = vm.get("trend", "flat")
            mem = vm.get("mem_used_max")
            cpu = vm.get("cpu_max")

            if sc == 0:
                continue

            # Determine dominant metric
            dom_metric = "CPU"
            dom_val = cpu or 0
            if mem and (mem > (cpu or 0)):
                dom_metric = "MEM"
                dom_val = mem

            sev = "critical" if dom_val >= 90 or trend == "rising" else "warning"

            trend_text = ""
            if trend == "rising":
                trend_text = " — TREND RISING (accelerating toward breach)"
            elif trend == "recovering":
                trend_text = " — trend recovering"

            add(sev, "🔥",
                f"{vm_name} ({role}): {dom_metric} peak {dom_val:.0f}%, "
                f"{sc} critical spike{'s' if sc > 1 else ''} in {dd_hours}h{trend_text}",
                f"Time-series validated — not a snapshot artifact. "
                f"{'Immediate investigation required: ' if sev == 'critical' else ''}"
                f"{'Check SGA/PGA sizing or VM memory allocation' if dom_metric == 'MEM' else 'Profile top SQL, check parallel degree and vCPU sizing'}.",
                source="resource",
                confidence=95,
                evidence_class="measured",
                root_cause=f"{dom_metric.lower()}_pressure")

        # RULE DD3: Recurring spike patterns (scheduled job signal)
        for vm in dd_vms[:5]:
            spikes = vm.get("spikes", [])
            if len(spikes) < 2:
                continue
            # Check for same-metric recurrence across different days
            by_metric: dict = {}
            for s in spikes:
                m = s.get("metric", "")
                by_metric.setdefault(m, []).append(s)
            for metric, spike_list in by_metric.items():
                if len(spike_list) < 2:
                    continue
                days = set()
                for s in spike_list:
                    pt = s.get("peak_time", "")
                    if pt:
                        try:
                            from datetime import datetime
                            days.add(datetime.fromisoformat(pt.replace("Z", "+00:00")).strftime("%a"))
                        except Exception:
                            pass
                if len(days) >= 2:
                    metric_short = metric.split("Percentage")[-1].strip() or metric.split("Consumed")[-1].strip() or metric
                    add("warning", "🔄",
                        f"{vm.get('vm','?')}: {metric_short} spikes repeat across {'/'.join(sorted(days))} — "
                        f"scheduled job pattern detected ({len(spike_list)} occurrences)",
                        "This is a recurring batch-driven event, not a random incident. "
                        "Investigate the job schedule and consider staggering or resource isolation.",
                        source="resource",
                        confidence=90,
                        evidence_class="measured",
                        root_cause="batch_schedule")

    # ═══════════════════════════════════════════════════════════════
    # AZURE BASELINE INTELLIGENCE — 15-DAY PATTERN ANALYSIS
    # ═══════════════════════════════════════════════════════════════
    # When ≥2 days of time-series data are available, analyze behavioral
    # patterns that point-in-time snapshots and short-window spikes miss.
    # These rules establish STATISTICAL CONFIDENCE for PE judgments:
    # - Is this a one-off or a pattern?
    # - Is infrastructure trending worse or stable?
    # - Are spikes time-correlated with batch schedules?
    # - Does weekday load differ from weekend?
    dd_baseline = dd.get("baseline") or {}
    dd_base_vms = dd_baseline.get("per_vm") or {}
    dd_base_fleet = dd_baseline.get("fleet") or {}
    dd_days_observed = _f(dd_baseline.get("days_observed", 0))
    dd_sufficient = dd_baseline.get("sufficient_baseline", False)

    if dd_base_vms:
        # RULE DD4: Baseline observation window assessment
        if dd_days_observed >= 15:
            add("ok", "📊",
                f"Infrastructure baseline: {dd_days_observed:.0f} days of Azure Monitor data — "
                f"sufficient for PE judgment",
                f"15+ days of continuous metrics provide statistical confidence for trend analysis, "
                f"pattern detection, and capacity forecasting. All baseline-derived findings use "
                f"measured time-series evidence, not document snapshots.",
                source="resource", confidence=98,
                evidence_class="measured",
                root_cause="")
        elif dd_days_observed >= 7:
            add("info", "📊",
                f"Infrastructure baseline: {dd_days_observed:.0f} days of data — "
                f"adequate but 15 days recommended for full PE confidence",
                f"Current observation window captures weekday + weekend patterns but may miss "
                f"monthly batch cycles. Extend to 15 days for definitive PE assessment.",
                source="resource", confidence=80,
                evidence_class="measured",
                root_cause="INSUFFICIENT_BASELINE")
        elif dd_days_observed >= 2:
            add("warning", "📊",
                f"Infrastructure baseline: only {dd_days_observed:.0f} days of data — "
                f"insufficient for PE judgment",
                f"Minimum 15 days required to establish reliable baselines. With {dd_days_observed:.0f} days, "
                f"spike patterns cannot be distinguished from normal variance. "
                f"Select '15d' in the Deep Dive time range to collect adequate baseline.",
                source="resource", confidence=50,
                evidence_class="inferred",
                impact="PE infrastructure verdict has low confidence — patterns may be misidentified",
                recommendation="Re-fetch Azure time-series with 15-day window before making capacity decisions",
                root_cause="INSUFFICIENT_BASELINE")

        # RULE DD5: Per-VM hot hours — consistent pressure at specific times
        # This is the batch-schedule fingerprint: if CPU is consistently >80%
        # at 2-4 AM across 40%+ of observed days, that IS the batch window impact.
        for vm_name, metrics in dd_base_vms.items():
            for metric_name, mdata in metrics.items():
                hot_hours = mdata.get("hot_hours") or []
                if not hot_hours:
                    continue

                # Summarize hot windows (group consecutive hours)
                hours_sorted = sorted(h["hour"] for h in hot_hours)
                windows = []
                window_start = hours_sorted[0]
                window_end = hours_sorted[0]
                for h in hours_sorted[1:]:
                    if h == window_end + 1:
                        window_end = h
                    else:
                        windows.append((window_start, window_end))
                        window_start = h
                        window_end = h
                windows.append((window_start, window_end))

                window_strs = []
                for ws, we in windows:
                    if ws == we:
                        window_strs.append(f"{ws:02d}:00")
                    else:
                        window_strs.append(f"{ws:02d}:00–{we + 1:02d}:00")

                worst_hh = max(hot_hours, key=lambda h: h["avg"])
                avg_val = worst_hh["avg"]
                ratio = worst_hh["breach_ratio"]
                samples = worst_hh["samples"]

                metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")
                sev = "critical" if avg_val >= 90 or ratio >= 0.7 else "warning"

                add(sev, "🕐",
                    f"{vm_name}: {metric_short} pressure at {', '.join(window_strs)} — "
                    f"avg {avg_val:.0f}%, breaching {ratio * 100:.0f}% of observations",
                    f"Consistent {metric_short} elevation during these hours across "
                    f"{dd_days_observed:.0f} days of monitoring ({samples} samples). "
                    f"This is a repeatable pattern, not a random spike. "
                    f"Correlate with Ctrl-M batch schedule to identify the triggering job(s).",
                    source="resource", confidence=92,
                    evidence_class="measured",
                    impact=f"Predictable {metric_short} pressure window — capacity is systematically "
                           f"insufficient during these hours",
                    recommendation=f"Map batch jobs scheduled between {window_strs[0]} and cross-reference "
                                   f"with this VM's {metric_short} spike profile. Either reschedule, "
                                   f"optimize the triggering job, or scale the VM for peak demand.",
                    root_cause="RECURRING_PRESSURE_WINDOW")

        # RULE DD6: Trend acceleration — metrics getting worse over the observation window
        for vm_name, metrics in dd_base_vms.items():
            for metric_name, mdata in metrics.items():
                trend_dir = mdata.get("trend_direction", "stable")
                trend_delta = _f(mdata.get("trend_delta", 0))
                trend_pct = _f(mdata.get("trend_pct", 0))
                overall_mean = _f(mdata.get("overall_mean", 0))

                if trend_dir != "rising" or abs(trend_delta) < 3:
                    continue

                metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")
                sev = "critical" if trend_pct > 15 or overall_mean > 80 else "warning"

                add(sev, "📈",
                    f"{vm_name}: {metric_short} trending upward — "
                    f"+{trend_delta:.1f}pp ({trend_pct:+.1f}%) over {dd_days_observed:.0f} days",
                    f"First-half average vs second-half average shows {metric_short} is increasing. "
                    f"Current overall mean: {overall_mean:.1f}%. "
                    f"At this rate, the server will breach critical thresholds "
                    f"within {max(1, round((90 - overall_mean) / max(trend_delta / max(dd_days_observed / 2, 1), 0.01)))}"
                    f" days if trend continues.",
                    source="resource", confidence=85,
                    evidence_class="measured",
                    impact=f"{metric_short} is on an upward trajectory — capacity will be exhausted "
                           f"if the growth rate is not addressed",
                    recommendation=f"Investigate what is driving {metric_short} growth on {vm_name}: "
                                   f"increasing data volumes, new batch jobs, or degrading query plans. "
                                   f"Scale proactively before the trend reaches critical threshold.",
                    root_cause="TREND_ACCELERATION")

        # RULE DD7: Weekday vs weekend divergence — batch vs non-batch load signature
        for vm_name, metrics in dd_base_vms.items():
            for metric_name, mdata in metrics.items():
                weekday_avg = _f(mdata.get("weekday_avg", 0))
                weekend_avg = _f(mdata.get("weekend_avg", 0))
                divergence = _f(mdata.get("divergence", 0))

                if divergence < 15 or dd_days_observed < 7:
                    continue

                metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")
                higher = "weekday" if weekday_avg > weekend_avg else "weekend"
                lower = "weekend" if higher == "weekday" else "weekday"
                high_val = max(weekday_avg, weekend_avg)
                low_val = min(weekday_avg, weekend_avg)

                sev = "warning" if high_val > 75 else "info"

                add(sev, "📅",
                    f"{vm_name}: {metric_short} diverges {divergence:.0f}pp between "
                    f"weekday ({weekday_avg:.0f}%) and weekend ({weekend_avg:.0f}%)",
                    f"Significant {higher} vs {lower} load difference. "
                    f"{higher.capitalize()} average {high_val:.0f}% vs {lower} {low_val:.0f}%. "
                    f"{'This confirms batch scheduling drives the load profile — weekend shows true idle baseline. ' if higher == 'weekday' else 'Weekend load exceeds weekday — check for weekend-only batch jobs or maintenance windows. '}"
                    f"Use {'weekend' if higher == 'weekday' else 'weekday'} baseline ({low_val:.0f}%) as the "
                    f"infrastructure's natural idle state for capacity planning.",
                    source="resource", confidence=88,
                    evidence_class="measured",
                    root_cause="WEEKDAY_WEEKEND_DIVERGENCE")

        # RULE DD8: Chronic pressure — servers at high utilization for many days
        for vm_name, metrics in dd_base_vms.items():
            for metric_name, mdata in metrics.items():
                chronic_days = _i(mdata.get("chronic_pressure_days", 0))
                total_days = _i(mdata.get("total_days", 0))

                if total_days < 3 or chronic_days < 3:
                    continue

                chronic_ratio = chronic_days / total_days
                if chronic_ratio < 0.4:
                    continue

                overall_p95 = _f(mdata.get("overall_p95", 0))
                overall_mean = _f(mdata.get("overall_mean", 0))
                metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")

                sev = "critical" if chronic_ratio >= 0.7 or overall_p95 >= 90 else "warning"

                add(sev, "🔥",
                    f"{vm_name}: CHRONIC {metric_short} pressure — "
                    f"p95 ≥ threshold on {chronic_days}/{total_days} days ({chronic_ratio * 100:.0f}%)",
                    f"This is not spiky — the server is persistently loaded. "
                    f"Overall mean: {overall_mean:.0f}%, p95: {overall_p95:.0f}%. "
                    f"Spikes are symptoms; the baseline itself is the problem. "
                    f"Server is undersized for its workload.",
                    source="resource", confidence=95,
                    evidence_class="measured",
                    impact=f"{vm_name} is chronically loaded — any additional workload will push "
                           f"it into sustained critical state",
                    recommendation=f"Right-size {vm_name}: the VM needs more "
                                   f"{'vCPUs' if 'CPU' in metric_short else 'RAM' if 'Mem' in metric_short else 'disk throughput'}. "
                                   f"Current sizing is insufficient for the observed workload pattern.",
                    root_cause="CHRONIC_PRESSURE")

        # RULE DD9: Multi-day recurring spikes at same hour — definitive batch fingerprint
        for vm_name, metrics in dd_base_vms.items():
            for metric_name, mdata in metrics.items():
                recurring = mdata.get("recurring_spikes") or []
                if not recurring:
                    continue

                for rs in recurring[:3]:
                    hour = rs.get("hour", 0)
                    day_count = rs.get("day_count", 0)
                    day_names = rs.get("day_names", [])
                    worst_peak = _f(rs.get("worst_peak", 0))
                    avg_dur = _f(rs.get("avg_duration_min", 0))

                    if day_count < 2:
                        continue

                    metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")
                    sev = "critical" if worst_peak >= 90 or day_count >= 5 else "warning"
                    days_str = "/".join(day_names) if len(day_names) <= 5 else f"{day_count} days"

                    add(sev, "🔄",
                        f"{vm_name}: {metric_short} spike at ~{hour:02d}:00 repeats across "
                        f"{days_str} ({day_count} occurrences, peak {worst_peak:.0f}%)",
                        f"Average spike duration: {avg_dur:.0f} min. "
                        f"This is a DEFINITIVE scheduled pattern — same time window, same VM, "
                        f"across multiple days. Not random, not a one-off. "
                        f"The triggering job runs at ~{hour:02d}:00 and consumes {metric_short} resources "
                        f"for ~{avg_dur:.0f} min each time.",
                        source="resource", confidence=95,
                        evidence_class="measured",
                        impact=f"Predictable {metric_short} spike at {hour:02d}:00 — "
                               f"any jobs co-scheduled at this time will compete for resources",
                        recommendation=f"Identify the Ctrl-M job scheduled at ~{hour:02d}:00 on {vm_name}. "
                                       f"Options: (1) optimize the job to reduce {metric_short} footprint, "
                                       f"(2) reschedule conflicting jobs to avoid this window, "
                                       f"(3) scale the VM to absorb the peak demand.",
                        root_cause="RECURRING_SPIKE_PATTERN")

        # RULE DD10: Fleet-wide trend assessment
        for metric_name, fleet_data in dd_base_fleet.items():
            fleet_trend = _f(fleet_data.get("fleet_trend_delta", 0))
            fleet_hot = fleet_data.get("fleet_hot_hours") or []

            if abs(fleet_trend) >= 5:
                metric_short = metric_name.replace("Percentage ", "").replace(" Used %", "")
                direction = "rising" if fleet_trend > 0 else "falling"
                sev = "warning" if fleet_trend > 0 and fleet_trend >= 8 else "info"

                add(sev, "📊",
                    f"Fleet {metric_short}: {direction} trend — "
                    f"{fleet_trend:+.1f}pp across {dd_days_observed:.0f} days",
                    f"Fleet-wide average {metric_short} {'increased' if fleet_trend > 0 else 'decreased'} "
                    f"by {abs(fleet_trend):.1f} percentage points over the observation period. "
                    f"{'This is a capacity concern — the entire fleet is trending toward saturation. ' if fleet_trend > 0 else 'Load is decreasing — capacity headroom is improving. '}"
                    f"{'Investigate system-wide causes: data volume growth, new batch processes, or infrastructure changes.' if fleet_trend > 0 else ''}",
                    source="resource", confidence=85,
                    evidence_class="measured",
                    root_cause="FLEET_TREND" if fleet_trend > 0 else "")

            if fleet_hot:
                hours_str = ", ".join(f"{h:02d}:00" for h in sorted(fleet_hot)[:5])
                add("info", "🕐",
                    f"Fleet-wide {metric_name.replace('Percentage ', '').replace(' Used %', '')} "
                    f"hot hours: {hours_str}",
                    f"Multiple VMs show elevated utilization at these hours. "
                    f"This is the fleet's collective batch schedule fingerprint. "
                    f"Capacity planning should ensure sufficient headroom during these windows.",
                    source="resource", confidence=80,
                    evidence_class="measured",
                    root_cause="FLEET_HOT_HOURS")

    # ═══════════════════════════════════════════════════════════════
    # CROSS-SOURCE CORRELATION RULES
    # ═══════════════════════════════════════════════════════════════
    if has_batch and has_resource:
        # Batch + CPU pressure correlation
        all_zero_res = all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        ) if servers else True

        if not all_zero_res and bk.get("jobs_breach", 0) > 0:
            cpu_pct_vals = [_f(s.get("effective_cpu") or s.get("cpu_pct") or 0)
                            for s in servers
                            if _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) > 0]
            if cpu_pct_vals:
                max_cpu = max(cpu_pct_vals)
                if max_cpu >= 80:
                    # Enhance with Azure baseline context if available
                    _bl_note = ""
                    _bl_conf = 75
                    if dd_base_vms:
                        # Check if any VM with CPU pressure also has a rising trend
                        _rising_vms = [
                            vm for vm, metrics in dd_base_vms.items()
                            for mn, md in metrics.items()
                            if "CPU" in mn and md.get("trend_direction") == "rising"
                        ]
                        if _rising_vms:
                            _bl_note = (
                                f" Azure baseline confirms CPU trend is RISING on "
                                f"{len(_rising_vms)} VM(s) — this correlation is strengthening over time."
                            )
                            _bl_conf = 95
                        elif dd_days_observed >= 15:
                            _bl_note = (
                                f" Azure {dd_days_observed:.0f}-day baseline validates this correlation "
                                f"as a persistent pattern, not a transient coincidence."
                            )
                            _bl_conf = 92

                    add("critical", "🔗",
                        f"Batch breaches correlate with server CPU pressure (max {max_cpu:.0f}%)",
                        f"Resource contention is likely contributing to SLA overruns — "
                        f"scale infrastructure or reschedule heaviest jobs.{_bl_note}",
                        source="resource",
                        confidence=_bl_conf,
                        evidence_class="measured" if dd_base_vms else "inferred")

    # ═══════════════════════════════════════════════════════════════
    # CROSS-PILLAR DIAGNOSTIC SCENARIOS
    # When multiple data sources are loaded (or conspicuously absent),
    # synthesise a higher-level diagnosis that individual rules cannot express.
    # Each pattern is mutually exclusive and fires at most once per run.
    # ═══════════════════════════════════════════════════════════════

    # C1 — Batch BREACH + Healthy Infrastructure → root cause is SCHEDULING, not capacity
    if has_batch and has_resource:
        _c1_breach = _i(bk.get("jobs_breach", 0))
        _c1_crit   = _i(rk.get("n_critical", 0))
        _c1_warn   = _i(rk.get("n_warning", 0))
        _c1_grade  = rk.get("fleet_grade", "?")
        _c1_all_z  = all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        ) if servers else True

        if _c1_breach > 0 and _c1_crit == 0 and _c1_warn <= 1 and not _c1_all_z:
            add("warning", "🔬",
                f"DIAGNOSIS: SLA breach with healthy fleet (grade {_c1_grade}) → scheduling/logic issue, not infrastructure",
                f"{_c1_breach} batch breach(es) detected but all servers within normal thresholds "
                f"(fleet grade {_c1_grade}, 0 critical servers). Adding hardware will NOT resolve this. "
                f"Root cause is in job scheduling logic, SQL execution plans, data volume growth, "
                f"or job dependency sequencing.",
                source="batch", evidence_class="measured",
                impact="Misdiagnosing scheduling issues as infrastructure will waste remediation effort and delay sign-off",
                recommendation=(
                    "1) Profile each breaching job's SQL execution plan — compare current vs last-pass plan. "
                    "2) Check for data volume growth in input tables over the breach window. "
                    "3) Review job dependency chains for unnecessary serialisation. "
                    "4) Infrastructure remediation is NOT the first action here."
                ),
                root_cause="SCHEDULING_NOT_INFRA")

    # C2 — Batch within SLA + Critical Infrastructure → hidden SLA risk
    if has_batch and has_resource:
        _c2_breach = _i(bk.get("jobs_breach", 0))
        _c2_atrisk = _i(bk.get("jobs_at_risk", 0))
        _c2_crit   = _i(rk.get("n_critical", 0))
        _c2_all_z  = all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        ) if servers else True

        if _c2_breach == 0 and _c2_crit > 0 and not _c2_all_z:
            add("critical", "⚠️",
                f"HIDDEN RISK: Batch currently within SLA but {_c2_crit} server(s) at critical threshold",
                f"Zero breaches today — but {_c2_crit} server(s) are at critical resource levels. "
                f"The next data volume increase or peak batch load WILL push runtimes above SLA. "
                f"Do NOT sign off until infrastructure is remediated.",
                source="resource", evidence_class="inferred",
                impact="Current compliance is fragile — infrastructure headroom is exhausted. "
                       "Any load increase will convert at-risk jobs to SLA breaches",
                recommendation=(
                    f"Infrastructure remediation required BEFORE sign-off. "
                    f"Identify which {_c2_crit} server(s) are at critical state and scale or redistribute load. "
                    f"Re-run audit after infrastructure stabilisation."
                ),
                root_cause="HIDDEN_INFRA_RISK")

    # C3 — Batch BREACH + Critical Infrastructure → compound risk (two concurrent workstreams needed)
    if has_batch and has_resource:
        _c3_breach = _i(bk.get("jobs_breach", 0))
        _c3_crit   = _i(rk.get("n_critical", 0))
        _c3_all_z  = all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        ) if servers else True

        if _c3_breach > 0 and _c3_crit > 0 and not _c3_all_z:
            add("critical", "🚨",
                f"COMPOUND RISK: Batch SLA breach ({_c3_breach} job(s)) AND infrastructure critical ({_c3_crit} server(s))",
                f"Both batch scheduling and infrastructure are failing simultaneously. "
                f"Resolving only one workstream may not be sufficient to achieve full compliance. "
                f"Two parallel tracks are required: (A) infrastructure scale-up, (B) job optimisation.",
                source="batch", evidence_class="measured",
                impact="Compound risk is multiplicative — batch jobs failing on degraded infrastructure "
                       "produce unpredictable SLA outcomes that are harder to attribute and fix",
                recommendation=(
                    "Engage TWO workstreams in parallel: "
                    "(A) Infrastructure: scale critical servers immediately, target fleet grade A. "
                    "(B) Scheduling: profile breaching jobs for SQL, data volume, and dependency issues. "
                    "Do not defer infrastructure track while waiting for job optimisation results."
                ),
                root_cause="COMPOUND_RISK")

    # C4 — Batch BREACH + No Resource Data → cannot rule out infrastructure as root cause
    if has_batch and not has_resource:
        _c4_breach = _i(bk.get("jobs_breach", 0))
        _c4_atrisk = _i(bk.get("jobs_at_risk", 0))
        if _c4_breach > 0:
            add("warning", "🔍",
                f"INCOMPLETE DIAGNOSIS: {_c4_breach} SLA breach(es) — no resource data to corroborate root cause",
                f"{_c4_breach} job(s) breached SLA but no infrastructure report has been uploaded. "
                f"Cannot determine whether root cause is infrastructure pressure, scheduling logic, "
                f"SQL regression, or data volume growth. The PE finding is incomplete.",
                source="batch", evidence_class="inferred",
                impact="Without resource data, the SLA breach root cause cannot be confirmed. "
                       "Customer may challenge audit defensibility",
                recommendation=(
                    "Upload the server resource report (CPU/Memory/Disk for all batch hosts). "
                    "If resource data is unavailable, document the infrastructure gap explicitly in the PE report "
                    "and obtain customer acknowledgement before sign-off."
                ),
                root_cause="INCOMPLETE_DIAGNOSIS")

    # C5 — Volume-to-batch capacity correlation (SOW DFU vs actual batch throughput)
    # When SOW baseline AND batch data are both available, compute throughput utilisation.
    if has_batch:
        try:
            from services import config_store as _sow_cs
            _sow_bl = _sow_cs.get("sow_baseline") or {}
            _daily_dfu = _f(_sow_bl.get("daily_dfu") or 0)
            _daily_sku = _f(_sow_bl.get("daily_sku") or 0)
            _total_runs = _i(bk.get("total_runs") or 0)
            _total_jobs = _i(bk.get("total_jobs") or 0)
            # date_span: prefer batch_cov (session-enriched), then bk.data_coverage, then fallback
            _date_span  = _i((batch_cov or {}).get("date_span_days")
                             or (bk.get("data_coverage") or {}).get("date_span_days")
                             or 1)
            # Unique run dates gives more meaningful daily average for weekly batches
            _unique_dates = len(set(
                w.get("run_date") for w in window_data if w.get("run_date")
            )) if window_data else _date_span
            _avg_daily_runs = round(_total_runs / max(_unique_dates, 1), 1)
            if _daily_dfu > 0 and _total_runs > 0:
                _dfu_per_run = round(_daily_dfu / max(_avg_daily_runs, 1))
                add("info", "📦",
                    f"Volume-to-batch ratio: ~{_dfu_per_run:,.0f} DFU per batch run "
                    f"({_daily_dfu:,.0f} daily DFU ÷ {_avg_daily_runs:.1f} avg daily runs)",
                    f"Contracted capacity: {_daily_dfu:,.0f} DFU/day"
                    + (f" · {_daily_sku:,.0f} SKU/day" if _daily_sku > 0 else "")
                    + f". Batch dataset: {_total_jobs} jobs, {_total_runs} runs across "
                    f"{_unique_dates} unique run date(s) in a {_date_span}-day window.",
                    source="sow", evidence_class="measured",
                    impact="Understanding DFU load per job helps isolate which jobs will breach SLA as volume grows",
                    recommendation=(
                        "Map highest-DFU sub-applications to their runtime. "
                        "Jobs processing disproportionate DFU relative to their SLA window are prime candidates "
                        "for parallelisation or load balancing."
                    ),
                    root_cause="VOLUME_CAPACITY_RATIO")
        except Exception:
            pass

    # C6 — Waiver-adjusted verdict (reduce effective breach count by documented waivers)
    if has_batch:
        try:
            from services import config_store as _wv_cs
            _sla_intel = _wv_cs.get("_sla_intelligence") or {}
            _wv_contracts = _sla_intel.get("contracts") or []
            _waiver_kw = {"waiver", "waived", "exception", "approved exception",
                          "no breach", "agreed", "unofficial", "not enforced",
                          "excluded", "exempted", "customer approved"}
            _waived_jobs: set[str] = set()
            for _wv_c in _wv_contracts:
                _combined = (((_wv_c.get("comments") or "") + " " +
                              (_wv_c.get("interpretation_notes") or ""))).lower()
                if any(k in _combined for k in _waiver_kw):
                    _bn = _wv_c.get("batch_name") or ""
                    if _bn:
                        _waived_jobs.add(_bn)

            _total_breaches = _i(bk.get("jobs_breach") or 0)
            _eff_breaches = max(0, _total_breaches - len(_waived_jobs))
            if _waived_jobs and _total_breaches > 0:
                _waived_str = ", ".join(sorted(_waived_jobs)[:5])
                if _eff_breaches == 0:
                    _verdict_adj = "CONDITIONAL (all breaches have documented waivers)"
                elif _eff_breaches < _total_breaches:
                    _verdict_adj = f"still BLOCKED ({_eff_breaches} unwaived breach(es) remain)"
                else:
                    _verdict_adj = "BLOCKED (no waivers reduce breach count)"
                add("info" if _eff_breaches == 0 else "warning", "📜",
                    f"Waiver-adjusted verdict: {_total_breaches} breach(es) — "
                    f"{len(_waived_jobs)} with waiver language → {_eff_breaches} unresolved",
                    f"Jobs with waiver language in SLA matrix: {_waived_str}. "
                    f"Effective unresolved breach count: {_eff_breaches}. "
                    f"Adjusted verdict: {_verdict_adj}. "
                    f"Confirm each waiver is customer-signed before citing this in the PE report.",
                    source="sla", evidence_class="inferred",
                    impact=f"Waiver accounting changes the sign-off verdict from BLOCKED to {_verdict_adj}",
                    recommendation=(
                        "Obtain formal customer sign-off documents for each waiver. "
                        "Attach to the PE report as evidence. "
                        "If waivers are verbal only, they are NOT audit-defensible."
                    ),
                    root_cause="WAIVER_ADJUSTED_VERDICT")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════
    # SESSION-CACHE ENRICHMENT — accuracy safety net
    # ═══════════════════════════════════════════════════════════════
    # Pull all dead-cache keys written by batch/sla_matrix/sow routers that
    # were never forwarded by the client payload. This is the single canonical
    # place where server-side computed data is injected into findings so the
    # rule engine always operates on the latest numbers regardless of UI state.
    try:
        from services import session_cache as _sc

        # ── SLA ceilings from uploaded SLA XLSX ───────────────────
        # When sla_ceilings are not in the request payload (frontend
        # didn't include them), pull from config_store where upload.py persists them.
        if not req.sla_ceilings:
            try:
                from services import config_store as _ceil_cs
                _ceil_map: dict[str, float] = {}
                _SLA_KEY_MAP_R = {
                    "daily_sla_hrs": "DAILY",
                    "weekly_sla_hrs": "WEEKLY",
                    "monthly_sla_hrs": "MONTHLY",
                    "custom_sla_hrs": "CUSTOM",
                }
                for _cfg_key, _sched_type in _SLA_KEY_MAP_R.items():
                    _v = _ceil_cs.get(_cfg_key)
                    if _v is not None and _f(_v) > 0:
                        _ceil_map[_sched_type] = _f(_v)
                if _ceil_map:
                    req.sla_ceilings = _ceil_map
            except Exception:
                pass

        # ── SLA matrix enrichment ─────────────────────────────────
        if not req.sla_matrix:
            _wf_cache  = _sc.ac_get("workflow_sla_summary") or []
            _kpi_cache = _sc.ac_get("sla_matrix_kpis") or {}
            if _wf_cache or _kpi_cache:
                req.sla_matrix = {
                    "compliance_pct":        _kpi_cache.get("compliance_pct"),
                    "breaching_runs":        _kpi_cache.get("breaching_runs", 0),
                    "at_risk_runs":          _kpi_cache.get("at_risk_runs", 0),
                    "long_job_runs":         _kpi_cache.get("long_job_runs", 0),
                    "ok_runs":               _kpi_cache.get("ok_runs", 0),
                    "total_runs":            _kpi_cache.get("total_runs", 0),
                    "sla_limit_hrs":         _kpi_cache.get("sla_limit_hrs", 6.0),
                    "sla_label":             "Cached SLA Matrix",
                    "workflow_summary":      _wf_cache,
                    "breaches":              [],
                    "job_summary":           _sc.ac_get("job_summary") or [],
                    "window_compliance_pct": _kpi_cache.get("window_compliance_pct"),
                    "window_total_days":     _kpi_cache.get("window_total_days"),
                    "window_breach_days":    _kpi_cache.get("window_breach_days"),
                    "_enriched_from_cache":  True,
                }

        # ── sla_triage: synthesise from sla_matrix when client didn't send it ─
        # The JS _buildSlaTriage() builds this from window.appData.slaMatrix, which is
        # only available in the browser. Server-side regenerations (restart, direct call,
        # PE narrative trigger) get req.sla_matrix populated above but never get
        # req.sla_triage — causing the workflow audit summary and "overrun linked to N
        # workflows" cross-reference to silently produce no output despite data being
        # available in req.sla_matrix.workflow_summary.
        if not req.sla_triage:
            _wf_sum   = (req.sla_matrix or {}).get("workflow_summary") or []
            _job_sum  = (req.sla_matrix or {}).get("job_summary")      or []
            _brch_rows= (req.sla_matrix or {}).get("breaches")         or []
            if _wf_sum or _job_sum:
                _LOW = 20.0
                _wf_br = [w for w in _wf_sum if w.get("status") == "BREACH"]
                _wf_lb = [w for w in _wf_sum
                           if w.get("status") != "BREACH"
                           and _f(w.get("buffer_pct", 999)) < _LOW]
                _lbj   = sorted(
                    [j for j in _job_sum if _f(j.get("buffer_pct", 999)) < _LOW],
                    key=lambda j: _f(j.get("buffer_pct", 999))
                )[:10]
                _ubr   = [r for r in _brch_rows if r.get("status") == "BREACH"][:10]

                def _wf_row_br(w):
                    return {"workflow":   w.get("workflow_name") or w.get("workflow") or w.get("sub_application") or "?",
                            "batch_type": w.get("batch_type") or "?",
                            "sla_hours":  _f(w.get("sla_h") or w.get("sla_hours") or 0),
                            "runtime_h":  _f(w.get("runtime_h") or 0),
                            "buffer_pct": w.get("buffer_pct"),
                            "sla_source": w.get("sla_source") or "batch_sla_xlsx",
                            "data_src":   "ctrl_m_canonical"}

                def _wf_row_lb(w):
                    return {**_wf_row_br(w),
                            "status": w.get("status") or "AT_RISK"}

                req.sla_triage = {
                    "low_buffer_jobs": [
                        {"job_name":    j.get("job_name") or j.get("Job_Name") or "?",
                         "buffer_pct":  _f(j.get("buffer_pct")),
                         "peak_hrs":    _f(j.get("peak_hrs")),
                         "sla_hrs":     _f(j.get("sla_limit") or j.get("sla_limit_hrs") or j.get("sla_hrs") or 0),
                         "breach_rate": _f(j.get("breach_rate"))}
                        for j in _lbj
                    ],
                    "unexplained_breaches": [
                        {"job_name":      r.get("job_name") or "?",
                         "run_date":      r.get("run_date") or "",
                         "run_hrs":       _f(r.get("run_hrs")),
                         "sla_limit_hrs": _f(r.get("sla_limit_hrs")),
                         "margin_hrs":    _f(r.get("breach_margin_hrs")),
                         "sla_source":    r.get("sla_source") or "global"}
                        for r in _ubr
                    ],
                    "wf_breaching":        [_wf_row_br(w) for w in _wf_br[:5]],
                    "wf_low_buffer":       [_wf_row_lb(w) for w in _wf_lb[:5]],
                    "total_jobs_analysed": len(_job_sum),
                    "total_wfs_analysed":  len(_wf_sum),
                    "source_active": {
                        "batch_sla_xlsx":   False,
                        "ctrl_m_canonical": bool(_wf_sum),
                        "sow_ceilings":     False,
                    },
                    "_enriched_from_cache": True,
                }

        # ── regression_df: jobs with timing anomalies (from batch upload) ─
        # Written by batch.py as resp_dict["anomalies"] — jobs whose runtime
        # this run is a statistical outlier vs their own recent history.
        # Shape: [{Job_Name, run_hrs, avg_hrs, sigma, zscore, run_date}, ...]
        if not req.anomalies:
            _reg = _sc.ac_get("regression_df") or []
            if _reg:
                req.anomalies = _reg  # type: ignore[assignment]

        # ── adaptive_sla: per-job dynamic baselines (from sla_matrix) ────
        # Written by batch.py from sla_mx_dict["job_baselines"] — each entry
        # has {job_name, baseline_hrs, p95_hrs, stddev, sla_hrs, recommendation}.
        # Used to flag jobs where the dynamic baseline is tighter than config SLA.
        if not req.issues:
            _asl = _sc.ac_get("adaptive_sla") or []
            if _asl:
                req.issues = _asl  # type: ignore[assignment]

        # ── volume_vs_sow: contractual volume vs observed (from SOW parse) ─
        # Written by sow.py — {volume_by_year, max_item_locations}.
        # Injected into sow_compare so the SOW rules can check volume drift.
        if not req.sow_compare:
            _vol = _sc.ac_get("volume_vs_sow") or {}
            _sow = _sc.ac_get("sow_contract")  or {}
            # Only treat the cached contract as a real SOW when it carries the
            # canonical SOW structure written by the SOW parser (sow.py). Presence
            # of these keys — not their truthiness — is the signal, so a sparse but
            # genuine SOW still counts, while a stray slot can't masquerade as a SOW
            # and trigger false "SOW comparison included" claims.
            _SOW_STRUCT_KEYS = ("sla_windows", "volume_by_year", "operational_standards")
            _sow_is_real = isinstance(_sow, dict) and any(k in _sow for k in _SOW_STRUCT_KEYS)
            if _vol or _sow_is_real:
                req.sow_compare = {**(_sow if _sow_is_real else {}), **_vol,
                                   "_enriched_from_cache": True}

    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════
    # SLA MATRIX RULES
    # ═══════════════════════════════════════════════════════════════
    sla = req.sla_matrix or {}
    if sla:
        cov.sla = True
        sla_label   = sla.get("sla_label") or "SLA"
        sla_limit   = _f(sla.get("sla_limit_hrs"))
        sla_breach  = _i(sla.get("breaching_runs"))
        sla_atrisk  = _i(sla.get("at_risk_runs"))
        sla_ok      = _i(sla.get("ok_runs"))
        sla_runs    = _i(sla.get("total_runs"))
        sla_jobs    = _i(sla.get("total_jobs"))
        # Per-JOB SLA compliance = fraction of runs that stayed within their
        # individual ceiling (run-level pass rate). This is distinct from the
        # window/day-level headline compliance and is the only metric the
        # "Per-Job SLA" findings below should ever cite.
        sla_comp    = (((sla_runs - sla_breach) / sla_runs) * 100.0
                       if sla_runs > 0 else 100.0)
        breach_rows = sla.get("breaches") or []
        job_summary = sla.get("job_summary") or []
        worst_job   = sla.get("worst_job") or ""
        worst_hrs   = _f(sla.get("worst_hrs"))
        worst_marg  = _f(sla.get("worst_margin_hrs"))
        # Window compliance — resolve breach/total days first, then DERIVE the
        # day-level compliance % from them so the headline % and the
        # "{breach}/{total} day(s)" fraction always reconcile. Pair-level
        # ((sub_app × day)) compliance is retained only as a labeled secondary.
        win_total_days = _i(
            sla.get("window_total_days")
            or bk.get("window_total_days")
            or 0
        )
        win_breach_days = _i(
            sla.get("window_breach_days")
            or bk.get("window_breach_days")
            or 0
        )
        win_comp_pair = sla.get("window_compliance_pct")
        if win_comp_pair is None:
            win_comp_pair = bk.get("window_compliance_pct")
        if win_comp_pair is None:
            win_comp_pair = bk.get("batch_window_compliance")
        if win_total_days > 0:
            win_comp_pct = round((win_total_days - win_breach_days) / win_total_days * 100, 1)
        else:
            win_comp_pct = win_comp_pair

        # Distinct breaching jobs
        breach_names: list[str] = []
        seen_b: set[str] = set()
        for r in breach_rows:
            jn = r.get("job_name") or ""
            if jn and r.get("status") == "BREACH" and jn not in seen_b:
                seen_b.add(jn); breach_names.append(jn)

        # ── PRIMARY: Window compliance verdict (matches Executive Dashboard) ──
        # Window compliance = days where elapsed wall-clock ≤ SLA ceiling.
        # Even if every individual job is under the SLA, jobs running
        # sequentially/overlapping can inflate the total batch window past SLA.
        if win_comp_pct is not None and win_total_days > 0:
            pass_days = win_total_days - win_breach_days
            if win_breach_days > 0:
                # Primary window verdict — emit only if the batch section (R1b) didn't
                # already report the identical metric, to avoid a duplicate critical
                # with potentially divergent severity.
                if not _window_finding_emitted:
                    win_level = "critical" if win_comp_pct < 75 else "warning"
                    add(win_level, "📅",
                        f"Batch Window Compliance: {win_comp_pct:.1f}% — {win_breach_days}/{win_total_days} day(s) breached",
                        f"Elapsed batch window exceeded {sla_limit:.2f}h SLA on {win_breach_days} day(s). "
                        f"Individual jobs may be within SLA but total batch window is not."
                        + (f" (Per sub-app \u00d7 day window: {_f(win_comp_pair):.1f}%.)" if win_comp_pair is not None else ""),
                        source="sla", evidence_class="measured",
                        impact=f"PE sign-off blocked — batch window overran on {win_breach_days} of {win_total_days} days",
                        evidence=f"SLA Matrix · Window compliance · {sla_limit:.2f}h ceiling · {win_total_days} days analysed",
                        recommendation="Investigate job overlap and sequencing; parallelise or reschedule to compress total batch window",
                        root_cause="BATCH_WINDOW_OVERRUN")
                    _window_finding_emitted = True

                # ── Cross-reference: which workflows are driving the window overrun ──
                # When the batch window is breaching AND specific workflows are under SLA pressure,
                # name them directly — this is the "root cause" the PE consultant actually needs.
                # Unique value (names the pressure workflows) → fires regardless of which section
                # emitted the primary verdict above.
                _xref_triage = req.sla_triage or {}
                if isinstance(_xref_triage, dict):
                    _xwf = (_xref_triage.get("wf_breaching") or []) + \
                           (_xref_triage.get("wf_low_buffer") or [])
                    if _xwf:
                        _xwf_parts = []
                        for _xw in _xwf[:4]:
                            _xn = _xw.get("workflow", "?")
                            _xb = _xw.get("buffer_pct")
                            _xs = _xw.get("status", "?")
                            _xrt = _xw.get("runtime_h")
                            _xsl = _xw.get("sla_hours")
                            _detail = ""
                            if _xrt is not None and _xsl is not None and float(_xsl) > 0:
                                _detail = f" — {float(_xrt):.2f}h vs {float(_xsl):.1f}h SLA"
                            elif _xb is not None:
                                _detail = f" — {float(_xb):.1f}% buffer"
                            _xwf_parts.append(f"{_xn} ({_xs}{_detail})")
                        _xwf_str  = "; ".join(_xwf_parts)
                        _xwf_src  = "Ctrl-M" if _xref_triage.get("source_active", {}).get("ctrl_m_canonical") else "XLSX snapshot"
                        add("warning", "🔗",
                            f"Batch window overrun linked to {len(_xwf)} workflow(s) under SLA pressure",
                            f"Window compliance {win_comp_pct:.1f}% ({win_breach_days}/{win_total_days} days breached). "
                            f"Pressure workflows: {_xwf_str}. Source: {_xwf_src}. "
                            "Reducing runtime in these workflows is the direct path to recovering batch window compliance.",
                            source="sla", evidence_class="inferred",
                            impact="These workflows are the proximate cause of batch window overruns — window won't recover until they do",
                            recommendation="Start with the lowest-buffer workflow. Profile its longest-running job, check for data volume growth, "
                                          "parallelisation opportunities, and DB contention. Target >40% buffer on each workflow.",
                            root_cause="WORKFLOW_DRIVEN_WINDOW_BREACH")

            else:
                if not _window_finding_emitted:
                    add("ok", "📅",
                        f"Batch Window Compliance: {win_comp_pct:.1f}% — all {win_total_days} day(s) within SLA",
                        f"Elapsed wall-clock window ≤ {sla_limit:.2f}h on every day",
                        source="sla", evidence_class="measured",
                        root_cause="")
                    _window_finding_emitted = True

        # ── SECONDARY: Per-run individual job findings ──
        if sla_breach > 0:
            head = ", ".join(breach_names[:3]) or worst_job or "?"
            add("critical", "⏰",
                f"Per-Job SLA: {sla_breach} run(s) BREACHED individual SLA ceiling",
                f"Worst: {worst_job} at {worst_hrs:.2f}h (+{worst_marg:.2f}h over {sla_limit:.2f}h). "
                f"Breaching jobs: {head}{'…' if len(breach_names) > 3 else ''}.",
                source="sla", evidence_class="measured",
                impact="Individual job SLA breaches must each have an approved fix or signed waiver",
                evidence=f"SLA Matrix · {sla_label} · {sla_runs} runs / {sla_jobs} jobs",
                recommendation="Drill into the SLA Matrix tab; for each breaching job either optimise runtime or obtain customer SLA waiver",
                root_cause="JOB_SLA_BREACH")
        elif sla_atrisk > 0:
            add("warning", "⏰",
                f"Per-Job SLA: {sla_atrisk} run(s) AT_RISK — {sla_comp:.1f}% compliance vs {sla_label}",
                f"{sla_ok} OK runs · {sla_atrisk} within 15% of ceiling — any production load spike will breach",
                source="sla", evidence_class="measured",
                impact="At-risk jobs will breach SLA under increased data volume or concurrent load",
                recommendation="Profile at-risk jobs under load-test conditions; pre-emptively engage app owner",
                root_cause="JOB_SLA_AT_RISK")
        elif sla_runs > 0 and sla_jobs > 0:
            add("ok", "✅",
                f"Per-Job SLA: {sla_comp:.1f}% — all {sla_runs} runs within individual job SLA",
                f"{sla_jobs} job(s) analysed · 0 breaches · 0 at-risk · {sla_label}",
                source="sla", evidence_class="measured",
                root_cause="")

        # Tightest job buffer (from job_summary)
        if job_summary:
            tight = min(job_summary, key=lambda j: _f(j.get("buffer_pct"), 999))
            buf_pct = _f(tight.get("buffer_pct"), 999)
            if buf_pct < 15:
                add("warning", "⚡",
                    f"Tightest SLA buffer: {buf_pct:.1f}% — {tight.get('job_name','?')}",
                    f"Peak {_f(tight.get('peak_hrs')):.2f}h vs {sla_limit:.1f}h ceiling — "
                    "any runtime spike will breach SLA",
                    source="sla", evidence_class="measured")

        # ── SLA triage from JS _buildSlaTriage() ─────────────────────
        sla_triage = req.sla_triage or {}
        if isinstance(sla_triage, dict):
            low_buf_jobs = sla_triage.get("low_buffer_jobs") or []
            unexplained  = sla_triage.get("unexplained_breaches") or []

            # Priority application jobs — the smallest buffers are the first
            # jobs to watch in the heat map and in the PE narrative.
            if low_buf_jobs:
                sorted_low = sorted(
                    low_buf_jobs,
                    key=lambda j: (_f(j.get("buffer_pct", 999)), -_f(j.get("breach_rate", 0))),
                )
                top_low = sorted_low[:5]
                names = "; ".join(
                    f"{j.get('job_name', '?')} ({_f(j.get('buffer_pct', 0)):.1f}% buffer)"
                    for j in top_low
                )
                sev = "critical"
                add(sev, "⭐",
                    f"Priority application jobs need attention: {len(low_buf_jobs)} job(s) below 20% buffer",
                    f"These are the jobs to watch first in the heat map and PE review: {names}"
                    f"{'…' if len(sorted_low) > 5 else ''}.",
                    source="sla", evidence_class="measured",
                    impact="Low-buffer jobs are the first to fail when data volume or runtime shifts upward",
                    recommendation="Prioritise these jobs for optimisation, then re-run the review to confirm buffer recovery.",
                    root_cause="PRIORITY_APPLICATION_JOBS")

            # Low-buffer jobs (< 20% headroom) — per-job findings
            for j in low_buf_jobs[:5]:
                buf  = _f(j.get("buffer_pct", 0))
                jn   = j.get("job_name", "?")
                peak = _f(j.get("peak_hrs", 0))
                sla  = _f(j.get("sla_hrs", 0))
                lvl  = "critical" if buf < 10 else "warning"
                add(lvl, "⚡",
                    f"Thin SLA buffer: '{jn}' has only {buf:.1f}% headroom",
                    f"Peak runtime {peak:.2f}h vs {sla:.2f}h SLA ceiling — "
                    f"a {max(1, round(sla * (20 - buf) / 100, 2)):.2f}h increase will cause breach",
                    source="sla", evidence_class="measured",
                    impact=f"'{jn}' is {100 - buf:.1f}% utilised against SLA — production load growth will breach",
                    recommendation=f"Profile and optimise '{jn}' or negotiate SLA extension. "
                                   f"Target: reduce peak runtime below {sla * 0.80:.2f}h",
                    root_cause="THIN_SLA_BUFFER")

            # Unexplained breaches — breach with no correlated resource evidence
            for b in unexplained[:5]:
                jn  = b.get("job_name", "?")
                dt  = b.get("run_date", "?")
                hrs = _f(b.get("run_hrs", 0))
                sla_h = _f(b.get("sla_limit_hrs", 0))
                mgn = _f(b.get("margin_hrs", 0))
                src = b.get("sla_source", "global")
                add("critical", "🔍",
                    f"SLA breach — root cause unidentified: '{jn}'",
                    f"Ran {hrs:.2f}h (+{mgn:.2f}h over {sla_h:.2f}h SLA) on {dt}. "
                    f"No correlated infrastructure pressure found in resource data. "
                    f"SLA source: {src}. Manual investigation required.",
                    source="sla", evidence_class="inferred",
                    impact=f"'{jn}' exceeded SLA with no infrastructure evidence — may indicate code regression, "
                           "data volume spike, lock contention, or concurrent job interference",
                    recommendation=f"Review '{jn}' Ctrl-M job history, application logs, "
                                   f"and database query plans for {dt}. "
                                   "Check for concurrent batch jobs sharing the same DB or application tier.",
                    root_cause="UNEXPLAINED_SLA_BREACH")

        # Repeat offenders (same job breaching multiple runs)
        from collections import Counter as _Counter
        rep = _Counter(r.get("job_name") for r in breach_rows
                       if r.get("status") == "BREACH" and r.get("job_name"))
        repeats = [(j, n) for j, n in rep.items() if n >= 2]
        if repeats:
            names = ", ".join(f"{j}×{n}" for j, n in repeats[:3])
            add("critical", "🔁",
                f"{len(repeats)} job(s) breaching SLA repeatedly — pattern not anomaly",
                f"{names} — investigate trend (data volume, code regression, contention)",
                source="sla", evidence_class="measured",
                impact="Repeat breaches indicate systemic issue, not transient spike",
                recommendation="Engage app/DB owner — review last 30-day trend in SLA tab")

        # SLA ceilings notice — only when a CUSTOMER SLA file was genuinely loaded.
        # req.sla_ceilings is enriched from config_store, which ALWAYS carries default
        # values, so gating on it alone would falsely claim "loaded from XLSX" even with
        # no upload. sla_loaded is the authoritative signal (batch sla_source.type or the
        # SLA-matrix tab), keeping this notice consistent with the batch-section labels.
        if sla_loaded and req.sla_ceilings:
            parts = [f"{k}: {_fmt_hrs(v)}h" for k, v in req.sla_ceilings.items()]
            add("info", "📐",
                f"Customer SLA ceilings loaded from XLSX",
                f"Active windows: {' · '.join(parts)} — all status calculations use these values",
                source="sla",
                impact="Customer-approved SLA values are applied — compliance results are audit-defensible",
                recommendation="Verify ceiling values match the signed PE agreement before submitting audit report",
                root_cause="")

        # ── Workflow-level SLA findings (from _buildSlaTriage → wf_breaching / wf_low_buffer) ──
        # These operate at the workflow (Sub_Application) level — not individual jobs.
        # Source priority: canonical Ctrl-M worst-case > XLSX snapshot.
        # Findings only generated when sla_triage carries the data; silently skipped otherwise.
        sla_triage = req.sla_triage or {}
        if isinstance(sla_triage, dict):
            wf_breaching  = sla_triage.get("wf_breaching")  or []
            wf_low_buffer = sla_triage.get("wf_low_buffer") or []
            total_wfs     = _i(sla_triage.get("total_wfs_analysed") or 0)

            # ── SLA workflow audit summary — tier breakdown with threshold context ──
            if wf_breaching or wf_low_buffer or total_wfs > 0:
                from services import pe_config as _pec
                _at_pct = float(getattr(_pec, "SLA_ATRISK_PCT", 15.0))
                _lj_pct = float(getattr(_pec, "SLA_LONGJOB_PCT", 40.0))
                _tier: dict = {}
                for _w in wf_breaching:
                    _tier["BREACH"] = _tier.get("BREACH", 0) + 1
                for _w in wf_low_buffer:
                    _s = _w.get("status", "AT_RISK")
                    _tier[_s] = _tier.get(_s, 0) + 1
                _ok_ct = max(0, total_wfs - len(wf_breaching) - len(wf_low_buffer))
                _sp = []
                if _tier.get("BREACH"):    _sp.append(f'{_tier["BREACH"]} BREACH (<0% buffer)')
                if _tier.get("AT_RISK"):   _sp.append(f'{_tier["AT_RISK"]} AT_RISK (0\u2013{_at_pct:.0f}%)')
                if _tier.get("LONG_JOB"): _sp.append(f'{_tier["LONG_JOB"]} LONG_JOB ({_at_pct:.0f}\u2013{_lj_pct:.0f}%)')
                if _ok_ct > 0:             _sp.append(f'{_ok_ct} OK (>{_lj_pct:.0f}% buffer)')
                if _sp:
                    _src = "Ctrl-M canonical" if sla_triage.get("source_active", {}).get("ctrl_m_canonical") else "XLSX snapshot"
                    _slvl = "critical" if _tier.get("BREACH") \
                        else "warning" if (_tier.get("AT_RISK") or _tier.get("LONG_JOB")) \
                        else "info"
                    add(_slvl, "📊",
                        f"SLA Workflow Audit: {total_wfs} workflows — {'; '.join(_sp)}",
                        f"Tier thresholds: OK >{_lj_pct:.0f}% \u00b7 LONG_JOB {_at_pct:.0f}\u2013{_lj_pct:.0f}% \u00b7 "
                        f"AT_RISK 0\u2013{_at_pct:.0f}% \u00b7 BREACH <0%."
                        f" Buffer=(SLA\u2212runtime)\u00f7SLA\u00d7100. Source: {_src}.",
                        source="sla", evidence_class="measured",
                        impact=f"SLA compliance across {total_wfs} workflow(s) — breach count directly affects delivery risk verdict",
                        recommendation="Resolve all BREACH and AT_RISK workflows before go-live. Target >40% buffer for OK.",
                        root_cause="SLA_WORKFLOW_AUDIT")

            for wf in wf_breaching[:5]:
                name    = wf.get("workflow", "?")
                sla_h   = _f(wf.get("sla_hours"))
                rt_h    = _f(wf.get("runtime_h"))
                buf     = _f(wf.get("buffer_pct"))
                src     = wf.get("data_src", "xlsx_snapshot")
                sla_src = wf.get("sla_source", "batch_sla_xlsx")
                buf_str = f"{buf:.1f}%" if wf.get("buffer_pct") is not None else "unknown"
                over_min = round((rt_h - sla_h) * 60, 1) if rt_h and sla_h else None
                over_str = f" (+{over_min} min over SLA)" if over_min else ""
                add("critical", "🔴",
                    f"Workflow BREACH: '{name}' exceeded SLA window ({buf_str} buffer)",
                    f"Runtime {rt_h:.3f}h vs {sla_h:.2f}h SLA{over_str}. "
                    f"SLA source: {sla_src}. Runtime source: {src}.",
                    source="sla", evidence_class="measured",
                    impact=f"'{name}' ran past its contracted SLA window — delivery commitment at risk",
                    recommendation=f"Profile '{name}' workflow end-to-end. Identify longest-running job within the workflow and optimise or reschedule.",
                    root_cause="WORKFLOW_SLA_BREACH")

            for wf in wf_low_buffer[:5]:
                name   = wf.get("workflow", "?")
                sla_h  = _f(wf.get("sla_hours"))
                rt_h   = _f(wf.get("runtime_h"))
                buf    = _f(wf.get("buffer_pct"))
                status = wf.get("status", "?")
                src    = wf.get("data_src", "xlsx_snapshot")
                if buf is None:
                    continue
                buf_str  = f"{buf:.1f}%"
                used_pct = round(100 - buf, 1)
                headroom_min = round((sla_h - rt_h) * 60, 1) if rt_h and sla_h else None
                hdstr = f" ({headroom_min} min headroom)" if headroom_min else ""
                lvl   = "critical" if buf <= 5 else "warning"
                add(lvl, "⚠️",
                    f"Workflow {status}: '{name}' using {used_pct}% of SLA window — {buf_str} headroom{hdstr}",
                    f"Runtime {rt_h:.3f}h vs {sla_h:.2f}h SLA. Runtime source: {src}. "
                    "Any runtime increase will reduce margin further.",
                    source="sla", evidence_class="measured",
                    impact=f"'{name}' has limited tolerance for runtime growth — one degraded run could breach",
                    recommendation=f"Review '{name}' for recurring delay patterns. Target runtime below {sla_h * 0.6:.2f}h (60% SLA) for a safe OK classification.",
                    root_cause="WORKFLOW_SLA_AT_RISK")

    # ═══════════════════════════════════════════════════════════════
    # BENCHMARK RULES — TWO ISOLATED SECTIONS
    # ═══════════════════════════════════════════════════════════════
    # Section A: UI performance findings (Transaction Comparison Matrix data).
    # Section B: Batch runtime regression findings (batch_perf_summary data).
    # These two measurement types must never be mixed. A benchmark object with
    # kind="batch" has rows=[] and must not trigger UI performance findings.
    # ═══════════════════════════════════════════════════════════════
    bench = req.benchmark or {}
    if not bench:
        # Server-side fallback: pick up the last uploaded benchmark from session
        # cache so batch-perf / UI findings survive page reloads and server-side
        # regeneration even when the caller didn't echo it back in the request.
        try:
            if _session_cache:
                bench = _session_cache.get("last_benchmark") or {}
        except Exception as _be:
            log.debug("benchmark session fallback failed: %s", _be)
    if bench:
        cov.benchmark = True
        bench_kind = bench.get("kind", "ui")   # "batch" | "ui"

        # ── Section A: UI performance findings ────────────────────
        # Only fires when rows are present AND this is a UI benchmark file.
        # Batch files (kind="batch") have rows=[] by design — skip entirely.
        rows = bench.get("rows") or []
        ui_rows = [r for r in rows if bench_kind != "batch"]   # empty for batch files
        # NOTE: BenchmarkResponse.summary is a free-text string, NOT a dict.
        # Only treat it as a counts dict if a caller actually passed one (defensive:
        # a bare string here previously crashed the whole engine with
        # "'str' object has no attribute 'get'").
        _bs = bench.get("summary")
        bench_summ = _bs if isinstance(_bs, dict) else {}
        total_tx   = _i(bench_summ.get("total", bench.get("total_transactions", len(ui_rows))))
        threshold  = _f(bench.get("threshold_pct", 10.0))
        sla_breaches_b = _i(bench.get("sla_breaches", 0))

        # Summary-level fields — present when rows list is absent (template-upload / summary-only shape)
        b_degraded       = _i(bench.get("degraded") or 0)
        b_worst_delta    = _f(bench.get("worst_delta_pct") or 0)
        b_worst_tx       = bench.get("worst_transaction") or bench.get("worst_tx") or "—"
        b_sla_breach_cnt = _i(bench.get("sla_breach_count") or sla_breaches_b)

        # Status vocabulary: OK | WATCH | BREACH (legacy: GREEN | AMBER | RED)
        # Only applies to UI rows — batch files have ui_rows=[] so this whole
        # block produces nothing, which is the correct and intended behaviour.
        red_rows   = [r for r in ui_rows if r.get("status") in ("BREACH", "RED")]
        watch_rows = [r for r in ui_rows if r.get("status") in ("WATCH", "AMBER")]
        slow_pct   = (len(red_rows) / total_tx * 100) if total_tx > 0 else 0
        # Summary-based degraded pct (when rows are missing)
        b_slow_pct = (b_degraded / total_tx * 100) if (not ui_rows and total_tx > 0) else 0

        if slow_pct > 20 or b_slow_pct > 20:
            _nbreach = len(red_rows) if ui_rows else b_degraded
            add("critical", "🐢",
                f"UI Benchmark: {_nbreach}/{total_tx} transactions in BREACH (>{threshold:.0f}% regression or SLA exceeded)",
                f"Severe UI performance degradation — investigate top offenders",
                source="benchmark", evidence_class="measured",
                root_cause="UI_PERFORMANCE_REGRESSION")
        elif red_rows or b_sla_breach_cnt > 0 or (not ui_rows and b_degraded > 0 and b_worst_delta > threshold):
            _nbreach = len(red_rows) or b_degraded
            _wdstr = (f" · worst: '{b_worst_tx}' +{b_worst_delta:.0f}%"
                      if not ui_rows and b_worst_tx != "—" else "")
            add("warning", "📉",
                f"UI Benchmark: {_nbreach} BREACH/degraded transaction(s)"
                + (f" · {b_sla_breach_cnt} SLA breach(es)" if b_sla_breach_cnt else "")
                + (f" · {len(watch_rows)} on WATCH" if watch_rows else "")
                + _wdstr,
                f"Transactions exceeding {threshold:.0f}% regression threshold or contractual SLA"
                + (f". Worst: '{b_worst_tx}'" if not ui_rows and b_worst_tx != "—" else ""),
                source="benchmark", evidence_class="measured",
                root_cause="UI_PERFORMANCE_REGRESSION")
        elif watch_rows or (not ui_rows and b_worst_delta > threshold):
            _nw = len(watch_rows) or (1 if b_worst_delta > 0 else 0)
            _wdstr = (f". Worst: '{b_worst_tx}' +{b_worst_delta:.0f}%"
                      if not ui_rows and b_worst_tx != "—" else "")
            add("warning", "👀",
                f"UI Benchmark: {_nw}/{total_tx} transaction(s) on WATCH — within 10% of SLA or {threshold:.0f}-{threshold*2:.0f}% regressed"
                + _wdstr,
                "No breaches, but these flows are trending toward SLA limits — monitor under production load",
                source="benchmark")
        elif total_tx > 0:
            _wdstr = (f" — worst: '{b_worst_tx}' +{b_worst_delta:.0f}%"
                      if b_worst_tx != "—" and b_worst_delta > 0 else "")
            add("ok", "⚡",
                f"UI Benchmark: All {total_tx} transactions within {threshold:.0f}% of baseline{_wdstr}",
                "UI performance meets contractual benchmark targets",
                source="benchmark", evidence_class="measured")

        # Worst offender
        if ui_rows:
            worst = max(ui_rows, key=lambda r: abs(_f(r.get("delta_pct"))))
            dev   = _f(worst.get("delta_pct"))
            if abs(dev) > threshold:
                tx = worst.get("transaction") or "?"
                add("info", "🔍",
                    f"Slowest UI transaction: '{tx}' at {dev:+.1f}% vs baseline",
                    f"Actual: {worst.get('current_sec', '?')}s  ·  "
                    f"Baseline: {worst.get('baseline_sec', '?')}s"
                    + (f"  ·  SLA: {worst['sla_sec']}s" if worst.get('sla_sec') else ""),
                    source="benchmark")

        # Worksheet load / export time analysis (UI-only: screen load times)
        load_times  = [_f(r.get("current_sec") or r.get("baseline_sec") or 0)
                       for r in ui_rows if _f(r.get("current_sec") or r.get("baseline_sec") or 0) > 0]
        export_times = [_f(r.get("export_time_current") or r.get("export_time_baseline") or 0)
                        for r in ui_rows if _f(r.get("export_time_current") or r.get("export_time_baseline") or 0) > 0]

        if load_times:
            avg_load = sum(load_times) / len(load_times)
            max_load = max(load_times)
            slow_loads = [r for r in ui_rows
                          if _f(r.get("current_sec") or r.get("baseline_sec") or 0) > 15]
            avg_export = (sum(export_times) / len(export_times)) if export_times else 0

            if slow_loads:
                slow_names = ", ".join(
                    (r.get("transaction") or "?") + f" ({_f(r.get('current_sec') or r.get('baseline_sec') or 0):.1f}s)"
                    for r in sorted(slow_loads, key=lambda r: _f(r.get("current_sec") or 0), reverse=True)[:3]
                )
                add("warning", "🐌",
                    f"Worksheet performance: {len(slow_loads)}/{len(load_times)} worksheet(s) loading >15s",
                    f"Slow: {slow_names}. Avg load: {avg_load:.1f}s, max: {max_load:.1f}s"
                    + (f", avg export: {avg_export:.1f}s" if export_times else "")
                    + ". Investigate DB query plans or data volume behind slow-loading worksheets.",
                    source="benchmark", evidence_class="measured",
                    impact="Users experience noticeable delays on allocation worksheets — affects operational efficiency",
                    recommendation="Profile DB queries backing the slowest worksheets. Check for missing indexes, "
                                   "full table scans, or excessive data volume. Target <10s load time.",
                    root_cause="WORKSHEET_LOAD_SLOW")
            elif avg_load > 10:
                add("info", "📊",
                    f"Worksheet load times averaging {avg_load:.1f}s across {len(load_times)} worksheets"
                    + (f" · export avg {avg_export:.1f}s" if export_times else ""),
                    f"Max load: {max_load:.1f}s. No individual worksheet exceeded 15s threshold "
                    "but average is above 10s — monitor for degradation under production load.",
                    source="benchmark", evidence_class="measured",
                    root_cause="WORKSHEET_LOAD_ELEVATED")

            # Export time anomaly: any worksheet taking >5s to export
            if export_times:
                slow_exports = [r for r in ui_rows
                                if _f(r.get("export_time_current") or r.get("export_time_baseline") or 0) > 5]
                if slow_exports:
                    exp_names = ", ".join(
                        (r.get("transaction") or "?")
                        + f" ({_f(r.get('export_time_current') or r.get('export_time_baseline') or 0):.1f}s)"
                        for r in slow_exports[:3]
                    )
                    add("info", "📤",
                        f"Worksheet export: {len(slow_exports)} worksheet(s) with export time >5s",
                        f"Slow exports: {exp_names}. May indicate rendering bottleneck or large dataset export.",
                        source="benchmark", evidence_class="measured",
                        root_cause="WORKSHEET_EXPORT_SLOW")

        # Category-level findings (multi-sheet UI XLSX only — not batch windows)
        for cat in bench.get("categories") or []:
            name = cat.get("name", "?")
            total_c = _i(cat.get("total", 0))
            degraded_c = _i(cat.get("degraded", 0))
            failed_c = _i(cat.get("failed", 0))
            if degraded_c > 0:
                add("warning", "📊",
                    f"UI Benchmark — {name}: {degraded_c}/{total_c} regressions detected",
                    f"Average delta: {_f(cat.get('avg_delta', 0)):+.1f}% — review environment parity",
                    source="benchmark", root_cause=f"UI performance regression in {name}")

        # Fill rate drift findings
        fr = bench.get("fill_rate") or []
        fr_fails = [e for e in fr if str(e.get("status", "")).lower() not in ("pass", "")]
        if fr_fails:
            add("warning", "📉",
                f"Fill Rate: {len(fr_fails)} entries with drift between PROD & TEST",
                "Fill rate mismatch may indicate data migration or config issues",
                source="benchmark")
        elif fr:
            add("ok", "✅",
                f"Fill Rate: All {len(fr)} entries match between PROD & TEST",
                "Data fill rate is consistent across environments",
                source="benchmark")

        # SIT Observations findings
        obs = bench.get("observations") or []
        open_obs = [o for o in obs if str(o.get("status", "")).lower() not in ("closed", "resolved", "done")]
        if open_obs:
            add("warning", "🔎",
                f"SIT Observations: {len(open_obs)} open issue(s) remain",
                "; ".join(str(o.get("problem", ""))[:60] for o in open_obs[:3]),
                source="benchmark")

        # ───────────────────────────────────────────────────────────
        # BATCH RUNTIME PERFORMANCE (new-release vs old-release runtime
        # comparison file — distinct from UI transaction benchmark above).
        # Without this branch, batch runtime regressions are invisible to
        # PE Findings even when a batch-perf file is uploaded.
        # ───────────────────────────────────────────────────────────
        bp = bench.get("batch_perf_summary") or {}
        if bp:
            bp_total   = _i(bp.get("total_jobs", 0))
            bp_comp    = _i(bp.get("comparable", 0))
            bp_regr    = _i(bp.get("regressions", 0))
            bp_impr    = _i(bp.get("improvements", 0))
            bp_net     = _f(bp.get("net_delta_secs", 0))
            top_regr   = bp.get("top_regressions") or []
            # Prefer substantial-baseline regressions (real elapsed-time additions) for
            # the offenders list. top_regressions is ranked by % delta, so it surfaces
            # near-zero-baseline noise (e.g. 3s→375s = +12400%) that is NOT a meaningful
            # production risk and reads as alarming nonsense to a PE reviewer. The
            # projectable list is baseline-floored and ranked by absolute seconds added.
            _proj_regr = bp.get("projectable_regressions") or []
            _offenders = _proj_regr if _proj_regr else top_regr
            _offender_label = "Top regressions by time added" if _proj_regr else "Top regressions"
            regr_pct   = (bp_regr / bp_comp * 100) if bp_comp > 0 else 0.0
            # net_delta_secs: positive = time saved, negative = time added (per build convention)
            net_dir    = "saved" if bp_net >= 0 else "added"

            def _bp_offenders(n=3):
                return "; ".join(
                    f"{(r.get('job') or '?')} "
                    f"{_f(r.get('old_secs')):.0f}s→{_f(r.get('new_secs')):.0f}s "
                    f"({_f(r.get('delta_pct')):+.0f}%)"
                    for r in _offenders[:n]
                )

            _STOP = {"the","and","for","run","job","load","time","test","prod",
                     "uat","new","old","sec","secs","daily","weekly","monthly",
                     "batch","data","report","process","step","main","seq"}

            def _tokset(name):
                import re as _re
                base = _re.sub(r"^(?:PROD|TEST|UAT|DEV|STG)_+", "", str(name or "").upper())
                norm = _re.sub(r"[^A-Z0-9_]+", "_", base)
                toks = [t for t in norm.split("_") if t]
                return {t for t in toks if len(t) >= 3 and t.lower() not in _STOP and not t.isdigit()}

            if regr_pct >= 30 or (bp_net < 0 and abs(bp_net) > 1800):
                add("critical", "⏱️",
                    f"Batch runtime: {bp_regr}/{bp_comp} jobs regressed "
                    f"({regr_pct:.0f}%) · net {abs(bp_net):.0f}s {net_dir}/run",
                    f"{_offender_label} — {_bp_offenders()}",
                    source="benchmark", evidence_class="measured",
                    impact="New release lengthens batch runtimes — risks SLA breach and "
                           "downstream batch-window overrun in production.",
                    recommendation="Profile the regressed jobs against the new build. Compare "
                                   "execution plans / data volumes between releases before go-live.",
                    root_cause="BATCH_RUNTIME_REGRESSION")
            elif bp_regr > 0:
                add("warning", "⏱️",
                    f"Batch runtime: {bp_regr}/{bp_comp} job(s) regressed "
                    f"· {bp_impr} improved · net {abs(bp_net):.0f}s {net_dir}/run",
                    (f"{_offender_label} — {_bp_offenders()}" if _offenders
                     else "Some jobs run slower on the new release — review before sign-off."),
                    source="benchmark", evidence_class="measured",
                    root_cause="BATCH_RUNTIME_REGRESSION")
            elif bp_comp > 0:
                add("ok", "⏱️",
                    f"Batch runtime: all {bp_comp} comparable job(s) within tolerance "
                    f"· {bp_impr} improved · net {abs(bp_net):.0f}s {net_dir}/run",
                    "New release shows no batch runtime regressions vs prior baseline.",
                    source="benchmark", evidence_class="measured",
                    root_cause="BATCH_RUNTIME_CLEAN")

            # ── Coverage gap: jobs that did not genuinely run on the new release ──
            # Suspect near-instant collapses + dropped (zero-runtime) jobs are
            # excluded from the regression/improvement counts to keep them honest,
            # but a material share signals the new-env run lacked representative
            # data — which limits how much of the batch the comparison validated.
            bp_suspect = _i(bp.get("suspect", 0))
            bp_dropped = _i(bp.get("dropped", 0))
            _gap = bp_suspect + bp_dropped
            if _gap > 0 and bp_total > 0:
                _gap_pct = _gap / bp_total * 100
                _gap_sev = "warning" if _gap_pct >= 10 else "info"
                add(_gap_sev, "🕳️",
                    f"Batch perf coverage gap: {_gap}/{bp_total} job(s) ({_gap_pct:.0f}%) "
                    f"had no comparable runtime ({bp_suspect} near-instant · {bp_dropped} not run)",
                    f"These jobs collapsed to near-zero or did not run on the new release — "
                    f"most likely no test data or early exit. They are excluded from the "
                    f"regression and improvement counts so those stay credible, but they "
                    f"reduce how much of the batch the runtime comparison actually validated.",
                    source="benchmark", evidence_class="measured",
                    impact="A material share of jobs were not genuinely exercised on the new "
                           "release, so the runtime comparison covers only part of the batch.",
                    recommendation="Re-run the near-instant / not-run jobs against representative "
                                   "data volumes before relying on this comparison for go-live sign-off.",
                    root_cause="BATCH_PERF_COVERAGE_GAP")

            # ── Cross-layer correlation: systemic vs isolated ──
            # If the same subsystem token regresses in BOTH a batch job and a
            # UI transaction, the root cause is shared infra/DB, not isolated.
            batch_regr_tokens = {}
            for r in top_regr:
                for t in _tokset(r.get("job")):
                    batch_regr_tokens.setdefault(t, r.get("job"))
            ui_regr_tokens = {}
            for r in red_rows:
                for t in _tokset(r.get("transaction")):
                    ui_regr_tokens.setdefault(t, r.get("transaction"))

            shared = set(batch_regr_tokens) & set(ui_regr_tokens)
            if shared and (bp_regr > 0) and red_rows:
                examples = "; ".join(
                    f"'{tok}' (batch: {batch_regr_tokens[tok]} · UI: {ui_regr_tokens[tok]})"
                    for tok in list(shared)[:3]
                )
                add("critical", "🔗",
                    f"Systemic regression: {len(shared)} subsystem(s) slow in BOTH "
                    f"batch and UI layers",
                    f"Shared subsystem(s) — {examples}. Regression spans batch and UI "
                    f"simultaneously, pointing to shared DB/infrastructure root cause "
                    f"rather than isolated job tuning.",
                    source="benchmark", evidence_class="derived",
                    impact="A single infra/DB regression is degrading multiple layers — "
                           "fixing one layer alone will not resolve it.",
                    recommendation="Investigate the shared subsystem at the DB/infra tier "
                                   "(shared tables, connection pool, storage). Validate the fix "
                                   "improves both batch runtime and UI response together.",
                    root_cause="SYSTEMIC_PERFORMANCE_REGRESSION")

            # ── Ctrl-M production bridge: project release regressions onto prod SLA ──
            ctrlm_jobs = []
            for j in top_jobs:
                j_name = j.get("Job_Name") or j.get("job_name") or ""
                tokens = _tokset(j_name)
                if not j_name or not tokens:
                    continue
                ctrlm_jobs.append({
                    "job": j,
                    "job_name": str(j_name),
                    "tokens": tokens,
                    "peak_hrs": _f(j.get("peak_hrs")),
                })

            # Source for SLA projection: only regressions with a credible baseline.
            # projectable_regressions (substantial baseline, ranked by absolute seconds
            # added) is emitted by _build_batch_perf_summary. Fall back to filtering
            # top_regressions for older cached payloads that predate that field.
            try:
                from services import pe_config as _pec_proj
                _proj_min_base = float(getattr(_pec_proj, "BATCH_PROJECT_MIN_BASELINE_SEC", 60.0))
                _proj_max_ratio = float(getattr(_pec_proj, "BATCH_PROJECT_MAX_BASELINE_RATIO", 10.0))
            except Exception:
                _proj_min_base, _proj_max_ratio = 60.0, 10.0
            proj_regr = bp.get("projectable_regressions")
            if proj_regr is None:
                proj_regr = [r for r in top_regr if _f(r.get("old_secs")) >= _proj_min_base]

            regr_jobs = []
            for r in proj_regr:
                r_name = r.get("job") or r.get("transaction") or ""
                tokens = _tokset(r_name)
                if not r_name or not tokens:
                    continue
                regr_jobs.append({
                    "reg": r,
                    "job_name": str(r_name),
                    "tokens": tokens,
                    "delta_pct": _f(r.get("delta_pct")),
                    "old_secs": _f(r.get("old_secs")),
                    "new_secs": _f(r.get("new_secs")),
                })

            matched_impacts = []
            # Gap B: regressions whose token-matched Ctrl-M job diverges from the
            # benchmark baseline by more than _proj_max_ratio×. These are excluded
            # from the SLA projection (the % would be meaningless), but the
            # exclusion is itself an audit event — surfaced as an explicit INFO
            # finding below rather than silently dropped.
            unresolvable_mismatches: list[dict] = []
            for reg_job in regr_jobs:
                best = None
                best_overlap = 0.0
                best_shared = 0
                for prod_job in ctrlm_jobs:
                    shared = reg_job["tokens"] & prod_job["tokens"]
                    overlap = (len(shared) / max(1, len(reg_job["tokens"]))) if shared else 0.0
                    if overlap < 0.5:
                        continue
                    if overlap > best_overlap or (overlap == best_overlap and len(shared) > best_shared):
                        best = prod_job
                        best_overlap = overlap
                        best_shared = len(shared)
                if not best:
                    continue

                j_name = best["job_name"]
                prod_peak_hrs = _f(best["peak_hrs"])
                release_delta_pct = _f(reg_job["delta_pct"])

                # Baseline-consistency guard: a benchmark job and a Ctrl-M job are the
                # SAME job only if their production-side runtimes are in the same
                # ballpark. If the benchmark baseline (prod env) and the Ctrl-M peak
                # differ by more than _proj_max_ratio×, the token match is a false
                # positive — different jobs that merely share a name prefix — and the
                # percentage projection would be meaningless. Skip it.
                bench_base_secs = _f(reg_job.get("old_secs"))
                prod_peak_secs  = prod_peak_hrs * 3600.0
                if bench_base_secs > 0 and prod_peak_secs > 0:
                    _ratio = max(bench_base_secs, prod_peak_secs) / min(bench_base_secs, prod_peak_secs)
                    if _ratio > _proj_max_ratio:
                        unresolvable_mismatches.append({
                            "job_name":        j_name,
                            "bench_old_secs":  bench_base_secs,
                            "bench_new_secs":  _f(reg_job.get("new_secs")),
                            "prod_peak_secs":  prod_peak_secs,
                            "prod_peak_hrs":   prod_peak_hrs,
                            "ratio":           _ratio,
                        })
                        continue

                prod_sla_hrs = _f(get_sla_hrs(detect_batch_type(j_name), sla_ceil))
                if prod_sla_hrs <= 0:
                    continue
                prod_buffer_pct = ((prod_sla_hrs - prod_peak_hrs) / prod_sla_hrs) * 100
                projected_hrs_after_release = prod_peak_hrs * (1 + release_delta_pct / 100)
                projected_buffer_pct = ((prod_sla_hrs - projected_hrs_after_release) / prod_sla_hrs) * 100
                matched_impacts.append({
                    "job_name": j_name,
                    "shared_tokens": best_shared,
                    "overlap": best_overlap,
                    "release_delta_pct": release_delta_pct,
                    "prod_peak_hrs": prod_peak_hrs,
                    "prod_sla_hrs": prod_sla_hrs,
                    "prod_buffer_pct": prod_buffer_pct,
                    "projected_hrs_after_release": projected_hrs_after_release,
                    "projected_buffer_pct": projected_buffer_pct,
                })

            if matched_impacts:
                matched_impacts.sort(key=lambda m: (m["projected_buffer_pct"], -m["projected_hrs_after_release"]))
                worst_impact = matched_impacts[0]
                match_count = _i(len(matched_impacts))
                matched_jobs = ", ".join(m["job_name"] for m in matched_impacts[:3])
                common_kwargs = dict(
                    source="benchmark",
                    evidence_class="derived",
                    impact="New-release runtime regression will directly impact production SLA compliance if deployed as-is",
                    root_cause="RELEASE_SLA_IMPACT",
                )
                if worst_impact["projected_buffer_pct"] < 0:
                    add(
                        "critical",
                        "🚨",
                        f"Release regression will breach production SLA: {worst_impact['job_name']} "
                        f"projects {worst_impact['projected_hrs_after_release']:.2f}h vs "
                        f"{worst_impact['prod_sla_hrs']:.1f}h SLA (current prod: "
                        f"{worst_impact['prod_peak_hrs']:.2f}h, new release: "
                        f"{worst_impact['projected_hrs_after_release']:.2f}h, regression: "
                        f"+{worst_impact['release_delta_pct']:.0f}%)",
                        f"Matched {_i(match_count)}/{_i(len(regr_jobs))} regressed job(s) to Ctrl-M "
                        f"production jobs. Worst-case projected buffer {worst_impact['projected_buffer_pct']:.0f}% "
                        f"across {matched_jobs}.",
                        recommendation="Block deployment until the regressed batch job is tuned back under the "
                                       "production SLA ceiling or the release is rolled back.",
                        **common_kwargs,
                    )
                elif worst_impact["projected_buffer_pct"] < 15:
                    add(
                        "warning",
                        "⚠️",
                        f"Release regression shrinks production SLA buffer: {worst_impact['job_name']} "
                        f"buffer drops from {worst_impact['prod_buffer_pct']:.0f}% → "
                        f"{worst_impact['projected_buffer_pct']:.0f}% after new release",
                        f"Matched {_i(match_count)}/{_i(len(regr_jobs))} regressed job(s) to Ctrl-M "
                        f"production jobs. Projected runtime {_fmt_hrs(worst_impact['projected_hrs_after_release'])}h "
                        f"vs {_fmt_hrs(worst_impact['prod_sla_hrs'])}h SLA; current prod peak "
                        f"{worst_impact['prod_peak_hrs']:.2f}h; regression +{worst_impact['release_delta_pct']:.0f}%.",
                        recommendation="Treat the release as conditional. Re-test the regressed production-mapped "
                                       "job and recover SLA buffer above the 15% at-risk threshold before go-live.",
                        **common_kwargs,
                    )
                else:
                    add(
                        "ok",
                        "✅",
                        "Regressed jobs have adequate production SLA buffer",
                        f"Matched {_i(match_count)}/{_i(len(regr_jobs))} regressed job(s) to Ctrl-M "
                        f"production jobs. Tightest case: {worst_impact['job_name']} projects "
                        f"{_fmt_hrs(worst_impact['projected_hrs_after_release'])}h vs "
                        f"{_fmt_hrs(worst_impact['prod_sla_hrs'])}h SLA; buffer stays "
                        f"{worst_impact['projected_buffer_pct']:.0f}% after release.",
                        recommendation="Proceed with release monitoring focused on the matched jobs, but no "
                                       "production SLA breach is projected from current runtime regressions.",
                        **common_kwargs,
                    )

            # Gap B: surface intentionally-excluded regressions so the PE reviewer
            # sees the silent data exclusion instead of it vanishing.
            if unresolvable_mismatches:
                unresolvable_mismatches.sort(key=lambda m: -m["ratio"])
                _um_worst = unresolvable_mismatches[0]
                _um_n     = len(unresolvable_mismatches)
                _um_names = ", ".join(m["job_name"] for m in unresolvable_mismatches[:3])
                add(
                    "info",
                    "🔍",
                    f"{_um_n} regression(s) excluded from production-SLA projection — "
                    f"unresolvable baseline mismatch ({_um_worst['ratio']:.0f}× divergence)",
                    f"Worst: {_um_worst['job_name']} — benchmark baseline "
                    f"{_fmt_hrs(_um_worst['bench_old_secs'] / 3600.0)}h "
                    f"(new {_fmt_hrs(_um_worst['bench_new_secs'] / 3600.0)}h) vs Ctrl-M production peak "
                    f"{_fmt_hrs(_um_worst['prod_peak_hrs'])}h = {_um_worst['ratio']:.1f}× apart. "
                    "The benchmark job and the token-matched Ctrl-M job differ too much to be the "
                    "same workload, so the % release projection is not applied to them"
                    f"{' (' + _um_names + ')' if _um_n > 1 else ''}.",
                    source="benchmark",
                    evidence_class="measured",
                    impact="Intentionally left out of the production-SLA impact projection — neither "
                           "cleared nor breaching, the benchmark↔Ctrl-M mapping is ambiguous.",
                    recommendation="Reconcile job naming between the benchmark workbook and the Ctrl-M "
                                   "export (or confirm they are genuinely different jobs). Once a "
                                   "comparable production job is matched, the release-impact projection "
                                   "will include them.",
                    root_cause="UNRESOLVABLE_MISMATCH",
                )

    # ═══════════════════════════════════════════════════════════════
    # SOW COMPARE RULES
    # ═══════════════════════════════════════════════════════════════
    sow_cmp = req.sow_compare or {}
    if sow_cmp:
        # SOW data is present — mark coverage.  Contract data (sla_windows,
        # volume_by_year) counts even when compare items are not yet entered.
        cov.sow = True
        # Support both canonical shape {metrics:[{key,label,sow,actual,pct,status}]}
        # (from /api/sow/compare and manual entry) and legacy {items:[...]} shape.
        _raw_items = sow_cmp.get("metrics") or sow_cmp.get("items") or []
        # Normalise: canonical shape uses {status} directly; legacy may use {zone}.
        items: list = []
        for _m in _raw_items:
            if not isinstance(_m, dict):
                continue
            _status = _m.get("status") or _m.get("zone") or ""
            # Compute status from pct when not already set
            if not _status and _m.get("pct") is not None:
                _pct = _f(_m["pct"])
                _status = ("HIGH" if _pct > 110
                           else "OPTIMAL" if _pct >= 90
                           else "ACCEPTABLE" if _pct >= 70
                           else "LOW")
            items.append({**_m, "status": _status, "label": _m.get("label") or _m.get("key") or "?"})
        # Standard: 70%-110% = acceptable window. Only outside this needs review.
        exceeded = [i for i in items if i.get("status") in ("HIGH", "EXCEEDS")]
        low_util = [i for i in items if i.get("status") in ("LOW", "UNDER")]
        in_range = [i for i in items if i.get("status") in ("OPTIMAL", "ACCEPTABLE")]

        # Compact volume formatter — keeps the headline numbers readable
        # (191000 → "191K", 1_500_000 → "1.5M") so the consultative question
        # reads the way a PE lead would phrase it to the customer.
        def _fmt_vol(v) -> str:
            n = _f(v)
            a = abs(n)
            if a >= 1_000_000:
                return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
            if a >= 1_000:
                return f"{n / 1_000:.0f}K"
            return f"{n:.0f}"

        # Detect whether the loaded dataset is a TEST/UAT run so the question
        # adapts: a TEST run with partial volume is often intentional scope, a
        # PROD run below the contracted floor is a forecast/commercial concern.
        _env_names = [
            str(s.get("Sub_Application") or s.get("sub_application") or "")
            for s in (sub_stats or [])
        ]
        _n_test = sum(1 for n in _env_names if _re_env_test(n))
        _n_prod = sum(1 for n in _env_names if _re_env_prod(n))
        _is_test_env = _n_test > 0 and _n_test >= _n_prod

        # Per-metric consultative question — names the real percentage and the
        # actual-vs-target volumes, then asks the specific "why / confirm with
        # customer" question a reviewer would raise before sign-off.
        def _low_question(m) -> str:
            label = m.get("label") or m.get("key") or m.get("metric") or "Volume metric"
            pct   = _f(m.get("pct"))
            act   = _fmt_vol(m.get("actual"))
            sow   = _fmt_vol(m.get("sow"))
            _lbl_l = label.lower()
            if "sku" in _lbl_l:
                tail = ("has the customer confirmed this represents the full item master "
                        "in scope for TEST?" if _is_test_env else
                        "confirm with the customer whether the item master in scope has shrunk.")
            elif _is_test_env:
                tail = "is this a TEST environment intentional partial load or a genuine data gap?"
            else:
                tail = ("why is production volume below the contracted floor — has the forecast "
                        "changed, or is upstream data incomplete?")
            return f"{label} is only {pct:.1f}% of SOW target ({act} vs {sow}) — {tail}"

        def _high_question(m) -> str:
            label = m.get("label") or m.get("key") or m.get("metric") or "Volume metric"
            pct   = _f(m.get("pct"))
            act   = _fmt_vol(m.get("actual"))
            sow   = _fmt_vol(m.get("sow"))
            return (f"{label} is at {pct:.1f}% of SOW target ({act} vs {sow}) — above the 110% "
                    f"ceiling; confirm with the customer whether the volume forecast and "
                    f"commercials need revision before sign-off.")

        if exceeded:
            names = ", ".join(i.get("label") or i.get("metric") or "?" for i in exceeded[:3])
            _q = " · ".join(_high_question(i) for i in exceeded[:3])
            add("critical", "📈",
                f"SOW volume above 110% ceiling: {len(exceeded)} metric(s) exceed contracted limit",
                f"{_q} Per standard process, consumption must remain within 70%-110% of approved SOW; "
                f"formal review and acknowledgment required.",
                source="sow",
                recommendation="Raise commercial review with the customer and document the deviation "
                               "formally before sign-off.",
                evidence_class="measured",
                root_cause="SOW_VOLUME_OVER")
        if low_util:
            names = ", ".join(i.get("label") or i.get("metric") or "?" for i in low_util[:3])
            _q = " · ".join(_low_question(i) for i in low_util[:3])
            _reco = ("Confirm with the customer that the TEST scope is intentional and the scenarios "
                     "are representative; formally acknowledge the deviation before sign-off."
                     if _is_test_env else
                     "Confirm with the customer whether the volume forecast changed or upstream data "
                     "is incomplete; formally acknowledge the deviation before sign-off.")
            add("warning", "📉",
                f"SOW volume below 70% floor: {len(low_util)} metric(s) under target — customer confirmation needed",
                f"{_q} Per standard process, consumption must remain within 70%-110% of approved SOW.",
                source="sow",
                recommendation=_reco,
                evidence_class="measured",
                root_cause="SOW_VOLUME_UNDER")
        if in_range and not exceeded and not low_util:
            add("ok", "📊",
                f"SOW utilisation within 70%-110% standard process window: {len(in_range)} metric(s)",
                "Volume consumption aligns with contracted SOW targets — no formal review required.",
                source="sow")

    # ── Legacy SOW DFU direct check ───────────────────────────────────────────
    sow_dfu  = _f(req.sow_dfu)
    sow_base = _f(req.sow_dfu_base or sow_dfu)
    if sow_dfu > 0 and sow_base > 0 and not sow_cmp:
        util_pct = (sow_dfu / sow_base) * 100
        if util_pct > 100:
            add("critical", "📦",
                f"SOW DFU exceeded: {util_pct:.1f}% of contracted baseline",
                "Volume above contract ceiling — commercial review required",
                source="sow")
        elif util_pct > 85:
            add("warning", "📦",
                f"SOW DFU at {util_pct:.1f}% of contracted baseline",
                "Approaching contract ceiling — plan for next cycle",
                source="sow")

    # ═══════════════════════════════════════════════════════════════
    # REGRESSION RULES — jobs with timing anomalies vs their own baseline
    # ═══════════════════════════════════════════════════════════════
    # Source: session_cache["regression_df"] = resp_dict["anomalies"] from
    # batch.py. Shape: [{Job_Name, run_hrs, avg_hrs, sigma, zscore, run_date}]
    # A z-score > 2.0 means the job ran > 2 standard deviations above its mean.
    _reg_data = req.anomalies or []  # re-read after session-cache enrichment
    if _reg_data:
        # Separate genuine outliers (zscore > 2) from mild deviations
        _regressions = [
            r for r in _reg_data
            if _f(r.get("zscore") or r.get("z_score") or 0) > 2.0
        ]
        _mild = [
            r for r in _reg_data
            if 1.5 < _f(r.get("zscore") or r.get("z_score") or 0) <= 2.0
        ]

        # Consolidate regressions into a single finding per severity tier
        # instead of one finding per job — reduces noise while preserving detail.
        _crit_regressions = [
            r for r in sorted(_regressions, key=lambda x: _f(x.get("zscore") or x.get("z_score") or 0), reverse=True)
            if _f(r.get("zscore") or r.get("z_score") or 0) > 3.0
        ]
        _warn_regressions = [
            r for r in sorted(_regressions, key=lambda x: _f(x.get("zscore") or x.get("z_score") or 0), reverse=True)
            if _f(r.get("zscore") or r.get("z_score") or 0) <= 3.0
        ]

        def _reg_line(r):
            jn = r.get("Job_Name") or r.get("job_name") or "?"
            hrs = _f(r.get("run_hrs") or r.get("peak_hrs") or 0)
            avg = _f(r.get("avg_hrs") or r.get("mean_hrs") or 0)
            zs = _f(r.get("zscore") or r.get("z_score") or 0)
            # Distinguish timeout/wait from slow execution using failure context
            _fc = _i(r.get("fail_count") or 0)
            _zf = r.get("has_zero_sec_failures", False)
            if _zf and hrs > 1.0:
                # Job had 0-sec failures with large End-Start gap = timeout/wait
                return f"{jn}: waited {hrs:.2f}h before timeout ({_fc} failure(s), z={zs:.1f}σ)"
            elif _fc > 0:
                return f"{jn}: peak {hrs:.2f}h vs avg {avg:.3f}h (z={zs:.1f}σ, {_fc} failure(s))"
            return f"{jn}: peak {hrs:.2f}h vs avg {avg:.3f}h (z={zs:.1f}σ)"

        def _reg_reco(regressions):
            """Build recommendation that adapts to failure context."""
            _has_timeouts = any(r.get("has_zero_sec_failures") for r in regressions)
            _names = [r.get("Job_Name") or r.get("job_name") or "?" for r in regressions[:5]]
            base = f"Investigate: {', '.join(_names)}."
            if _has_timeouts:
                return (f"{base} FileWatcher/polling jobs waited hours before timeout — "
                        "check upstream file delivery, scheduler trigger conditions, and "
                        "timeout configuration.")
            return (f"{base} Check job logs for SQL plan changes, data volume spikes, "
                    "or upstream dependency delays.")

        if _crit_regressions:
            _lines = [_reg_line(r) for r in _crit_regressions[:5]]
            _has_timeouts = any(r.get("has_zero_sec_failures") for r in _crit_regressions)
            _impact = ("Timeout/wait anomalies — jobs consumed batch window hours waiting "
                       "for upstream triggers that never arrived"
                       if _has_timeouts else
                       "Extreme runtime outliers indicate runaway jobs or data volume explosion")
            add("critical", "📈",
                f"{len(_crit_regressions)} job(s) with severe runtime anomaly (>3σ above baseline)",
                " · ".join(_lines),
                source="batch", evidence_class="measured",
                impact=_impact,
                recommendation=_reg_reco(_crit_regressions),
                root_cause="RUNTIME_REGRESSION")

        if _warn_regressions:
            _lines = [_reg_line(r) for r in _warn_regressions[:5]]
            add("warning", "📈",
                f"{len(_warn_regressions)} job(s) with moderate runtime anomaly (2-3σ above baseline)",
                " · ".join(_lines),
                source="batch", evidence_class="measured",
                impact="These jobs are running slower than normal — potential regression before breach",
                recommendation=_reg_reco(_warn_regressions),
                root_cause="RUNTIME_REGRESSION")

        if _mild:
            names = ", ".join(
                r.get("Job_Name") or r.get("job_name") or "?" for r in _mild[:3]
            )
            add("info", "📊",
                f"{len(_mild)} job(s) showing mild runtime growth (1.5–2σ above mean)",
                f"Jobs: {names} — not yet anomalous but worth monitoring for trend continuation",
                source="batch", evidence_class="measured",
                root_cause="RUNTIME_DRIFT")

    # ═══════════════════════════════════════════════════════════════
    # ADAPTIVE SLA RULES — per-job dynamic baselines vs configured SLA
    # ═══════════════════════════════════════════════════════════════
    # Source: session_cache["adaptive_sla"] = sla_mx_dict["job_baselines"].
    # Shape: [{job_name, baseline_hrs, p95_hrs, stddev, sla_hrs, recommendation}]
    # Fires when a job's observed p95 runtime is tighter than the configured SLA
    # ceiling — meaning the job's own statistical behaviour sets a de-facto SLA
    # more demanding than what's configured.
    _asl_data = req.issues or []   # re-read after session-cache enrichment
    # Only process if the items look like adaptive_sla baselines (have p95_hrs)
    _baselines = [
        b for b in _asl_data
        if isinstance(b, dict) and b.get("p95_hrs") is not None
    ]
    if _baselines:
        for b in sorted(_baselines, key=lambda x: _f(x.get("p95_hrs") or 0), reverse=True)[:8]:
            jn       = b.get("job_name") or b.get("Job_Name") or "?"
            p95      = _f(b.get("p95_hrs") or 0)
            baseline = _f(b.get("baseline_hrs") or b.get("avg_hrs") or 0)
            stddev   = _f(b.get("stddev") or 0)
            sla_h    = _f(b.get("sla_hrs") or b.get("sla_limit_hrs") or 0)
            reco     = b.get("recommendation") or ""

            if p95 <= 0 or sla_h <= 0:
                continue

            # Effective margin from p95 to SLA
            p95_buf_pct = round((sla_h - p95) / sla_h * 100, 1)

            if p95_buf_pct < 10:
                # p95 runtime is within 10% of SLA — statistically, ~5% of runs will breach
                add("warning", "🎯",
                    f"Adaptive baseline alert: '{jn}' p95={p95:.3f}h is {p95_buf_pct:.1f}% below SLA ceiling",
                    f"Statistical p95 runtime {p95:.3f}h vs {sla_h:.2f}h SLA. "
                    f"Baseline avg={baseline:.3f}h ± {stddev:.3f}h stddev. "
                    "With this variance, ~5% of runs are expected to exceed SLA.",
                    source="sla", evidence_class="measured",
                    impact=f"'{jn}' SLA is statistically at risk based on its own run history",
                    recommendation=reco or (
                        f"Reduce '{jn}' baseline runtime below {sla_h * 0.85:.3f}h (85% SLA) "
                        "to achieve statistical SLA compliance. Investigate data volume trends."
                    ),
                    root_cause="ADAPTIVE_SLA_TIGHT")

    # ═══════════════════════════════════════════════════════════════
    # ISSUES REGISTER RULES
    # ═══════════════════════════════════════════════════════════════
    # Only process issues that look like Issues Register entries (have Status field)
    _issue_register = [
        i for i in (req.issues or [])
        if isinstance(i, dict) and i.get("Status") is not None
    ]
    if _issue_register:
        open_issues = [i for i in _issue_register if i.get("Status") in ("Open", "In Progress")]
        if open_issues:
            crit_i = [i for i in open_issues if i.get("Severity") == "Critical"]
            if crit_i:
                ids = ", ".join(str(i.get("ID", "?")) for i in crit_i[:3])
                add("critical", "📋",
                    f"{len(crit_i)} critical open issue(s) in Issues Register",
                    f"IDs: {ids} — must be resolved before PE sign-off",
                    source="issues")
            else:
                add("warning", "📋",
                    f"{len(open_issues)} open issue(s) in Issues Register",
                    "Review and action all open issues before PE audit sign-off",
                    source="issues")

    # ═══════════════════════════════════════════════════════════════
    # INTELLIGENCE RULES — misleading green, contradictions, idle-time
    # ═══════════════════════════════════════════════════════════════

    # ── A1: Misleading Green Detection ────────────────────────────
    # Compliance looks OK only because summed runtime was used instead
    # of the full elapsed batch window.
    if has_batch:
        elapsed_info = bk.get("elapsed_window") if isinstance(bk, dict) else None
        if not elapsed_info:
            elapsed_info = (req.batch_kpis or {}).get("elapsed_window")
        summed_info = bk.get("summed_runtime") if isinstance(bk, dict) else None
        if not summed_info:
            summed_info = (req.batch_kpis or {}).get("summed_runtime")

        if elapsed_info and summed_info:
            el_worst = _f((elapsed_info.get("worst_day") or {}).get("elapsed_hrs"))
            su_worst = _f(summed_info.get("worst_day_hrs"))
            el_avail = elapsed_info.get("available", False)

            if el_avail and el_worst > 0 and su_worst > 0:
                # Use the detected SLA ceiling — never hardcode 6h
                from services import pe_config as _pec
                daily_lim = _f(bk.get("sla_ceiling") or bk.get("daily_limit_hrs") or _pec.SLA_DAILY_HRS)
                if daily_lim <= 0:
                    daily_lim = _pec.SLA_DAILY_HRS
                # Case: summed looks fine but elapsed breaches
                if su_worst <= daily_lim and el_worst > daily_lim:
                    add("critical", "🎭",
                        "Misleading compliance: summed runtime within SLA but elapsed window breaches",
                        f"Summed runtime peak: {su_worst:.1f}h (within {daily_lim:.1f}h SLA) "
                        f"but real elapsed window: {el_worst:.1f}h (exceeds SLA by {el_worst - daily_lim:.1f}h). "
                        f"Orchestration gaps, sequencing delays, or wait states inflate the real batch window.",
                        source="batch", confidence=90,
                        impact="Compliance report appears green but actual batch window breaches SLA — "
                               "customer may challenge this during sign-off review",
                        evidence=f"Summed runtime {su_worst:.1f}h vs elapsed window {el_worst:.1f}h vs SLA {daily_lim:.1f}h",
                        recommendation="Investigate orchestration gaps between jobs. Report elapsed window "
                                       "as primary metric instead of summed runtime.",
                        evidence_class="measured",
                        root_cause="ORCHESTRATION_GAP")

                # Case: large gap between elapsed and summed = idle-time anomaly (A8)
                if el_worst > su_worst * 1.3 and el_worst > 1.0:
                    idle_hrs = el_worst - su_worst
                    idle_pct = (idle_hrs / el_worst) * 100
                    if idle_pct > 20:
                        add("warning", "⏸️",
                            f"Orchestration idle-time anomaly: {idle_pct:.0f}% of batch window is idle",
                            f"Elapsed window {el_worst:.1f}h but jobs only consumed {su_worst:.1f}h — "
                            f"{idle_hrs:.1f}h of idle/wait time between jobs. "
                            f"May indicate sequential dependencies, resource waits, or scheduling gaps.",
                            source="batch", confidence=80,
                            impact="Batch window is inflated by non-productive time — "
                                   "optimisation opportunity or scheduling investigation required",
                            evidence=f"Elapsed {el_worst:.1f}h − Summed {su_worst:.1f}h = {idle_hrs:.1f}h idle ({idle_pct:.0f}%)",
                            recommendation="Review job dependency chains for unnecessary serialisation. "
                                           "Check if approved wait windows exist.",
                            evidence_class="measured",
                            root_cause="IDLE_TIME")

    # ── A4: Hidden Waiver Detection ───────────────────────────────
    # Scan SLA contracts and comments for waiver-like language that
    # isn't reflected in compliance logic.
    #
    # Rule 4 (waiver dedup): emit EXACTLY ONE finding regardless of how many
    # rows contain waiver language. Listing 3+ near-identical cards adds noise
    # without adding signal — the action is identical for all of them.
    if has_batch:
        try:
            from services import config_store as _cs
            sla_intel = _cs.get("_sla_intelligence")
        except Exception:
            sla_intel = None

        if sla_intel and isinstance(sla_intel, dict):
            waiver_keywords = ["waiver", "waived", "exception", "approved exception",
                               "no breach", "agreed", "unofficial", "not enforced",
                               "excluded", "exempted", "customer approved"]
            waiver_hits = []  # [(batch_name, source_row, found_keywords, comment_excerpt)]
            for contract in (sla_intel.get("contracts") or []):
                comments = (contract.get("comments") or "").lower()
                interp = (contract.get("interpretation_notes") or "").lower()
                combined = comments + " " + interp
                found_keywords = [k for k in waiver_keywords if k in combined]
                if found_keywords:
                    waiver_hits.append((
                        contract.get("batch_name", "?"),
                        contract.get("source_row", "?"),
                        found_keywords,
                        (contract.get("comments") or "")[:120],
                    ))

            if waiver_hits:
                n = len(waiver_hits)
                names = [h[0] for h in waiver_hits]
                rows  = [str(h[1]) for h in waiver_hits]
                # Union of all keywords seen (deduped, keep order)
                seen_kw, all_kw = set(), []
                for h in waiver_hits:
                    for k in h[2]:
                        if k not in seen_kw:
                            seen_kw.add(k)
                            all_kw.append(k)
                preview_names = ", ".join(names[:5]) + ("…" if n > 5 else "")
                title = (f"{n} possible waivers in SLA matrix — not in compliance calculations"
                         if n > 1 else
                         f"Possible waiver detected in SLA matrix: '{names[0]}'")
                detail = (f"Rows [{preview_names}] contain comment-based waiver language "
                          f"({', '.join(all_kw[:5])}). "
                          f"None of these are currently reflected in compliance calculations.")
                add("warning", "📜",
                    title,
                    detail,
                    source="sla", confidence=70,
                    impact="Compliance may be understating actual performance if "
                           "waivers should adjust SLA thresholds",
                    evidence=f"SLA matrix rows: {', '.join(rows[:10])}{'…' if n > 10 else ''}",
                    recommendation="Review with customer whether these waivers should be formally "
                                   "incorporated into SLA calculations or documented as exclusions.",
                    evidence_class="inferred",
                    root_cause="WAIVER_NOT_APPLIED")

    # ── A9: Suspicious Null/Zero Server Metrics ──────────────────
    if has_resource and servers:
        zero_cpu = [s for s in servers
                    if _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
                    and _f(s.get("mem_pct")) == 0]
        nonzero  = [s for s in servers
                    if _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) > 0
                    or _f(s.get("mem_pct")) > 0]
        if zero_cpu and nonzero:
            # Some servers have data, some don't — suspicious
            ratio = len(zero_cpu) / len(servers) * 100
            if ratio >= 30:
                names = ", ".join(s.get("host", "?").split(".")[0] for s in zero_cpu[:4])
                add("warning", "👻",
                    f"{len(zero_cpu)} server(s) ({ratio:.0f}%) report zero CPU + Memory",
                    f"Hosts: {names}{'…' if len(zero_cpu) > 4 else ''} — "
                    f"these servers appear in the report but have no measurable metrics. "
                    f"This may indicate monitoring gaps, powered-off hosts, or document parsing issues.",
                    source="resource", confidence=60,
                    impact="Fleet average and resource findings may be skewed by phantom servers "
                           "that dilute or inflate utilization statistics",
                    evidence=f"{len(zero_cpu)}/{len(servers)} servers with CPU=0% + Mem=0%",
                    recommendation="Verify these servers are in scope and have active monitoring. "
                                   "Exclude powered-off or decommissioned hosts from the report.",
                    evidence_class="inferred",
                    root_cause="MONITORING_GAP")

    # ── A10: Cross-File Data Contradictions ──────────────────────
    if has_batch and sla_ceil:
        # Check if SLA ceilings contradict the actual SLA used in batch analysis.
        # Compare across all schedule types, not just DAILY.
        sla_src = (req.batch_kpis or {}).get("sla_source") or {}
        _bc_ceiling = _f((req.batch_kpis or {}).get("sla_ceiling") or 0)
        # Compare the batch calculator's resolved ceiling vs uploaded ceilings
        _uploaded_ceil = _f(sla_ceil.get(_detected_sched if has_batch else "DAILY", 0))
        if _bc_ceiling > 0 and _uploaded_ceil > 0 and abs(_bc_ceiling - _uploaded_ceil) > 0.5:
            add("warning", "⚠️",
                f"SLA contradiction: batch engine uses {_bc_ceiling:.1f}h but SLA matrix says {_uploaded_ceil:.1f}h ({_detected_sched})",
                f"The batch compliance was calculated against {_bc_ceiling:.1f}h {_sched_label} SLA "
                f"but the uploaded SLA matrix specifies {_uploaded_ceil:.1f}h for {_detected_sched} schedule. "
                f"This mismatch may produce misleading compliance percentages.",
                source="batch", confidence=85,
                impact="Compliance results may be calculated against the wrong SLA value",
                evidence=f"batch_calculator {_sched_label} SLA: {_bc_ceiling:.1f}h vs sla_ceilings {_detected_sched}: {_uploaded_ceil:.1f}h",
                recommendation="Re-upload the SLA matrix and re-run batch analysis to align "
                               "SLA values across all engines.",
                evidence_class="measured",
                root_cause="SLA_MISMATCH")

    # ── Environment Comparison Warnings ──────────────────────────
    try:
        from services import config_store as _cs2
        env_data = None  # env detection results if available via appData
    except Exception:
        env_data = None

    # ═══════════════════════════════════════════════════════════════
    # NARRATIVE GENERATION — structured 5-7 line RCA verdict
    # ═══════════════════════════════════════════════════════════════
    # Produces a clean, technically precise PE audit verdict — each line
    # addresses one dimension: scope, compliance, root cause, impact,
    # evidence quality, and recommended action.
    crit_findings = [f for f in findings if f.level == "critical"]
    warn_findings = [f for f in findings if f.level == "warning"]
    ok_findings   = [f for f in findings if f.level == "ok"]

    # ── Line 1: Data Scope ──
    scope_parts = []
    if has_batch:
        total_runs = _i(bk.get("total_runs"))
        total_jobs = _i(bk.get("total_jobs"))
        date_span  = _i((batch_cov or {}).get("date_span_days", 0))
        scope_parts.append(f"{total_runs} batch runs across {total_jobs} jobs ({date_span}-day window)")
    if has_resource:
        # Only claim a server was "reviewed" when its metrics are actually readable.
        # Image-only / all-zero resource docs are detected but cannot be reviewed,
        # so the scope line must not imply an infrastructure assessment happened.
        _res_reviewed = bool(servers) and not all(
            _f(s.get("effective_cpu") or s.get("cpu_pct") or 0) == 0
            and _f(s.get("mem_pct")) == 0
            for s in servers
        )
        _n_srv = len(servers) if servers else _i(rk.get("total_servers"))
        if _res_reviewed:
            scope_parts.append(f"{_n_srv} server(s) reviewed")
        elif _n_srv:
            scope_parts.append(f"{_n_srv} server(s) detected (metrics unreadable — not reviewed)")
    if sla_loaded:
        scope_parts.append("customer SLA matrix applied")
    elif has_batch:
        scope_parts.append("default SLA thresholds applied (customer matrix not uploaded)")
    if bench:
        # Distinguish a batch-runtime benchmark (PROD-vs-TEST job timings) from a
        # UI transaction benchmark — they are different measurement types.
        _bench_label = ("batch runtime benchmark included"
                        if (isinstance(bench, dict) and bench.get("kind") == "batch")
                        else "UI benchmark comparison included")
        scope_parts.append(_bench_label)
    if sow_cmp:
        scope_parts.append("SOW volume comparison included")
    line_scope = f"Scope: {'; '.join(scope_parts)}." if scope_parts else ""

    # ── Line 2: Compliance Summary ──
    if has_batch:
        comp_pct = _f(bk.get("compliance_pct"))
        breach_n = _i(bk.get("jobs_breach"))
        atrisk_n = _i(bk.get("jobs_at_risk"))
        line_compliance = (
            f"Compliance: {comp_pct:.1f}% job-level SLA compliance"
            f" — {breach_n} breach(es), {atrisk_n} at-risk."
        )
    elif has_resource:
        avg_cpu = _f(rk.get("avg_cpu"))
        avg_mem = _f(rk.get("avg_mem"))
        n_crit_s = _i(rk.get("n_critical"))
        fleet_grade = rk.get("fleet_grade", "?")
        line_compliance = (
            f"Fleet: avg CPU {avg_cpu:.1f}%, avg memory {avg_mem:.1f}%"
            f" — {n_crit_s} critical server(s), fleet grade {fleet_grade}."
        )
    else:
        line_compliance = ""

    # ── Line 3: Root Cause Analysis ──
    root_causes = set()
    for cf in crit_findings + warn_findings:
        rc = cf.root_cause
        if rc:
            root_causes.add(rc.replace("_", " ").lower())
    if root_causes:
        rc_list = sorted(root_causes)[:4]
        line_rca = f"Root causes identified: {', '.join(rc_list)}."
    elif crit_findings:
        line_rca = "Root cause: unclassified critical findings require manual investigation."
    elif warn_findings:
        line_rca = "Root cause: no critical blockers; warnings relate to threshold proximity."
    else:
        line_rca = ""

    # ── Line 4: Impact Statement ──
    if crit_findings:
        blocker_sources = set(f.source for f in crit_findings if f.source)
        line_impact = (
            f"Impact: {len(crit_findings)} critical finding(s) across "
            f"{', '.join(sorted(blocker_sources)) or 'multiple pillars'}"
            f" block PE sign-off until resolved."
        )
    elif warn_findings:
        line_impact = (
            f"Impact: {len(warn_findings)} warning(s) require documented acknowledgement "
            f"before conditional sign-off can proceed."
        )
    else:
        line_impact = "Impact: all reviewed metrics within acceptable operational thresholds."

    # ── Line 5: Evidence Quality ──
    measured_count  = sum(1 for f in findings if f.evidence_class == "measured")
    defaulted_count_n = sum(1 for f in findings if f.evidence_class == "defaulted")
    inferred_count_n  = sum(1 for f in findings if f.evidence_class == "inferred")
    total_evidence = measured_count + defaulted_count_n + inferred_count_n
    if total_evidence > 0:
        measured_pct = (measured_count / total_evidence) * 100
        line_evidence = (
            f"Evidence: {measured_pct:.0f}% of findings backed by direct measurement"
        )
        if defaulted_count_n > 0:
            line_evidence += f", {defaulted_count_n} use assumed defaults"
        if inferred_count_n > 0:
            line_evidence += f", {inferred_count_n} inferred from document snapshots"
        line_evidence += "."
    else:
        line_evidence = ""

    # ── Line 6: Decision ──
    if crit_findings:
        line_decision = (
            f"Decision: BLOCKED. Resolve all {len(crit_findings)} critical item(s) "
            f"and re-run analysis before customer review."
        )
    elif warn_findings:
        line_decision = (
            "Decision: CONDITIONAL. Obtain owner acknowledgement on each warning "
            "to proceed with sign-off."
        )
    else:
        line_decision = "Decision: APPROVED. Proceed to customer sign-off."

    # ── Line 7 (optional): Worst offender ──
    line_worst = ""
    if crit_findings:
        worst = crit_findings[0]
        worst_text = (worst.text or "")[:80]
        line_worst = f"Primary blocker: {worst_text}."

    # Assemble lines (filter empty)
    verdict_lines = [l for l in [
        line_scope, line_compliance, line_rca,
        line_impact, line_evidence, line_decision, line_worst,
    ] if l]

    narrative_text = "\n".join(verdict_lines)
    add("info", "📝",
        "Audit Narrative",
        narrative_text,
        source="", evidence_class="measured",
        root_cause="")

    # Re-check sow_loaded after session-cache enrichment (sow_cmp may have been
    # populated by the enrichment block above).  Also detect SOW contract data
    # that has sla_windows/volume_by_year but no compare items yet.
    sow_loaded = sow_loaded or bool(sow_cmp)
    if not sla_loaded and cov.sla:
        # SLA matrix was loaded from session cache during enrichment
        sla_loaded = True

    # ── OPEN AUDIT GAPS ──────────────────────────────────────────
    gaps = []
    if not sla_loaded:
        gaps.append("Customer SLA matrix not uploaded — compliance uses assumed defaults")
    if not has_resource:
        gaps.append("No resource utilization report — cannot correlate infra with batch performance")
    if not bench:
        gaps.append("No UI benchmark report — cannot validate user-facing performance")
    if not sow_cmp and not (sow_dfu > 0):
        gaps.append("No SOW/volume comparison — cannot confirm workload within contract limits")
    if has_batch and not (batch_cov or {}).get("has_end_time"):
        gaps.append("End_Time column missing from Ctrl-M export — elapsed window unavailable")
    if has_batch and _i((batch_cov or {}).get("date_span_days")) < 30:
        gaps.append(f"Only {_i((batch_cov or {}).get('date_span_days'))} day(s) of batch data — "
                    "30-day minimum recommended for PE audit")
    if not issues:
        gaps.append("No issues register uploaded — open issues not tracked")

    if gaps:
        add("info", "📋",
            f"Open Audit Gaps: {len(gaps)} area(s) require attention",
            " · ".join(gaps),
            source="", evidence_class="measured",
            impact="Audit may be challenged if these gaps are not addressed",
            recommendation="Upload missing evidence files and re-run analysis to close gaps.")

    # ═══════════════════════════════════════════════════════════════
    # AUDIT READINESS SUMMARY — decision-driven, not score-driven
    # ═══════════════════════════════════════════════════════════════
    crit_count = sum(1 for f in findings if f.level == "critical")
    warn_count = sum(1 for f in findings if f.level == "warning")

    # Identify blockers for sign-off decision strip
    blockers = [f.text for f in findings if f.level == "critical"]
    blocker_causes = []
    if any("batch window" in b.lower() for b in blockers):
        blocker_causes.append("batch-window overruns")
    if any("cpu" in b.lower() or "critical state" in b.lower() for b in blockers):
        blocker_causes.append("critical CPU/resource concentration")
    if any("sla" in b.lower() and "breach" in b.lower() for b in blockers):
        blocker_causes.append("SLA breaches")
    if any("evidence" in b.lower() or "insufficient" in b.lower() for b in blockers):
        blocker_causes.append("incomplete evidence")
    if any("misleading" in b.lower() for b in blockers):
        blocker_causes.append("misleading compliance (elapsed vs summed)")
    if any("waiver" in b.lower() for b in [f.text for f in findings if f.level == "warning"]):
        blocker_causes.append("unresolved waivers in SLA matrix")

    # Count evidence classes
    defaulted_count = sum(1 for f in findings if f.evidence_class == "defaulted")
    inferred_count  = sum(1 for f in findings if f.evidence_class == "inferred")
    unavail_count   = sum(1 for f in findings if f.evidence_class == "unavailable")

    if crit_count == 0 and warn_count == 0:
        sources_loaded = sum([
            has_batch, has_resource,
            bool(sla), bool(bench), bool(sow_cmp),
        ])
        trust_note = ""
        if defaulted_count > 0:
            trust_note = f" · {defaulted_count} finding(s) use default SLA (not customer-approved)"
        if inferred_count > 0:
            trust_note += f" · {inferred_count} finding(s) from document snapshots (not time-series)"
        add("ok", "🏆",
            f"PE audit ready — no critical or warning findings across {sources_loaded} data source(s)",
            f"Customer: {req.customer_name or 'Unknown'} — all reviewed areas within acceptable thresholds"
            + trust_note,
            source="", evidence_class="measured")
    elif crit_count > 0:
        cause_str = ", ".join(blocker_causes) if blocker_causes else "see critical findings above"
        add("info", "⛔",
            f"PE sign-off blocked — {crit_count} critical finding(s), {warn_count} warning(s)",
            f"Ready for sign-off: NO · Blockers: {cause_str}. "
            f"Address all critical items and re-run analysis.",
            source="", evidence_class="measured")

    # ═══════════════════════════════════════════════════════════════
    # POST-PROCESSING: Generic deduplication
    # ═══════════════════════════════════════════════════════════════
    # Merge findings that share the same root_cause AND level.
    # This prevents future rule additions from creating overlapping findings
    # without needing manual cross-rule coordination.
    _dedup: dict[tuple[str, str], int] = {}  # (root_cause, level) → first index
    _remove_idxs: set[int] = set()
    for i, f in enumerate(findings):
        if not f.root_cause:
            continue  # No root_cause tag — can't dedup
        key = (f.root_cause, f.level)
        if key in _dedup:
            # Merge sub-text of duplicate into the first occurrence
            first_idx = _dedup[key]
            first = findings[first_idx]
            if f.sub and f.sub not in first.sub:
                findings[first_idx] = first.model_copy(
                    update={"sub": first.sub + " · " + f.sub}
                )
            _remove_idxs.add(i)
        else:
            _dedup[key] = i
    if _remove_idxs:
        findings = [f for i, f in enumerate(findings) if i not in _remove_idxs]

    # Priority sort: critical → warning → info → ok
    _order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    findings.sort(key=lambda f: _order.get(f.level, 9))
    return findings, cov


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post(
    "/generate-findings",
    response_model=FindingsResponse,
    summary="Run rule-based PE Audit Findings engine v2",
)
async def generate_findings(body: FindingsRequest) -> FindingsResponse:
    try:
        return await _generate_findings_impl(body)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("generate_findings: unhandled exception — %s\n%s", exc, tb)
        # Return a valid response with a single error finding rather than a 500.
        # This keeps the UI functional while the RCA is logged above.
        err_finding = Finding(
            level="warning", icon="⚠️",
            text=f"Findings engine error: {type(exc).__name__}: {str(exc)[:120]}",
            sub="An unexpected error occurred in the PE Findings rule engine. "
                "Check server logs for the full traceback. Re-upload source files and retry.",
            source="", confidence=0, evidence_class="unavailable",
            root_cause="ENGINE_ERROR",
        )
        return FindingsResponse(
            findings=[err_finding],
            summary=FindingsSummary(warning=1, total=1),
            data_coverage=DataCoverage(),
            audit_coverage=AuditCoverage(
                evidence_30day="missing", sla_source="missing",
                waivers="missing", ui_signoff="missing",
                automation_status="missing", volume_vs_sow="missing",
                confidence=0, confidence_label="INSUFFICIENT",
            ),
            penalty_score=0.0,
            findings_grade="F",
            findings_grade_label="ERROR — check server logs",
        )


async def _generate_findings_impl(body: FindingsRequest) -> FindingsResponse:
    findings, cov = _generate(body)
    summary = FindingsSummary(
        critical=sum(1 for f in findings if f.level == "critical"),
        warning =sum(1 for f in findings if f.level == "warning"),
        info    =sum(1 for f in findings if f.level == "info"),
        ok      =sum(1 for f in findings if f.level == "ok"),
        total   =len(findings),
    )

    # NOTE: LLM narrative refinement was intentionally removed from this path.
    # The deterministic narrative lines (Scope / Compliance / Root cause / Impact /
    # Evidence / Decision / Primary blocker) are already well-structured and appear
    # in the verdict hero immediately.  LLM polish happens via the separate
    # /api/smart-findings call that the frontend fires in the background — it never
    # blocks the rule-engine response.

    # ── Build PE Audit Coverage Strip ──────────────────────────────────────────
    # Uses shared _get_data_coverage() helper — no import-inside-function.
    batch_cov_data = _get_data_coverage(body.batch_kpis) or {}
    sla_ceil = body.sla_ceilings or {}

    date_span = _i(batch_cov_data.get("date_span_days"))
    if date_span >= 30:
        ev_30 = "loaded"
    elif date_span >= 14:
        ev_30 = "partial"
    else:
        ev_30 = "missing"

    sla_src = "customer" if sla_ceil else (
        "customer" if cov.sla else (
            "default" if cov.batch else "missing"
        )
    )

    # Check if waivers were detected in findings
    waiver_findings = [f for f in findings if "waiver" in (f.text or "").lower()
                       or f.root_cause == "WAIVER_NOT_APPLIED"]
    waiver_status = "loaded" if waiver_findings else "missing"

    # ── Audit confidence: weighted by loaded data sources ──────
    # Each pillar contributes to overall audit confidence:
    #   batch=25, resource=25, sla=20, sow=15, benchmark=15
    _conf = 0
    if cov.batch:
        _batch_conf = _f((batch_cov_data or {}).get("confidence", 75))
        _conf += min(_batch_conf * 0.25, 25)
    if cov.resource:
        _conf += 25
    if cov.sla:
        _conf += 20
    if cov.sow:
        _conf += 15
    if cov.benchmark:
        _conf += 15
    _conf = min(int(_conf), 100)
    _conf_label = (
        "HIGH" if _conf >= 75 else
        "MEDIUM" if _conf >= 50 else
        "LOW" if _conf >= 30 else "INSUFFICIENT"
    )

    # Also check if SLA was enriched from cache (cov.sla set during SLA rules)
    if cov.sla and sla_src == "missing":
        sla_src = "default"

    audit_cov = AuditCoverage(
        evidence_30day    = ev_30,
        sla_source        = sla_src,
        waivers           = waiver_status,
        ui_signoff        = "missing",  # placeholder — no UI signoff upload yet
        automation_status = "missing",  # placeholder
        volume_vs_sow     = "loaded" if cov.sow else "missing",
        confidence        = _conf,
        confidence_label  = _conf_label,
    )

    # ── Patch I: Unified grade computation ────────────────────────────────────
    _n_crit = sum(1 for f in findings if f.level == "critical")
    _n_warn = sum(1 for f in findings if f.level == "warning")
    _n_ok   = sum(1 for f in findings if f.level == "ok")
    penalty_score = max(0.0, min(100.0,
        100.0 - (_n_crit * 15.0) - (_n_warn * 5.0) + (_n_ok * 2.0)
    ))
    if   penalty_score >= 90: _grade, _glabel = "A", "APPROVED"
    elif penalty_score >= 75: _grade, _glabel = "B", "APPROVED WITH NOTES"
    elif penalty_score >= 60: _grade, _glabel = "C", "CONDITIONAL HOLD"
    elif penalty_score >= 45: _grade, _glabel = "D", "BLOCKED — MINOR"
    else:                      _grade, _glabel = "F", "BLOCKED — MAJOR"
    # Hard floor: any critical finding caps the grade at C regardless of ok count.
    # Prevents "3 breaches + 20 passing checks = Grade A" dilution.
    if _n_crit > 0 and _grade in ("A", "B"):
        _grade, _glabel = "C", "CONDITIONAL HOLD"
    # Persist so executive.py can blend it into OSHS (Patch F)
    try:
        if _session_cache:
            _session_cache.ac_set("findings_penalty_score", round(penalty_score, 1))
    except Exception as _e:
        log.debug("findings: penalty score cache failed: %s", _e)
    # ── end grade ─────────────────────────────────────────────────────────────

    # ── Gap A: failure-density grid (Sub_Application × run_date) ──────────────
    # Computed once in batch_calculator.build_batch_payload and cached by the
    # batch router. Read it here so the PE Findings page renders the heatmap
    # straight from the findings response. Fall back to a request-supplied grid.
    _failure_grid = None
    try:
        if _session_cache:
            _failure_grid = _session_cache.ac_get("failure_grid") or None
    except Exception as _e:
        log.debug("findings: failure_grid cache read failed: %s", _e)
    if not _failure_grid:
        _fg_body = getattr(body, "failure_grid", None)
        if isinstance(_fg_body, dict) and _fg_body.get("has_data"):
            _failure_grid = _fg_body

    # Patch J: include penalty_score fields in response
    resp = FindingsResponse(
        findings             = findings,
        summary              = summary,
        data_coverage        = cov,
        audit_coverage       = audit_cov,
        penalty_score        = round(penalty_score, 1),
        findings_grade       = _grade,
        findings_grade_label = _glabel,
        failure_grid         = _failure_grid,
    )
    # Cache so agent tools can list/read findings without recomputing.
    try:
        if _session_cache:
            _session_cache.set("last_findings", resp.model_dump())
    except Exception as _e:
        log.debug("findings: cache write failed: %s", _e)
    return resp


# ── Smart findings endpoint — Claude-grade structured briefing ─────────────
@router.post(
    "/smart-findings",
    summary="Apply FINDINGS OUTPUT RULES + Gemma verdict to the rule engine",
)
async def smart_findings(body: FindingsRequest) -> dict:
    """Run the rule engine, then post-process via services.smart_findings."""
    try:
        return await _smart_findings_impl(body)
    except Exception as exc:
        tb = traceback.format_exc()
        log.error("smart_findings: unhandled exception — %s\n%s", exc, tb)
        return {
            "findings": [], "verdict": {}, "next_actions": [], "open_gaps": [],
            "_error": f"{type(exc).__name__}: {str(exc)[:120]}",
        }


async def _smart_findings_impl(body: FindingsRequest) -> dict:
    """Run the rule engine, then post-process via services.smart_findings.

    The deterministic dedup + verdict + next-actions runs in-process.
    Gemma is invoked best-effort to produce a 15-word verdict summary;
    the call has a hard timeout and falls back to a deterministic line so
    the UI never waits more than a few seconds.
    """
    import asyncio
    from services.smart_findings import smartify

    # 1. Deterministic rule engine (fast, no LLM)
    raw_findings, cov = _generate(body)
    raw_dicts = [f.model_dump() for f in raw_findings]

    # 2. Dedup + structure (synchronous, O(N))
    # Build KPI evidence for severity mismatch detection
    kpi_ev: dict = {}
    if body.batch_kpis:
        kpi_ev["batch"] = body.batch_kpis
    if body.resource_kpis:
        kpi_ev["resource"] = body.resource_kpis
    if body.sla_matrix:
        kpi_ev["sla"] = body.sla_matrix
    smart = smartify(raw_dicts, customer_name=body.customer_name,
                     kpi_evidence=kpi_ev or None)

    # 3. LLM enrichment — two jobs in one structured call:
    #    A) Enrich findings that are missing root_cause/impact/recommendation
    #    B) Generate a concise verdict headline
    #    Both are data-driven prompts with actual numbers; LLM is not guessing.
    try:
        from services.ai_narrator import narrate
        import asyncio

        # Identify findings that need LLM enrichment (missing key fields)
        findings_needing_enrichment = [
            {"id": i, "severity": f["severity"], "title": f["title"],
             "one_line": f.get("one_line", ""), "source": f.get("source", ""),
             "root_cause": f.get("root_cause", ""), "impact": f.get("impact", ""),
             "action": f.get("action", "—")}
            for i, f in enumerate(smart["findings"])
            if f["level"] in ("critical", "warning")
            and (not f.get("root_cause") or not f.get("impact") or not f.get("action") or f.get("action") == "—")
        ][:8]  # Cap at 8 to keep prompt tight

        # Build context from KPIs for accurate enrichment
        kpi_context = {}
        if body.batch_kpis:
            bk = body.batch_kpis
            kpi_context["batch"] = {
                "compliance_pct": bk.get("compliance_pct"),
                "batch_window_compliance": bk.get("batch_window_compliance"),
                "window_breach_days": bk.get("window_breach_days"),
                "jobs_breach": bk.get("jobs_breach"),
                "total_runs": bk.get("total_runs"),
                "total_jobs": bk.get("total_jobs"),
                "daily_limit_hrs": bk.get("daily_limit_hrs"),
            }
        if body.resource_kpis:
            rk = body.resource_kpis
            kpi_context["resource"] = {
                "fleet_grade": rk.get("fleet_grade"),
                "n_critical": rk.get("n_critical"),
                "n_warning": rk.get("n_warning"),
                "avg_cpu": rk.get("avg_cpu"),
                "avg_mem": rk.get("avg_mem"),
            }
        if body.sla_matrix:
            sm = body.sla_matrix
            kpi_context["sla"] = {
                "compliance_pct": sm.get("compliance_pct"),
                "window_compliance_pct": sm.get("window_compliance_pct"),
                "window_breach_days": sm.get("window_breach_days"),
                "breaching_runs": sm.get("breaching_runs"),
                "worst_job": sm.get("worst_job"),
                "worst_hrs": sm.get("worst_hrs"),
            }

        enrichment_payload = {
            "customer": body.customer_name or "Unknown",
            "decision": smart["verdict"]["decision"],
            "findings_to_enrich": findings_needing_enrichment,
            "kpi_context": kpi_context,
        }

        # Run enrichment + verdict in parallel with a 15s budget
        async def _enrich():
            try:
                text, model = await asyncio.wait_for(
                    asyncio.to_thread(
                        narrate, "findings_enrich", enrichment_payload,
                        max_tokens=600, temperature=0.1,
                    ),
                    timeout=15.0,
                )
                return text, model
            except Exception:
                return None, None

        async def _verdict():
            try:
                verdict_payload = {
                    "decision": smart["verdict"]["decision"],
                    "blocker_count": smart["verdict"]["blocker_count"],
                    "warning_count": smart["verdict"]["warning_count"],
                    "top_findings": [
                        {"severity": f["severity"], "title": f["title"],
                         "evidence": f["evidence"], "impact": f.get("impact", "")}
                        for f in smart["findings"][:5]
                        if f["level"] in ("critical", "warning")
                    ],
                    "customer": smart["verdict"]["customer"],
                    "kpi_context": kpi_context,
                }
                text, model = await asyncio.wait_for(
                    asyncio.to_thread(
                        narrate, "smart_verdict_15w", verdict_payload,
                        max_tokens=80, temperature=0.2,
                    ),
                    timeout=12.0,
                )
                return text, model
            except Exception:
                return None, None

        (enrich_text, enrich_model), (verdict_text, verdict_model) = await asyncio.gather(
            _enrich(), _verdict()
        )

        # Apply LLM enrichment to findings
        if enrich_text:
            import json as _json
            try:
                # Expected: [{id, root_cause, impact, recommendation}]
                enrichments = _json.loads(enrich_text)
                if isinstance(enrichments, list):
                    enrich_map = {e["id"]: e for e in enrichments if isinstance(e, dict) and "id" in e}
                    for i, f in enumerate(smart["findings"]):
                        if i in enrich_map:
                            e = enrich_map[i]
                            if e.get("root_cause") and not f.get("root_cause"):
                                f["root_cause"] = str(e["root_cause"])[:80]
                            if e.get("impact") and (not f.get("impact") or f.get("impact") == "—"):
                                f["impact"] = str(e["impact"])[:150]
                            if e.get("recommendation") and (not f.get("action") or f.get("action") == "—"):
                                f["action"] = str(e["recommendation"])[:120]
                    smart["verdict"]["enrich_model"] = enrich_model
            except Exception:
                pass  # JSON parse error — enrichment silently skipped

        # Apply LLM verdict summary
        if verdict_text:
            smart["verdict"]["summary"]    = verdict_text.strip().rstrip(".") + "."
            smart["verdict"]["ai_model"]   = verdict_model
            smart["verdict"]["ai_powered"] = True
        else:
            smart["verdict"]["ai_powered"] = False
            smart["verdict"]["ai_note"]    = "AI verdict timed out — using deterministic summary"

    except Exception:
        smart["verdict"]["ai_powered"] = False

    # 4. Attach data coverage + audit coverage (re-use the rich shape)
    smart["data_coverage"] = cov.model_dump()

    # 5. Cache smart findings so PE Narrative can cross-validate verdict
    try:
        from services import session_cache
        session_cache.set("last_smart_findings", smart)
    except Exception:
        pass

    return smart
