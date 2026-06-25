"""
Executive Dashboard API — computes all 5 correlation formulas and returns
the full payload needed by the Plotly-based executive dashboard frontend.

POST /api/executive-dashboard
  Body: {batch_kpis, top_jobs, top_breaches, resource_kpis, servers,
         sla_data, sub_stats, window}
  Returns: {kpis, oshs, sub_app_metrics, server_heatmap, temporal,
            waterfall, narrative, job_sla_bars}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from services import pe_config
from services.pe_utils import coerce_float as _f
from services.correlation_engine import (
    calc_rfcs,
    calc_sri,
    calc_jrtos,
    calc_crs,
    calc_oshs,
    derive_batch_score,
    derive_resource_score,
    derive_sla_score,
    build_sub_app_metrics,
    generate_narrative,
)

router = APIRouter()


class ExecDashRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_kpis:    Optional[Dict[str, Any]]       = None
    top_jobs:      Optional[List[Dict[str, Any]]] = None
    top_breaches:  Optional[List[Dict[str, Any]]] = None
    resource_kpis: Optional[Dict[str, Any]]       = None
    servers:       Optional[List[Dict[str, Any]]] = None
    sla_data:      Optional[Dict[str, Any]]       = None
    sub_stats:     Optional[List[Dict[str, Any]]] = None
    window:        Optional[List[Dict[str, Any]]] = None
    hourly_counts: Optional[Dict[str, Any]]       = None
    sow_compare:   Optional[Dict[str, Any]]       = None
    findings:      Optional[List[Dict[str, Any]]] = None
    daily_jobs:    Optional[Dict[str, Any]]       = None  # { "YYYY-MM-DD": [{job, start_hr, end_hr}] }
    customer_name: Optional[str]                  = None
    deep_dive:     Optional[Dict[str, Any]]       = None  # time-series spike evidence


@router.post("/executive-dashboard")
def executive_dashboard(body: ExecDashRequest) -> Dict[str, Any]:
    bk       = body.batch_kpis    or {}
    rk       = body.resource_kpis or {}
    servers  = body.servers       or []
    top_jobs = body.top_jobs      or []
    breaches = body.top_breaches  or []
    sla_data = body.sla_data      or {}
    sub_stats = body.sub_stats    or []
    window   = body.window        or []

    sla_ceiling = float(bk.get("daily_limit_hrs") or pe_config.SLA_DAILY_HRS)

    # ── Per-sub-app ceiling map (BUG-M2/BUG-W5 fix) ─────────────
    # Prefer the shared build_ceiling_map result stored at compute time;
    # fall back to building it now from session_cache._batch_sla_xlsx.
    _ceiling_map: dict[str, float] = {}
    try:
        from services import session_cache as _sc_cm
        from services import compliance_engine as _ce_exec
        from services import config_store as _cs_exec
        _ceiling_map = _sc_cm.ac_get("ceiling_map") or {}
        if not _ceiling_map:
            _xlsx_cfg = (_cs_exec.get("_batch_sla_xlsx") or
                         _cs_exec.get("batch_sla_xlsx") or None)
            _all_sas = list({
                (j.get("Sub_Application") or j.get("sub_application") or "")
                for j in top_jobs if (j.get("Sub_Application") or j.get("sub_application"))
            })
            if _xlsx_cfg and _all_sas:
                _ceiling_map = _ce_exec.build_ceiling_map(
                    sub_applications=_all_sas,
                    xlsx_config=_xlsx_cfg,
                    pe_config_ref=pe_config,
                )
    except Exception:
        pass

    # ── Averages ────────────────────────────────────────────────
    known = [s for s in servers if _f(s.get("cpu_used")) > 0 or _f(s.get("mem_used")) > 0]
    avg_cpu  = _avg(known, "cpu_used")
    avg_mem  = _avg(known, "mem_used")
    avg_disk = _avg(known, "disk_used_max")
    peak_cpu = max((_f(s.get("cpu_used")) for s in known), default=0.0)

    crit_count = sum(1 for s in known if _f(s.get("cpu_used")) >= 90)

    compliance = _f(bk.get("compliance_pct"), 100.0)
    fail_rate  = 100.0 - compliance if compliance <= 100 else 0.0
    total_jobs = max(int(bk.get("total_jobs") or 0), len(top_jobs))

    # ── Formula 1: RFCS ─────────────────────────────────────────
    rfcs = calc_rfcs(fail_rate, avg_cpu, avg_mem, crit_count)

    # ── Formula 2: SRI per job (per-sub-app ceiling + outlier 3× cap) ──────────
    job_sla_bars = []
    for j in top_jobs[:20]:
        sub = (j.get("Sub_Application") or j.get("sub_application") or "").upper().strip()
        job_ceiling = float(_ceiling_map.get(sub) or sla_ceiling)
        peak = _f(j.get("peak_hrs"))
        buf_raw = j.get("buffer_pct")           # None = truly unknown — do NOT default to 50

        # Outlier guard: cap SRI at 1.0 when peak ≥ 3× ceiling (don't break 0–1 scale)
        if job_ceiling > 0 and peak >= job_ceiling * 3.0:
            sri    = 1.0
            status = "BREACH"
            buf    = min(float(buf_raw) if buf_raw is not None else -1.0, -1.0)
        else:
            sri = calc_sri(peak, job_ceiling, avg_cpu)
            if buf_raw is None:
                status = "UNKNOWN"
                buf = None
            else:
                buf = float(buf_raw)
                status = "BREACH" if buf < 0 else ("AT_RISK" if buf < 15 else "OK")

        job_sla_bars.append({
            "job_name":    j.get("Job_Name", "?"),
            "sub_app":     j.get("Sub_Application") or j.get("sub_application", ""),
            "peak_hrs":    round(peak, 2),
            "sla_ceiling": round(job_ceiling, 3),
            "buffer_pct":  round(buf, 1) if buf is not None else None,
            "sri":         round(sri, 3),
            "status":      status,
        })
    job_sla_bars.sort(key=lambda x: x["sri"], reverse=True)

    # ── Formula 3: JRTOS (temporal) ──────────────────────────────
    # Prefer real hourly counts from batch processing (Start_Time.dt.hour)
    hc = body.hourly_counts or {}
    hourly_jobs: dict[int, int] = {
        int(k): int(v) for k, v in (hc.get("hourly_jobs") or {}).items()
    }
    hourly_fails: dict[int, int] = {
        int(k): int(v) for k, v in (hc.get("hourly_fails") or {}).items()
    }

    if not hourly_jobs and window:
        # Fallback heuristic: weighted distribution matching real batch ramp-and-drain shape.
        # Batch windows typically spike at start (20:00-22:00) then drain through to ~04:00.
        # GAP-D4: removed flat 0.12 factor and dead total_batch_hrs variable.
        days = len(window) or 1
        avg_jobs_per_day = total_jobs / days
        BATCH_HOUR_WEIGHTS = {
            20: 0.18, 21: 0.16, 22: 0.13, 23: 0.10,
             0: 0.09,  1: 0.08,  2: 0.07,  3: 0.06,  4: 0.05,
        }
        for h in range(24):
            w_factor = BATCH_HOUR_WEIGHTS.get(h, 0.01)
            hourly_jobs[h]  = round(avg_jobs_per_day * w_factor)
            hourly_fails[h] = round(hourly_jobs[h] * fail_rate / 100.0)

    temporal = calc_jrtos(hourly_jobs, hourly_fails, peak_cpu)

    # ── Formula 4+5: Sub-app metrics (BUG-W5 fix — pass ceiling_map for per-sub-app SRI/CRS) ─
    sub_app_metrics = build_sub_app_metrics(top_jobs, servers, sla_ceiling,
                                            ceiling_map=_ceiling_map)

    # ── OSHS computation ─────────────────────────────────────────
    # Window compliance = the contracted daily batch-window on-time rate. It is
    # the binding "did the batch finish inside its window" signal and MUST flow
    # into the score — a batch can satisfy every per-job SLA yet blow its nightly
    # window, so neither the batch nor the SLA component may read 100 while
    # windows breach. (Reads window_compliance_pct, falls back to the legacy key.)
    _win_comp_raw = bk.get("window_compliance_pct")
    if _win_comp_raw is None:
        _win_comp_raw = bk.get("batch_window_compliance")
    _win_comp = _f(_win_comp_raw) if _win_comp_raw is not None else None

    # Batch component: job-level health floored by window compliance, so a batch
    # that breaches its windows can never present a clean 100.
    _eff_batch_compliance = compliance if _win_comp is None else min(compliance, _win_comp)
    batch_score    = derive_batch_score(_eff_batch_compliance, fail_rate)
    resource_score = derive_resource_score(avg_cpu, avg_mem, avg_disk)

    # SLA component: contractual window compliance. Prefer the SLA Matrix tab's
    # number; fall back to the batch window compliance; only use job-level
    # compliance as a last resort when no window data exists at all.
    sla_compliance_raw = sla_data.get("compliance_pct")   # None = SLA Matrix not run
    sla_breaches   = int(sla_data.get("breaching_runs") or bk.get("jobs_breach") or 0)
    sla_total      = int(sla_data.get("total_runs") or total_jobs or 1)
    if sla_compliance_raw is None:
        sla_compliance_raw = _win_comp        # batch window compliance
    if sla_compliance_raw is not None:
        sla_score = derive_sla_score(float(sla_compliance_raw), sla_breaches, sla_total)
    else:
        sla_score = derive_sla_score(compliance, sla_breaches, sla_total)

    # Resource evidence is "available" only when at least one server reported a
    # real (non-zero) utilization metric. An image-only DOCX or a missing upload
    # parses to all-zero metrics — that is "no data", not "zero pressure", so the
    # resource pillar is dropped from OSHS rather than scored a misleading 100.
    resource_available = len(known) > 0
    oshs = dict(calc_oshs(batch_score, resource_score, sla_score,
                          resource_available=resource_available))

    # ── Findings penalty (critical 3pts · warning 0.5pts, capped 15) ──
    findings_list = body.findings or []
    _crit_f = [f for f in findings_list if str(f.get("level", "")).lower() == "critical"]
    _warn_f = [f for f in findings_list if str(f.get("level", "")).lower() == "warning"]
    findings_penalty = round(min(15.0, len(_crit_f) * 3.0 + len(_warn_f) * 0.5), 1)
    oshs["score"]            = round(max(0.0, oshs["score"] - findings_penalty), 1)
    oshs["findings_penalty"] = findings_penalty

    # Patch F — blend the findings-engine penalty_score at 20% so the executive
    # badge tracks the PE Findings grade rather than drifting from it.
    try:
        from services import session_cache as _sc_exec
        _fp_score = _sc_exec.ac_get("findings_penalty_score")
        if _fp_score is not None:
            _fp = float(_fp_score)
            oshs["score"] = round(oshs["score"] * 0.80 + _fp * 0.20, 1)
            oshs["findings_blended"] = True
            oshs["findings_score"]   = round(_fp, 1)
    except Exception:
        pass

    # ── Release-blocking hard floor (single source of truth: pe_config grades) ──
    # The headline badge must never contradict the Go-Live Gate. Unresolved
    # critical findings or sub-95% window compliance block sign-off, so the OSHS
    # grade/label can never read APPROVED (A/B) regardless of the numeric score.
    # Capping the score (not just the label) keeps the ring colour consistent
    # with the verdict.
    from services.pe_config import score_to_grade as _s2g
    _floor_reason = None
    if len(_crit_f) > 0:
        _floor_reason = (f"{len(_crit_f)} unresolved critical finding(s)")
    elif _win_comp is not None and _win_comp < 95.0:
        _floor_reason = (f"window compliance {_win_comp:.0f}% below 95%")
    if _floor_reason is not None and oshs["score"] > 70.0:
        oshs["score"] = 70.0   # top of the CONDITIONAL HOLD band — never APPROVED
        oshs["floor_applied"] = _floor_reason

    _letter, _label = _s2g(oshs["score"])
    oshs["grade"] = _letter
    oshs["label"] = _label

    # ── Server Heatmap data ──────────────────────────────────────
    server_heatmap = []
    for s in known[:25]:
        server_heatmap.append({
            "host": s.get("host", s.get("label", "?")),
            "cpu":  round(_f(s.get("cpu_used")), 1),
            "mem":  round(_f(s.get("mem_used")), 1),
            "disk": round(_f(s.get("disk_used_max")), 1),
        })

    # ── Waterfall data ──────────────────────────────────────────────────────
    # Targets are each pillar's actual weight × 100, read straight from the OSHS
    # components so the bars can never disagree with the scoring math. When the
    # resource pillar is dropped (no measured evidence) its weight is 0 and the
    # batch/SLA targets widen to their re-normalised weights automatically.
    _wc = oshs["components"]
    waterfall = {
        "batch_contribution":    _wc["batch"]["contribution"],
        "resource_contribution": _wc["resource"]["contribution"],
        "sla_contribution":      _wc["sla"]["contribution"],
        "total":                 oshs["score"],
        "batch_target":    round(_wc["batch"]["weight"] * 100.0, 1),
        "resource_target": round(_wc["resource"]["weight"] * 100.0, 1),
        "sla_target":      round(_wc["sla"]["weight"] * 100.0, 1),
        "max_score":       100.0,  # anchor — JS must use this, never hardcode
        "findings_penalty": oshs.get("findings_penalty", 0.0),
        "resource_available": oshs.get("resource_available", True),
    }

    # ── KPI strip ────────────────────────────────────────────────
    window_comp = _f(bk.get("batch_window_compliance"))
    window_breach_days = int(bk.get("window_breach_days") or 0)

    kpis = {
        "oshs_score":          oshs["score"],
        "oshs_grade":          oshs["grade"],
        "oshs_label":          oshs["label"],
        "resource_available":  resource_available,
        "batch_rate":          round(compliance, 1),
        "window_compliance":   round(window_comp, 1) if window_comp else None,
        "window_breach_days":  window_breach_days,
        "sla_daily_hrs":       sla_ceiling,
        "fleet_grade":         rk.get("fleet_grade", "N/A"),
        "sla_breaches":        sla_breaches,
        "sla_at_risk":         int(sla_data.get("at_risk_runs") or bk.get("jobs_at_risk") or 0),
        "rfcs":                rfcs,
        "total_servers":       len(known),
        "total_jobs":          total_jobs,
        "avg_cpu":             round(avg_cpu, 1),
        "avg_mem":             round(avg_mem, 1),
        "avg_disk":            round(avg_disk, 1),
    }

    # ── Narrative (deterministic-only — always quotes exact metrics) ────────
    deterministic = generate_narrative(
        rfcs, oshs, bk, rk, servers, top_jobs,
        sla_data, sub_app_metrics,
    )
    narrative = deterministic

    # ── Deep dive enrichment: append time-series evidence to narrative ────
    dd = body.deep_dive or {}
    dd_vms = dd.get("per_vm") or []
    deep_dive_summary = None
    if dd.get("total_critical", 0) > 0 and dd_vms:
        dd_lines = []
        dd_lines.append(
            f"\n\nAzure Monitor Time-Series Evidence ({dd.get('hours_back', 24)}h window): "
            f"{dd['total_critical']} critical anomalies across {dd.get('affected_vms', 0)} VMs."
        )
        for vm in dd_vms[:3]:
            vm_name = vm.get("vm", "?")
            role = vm.get("role", "")
            mem = vm.get("mem_used_max")
            cpu = vm.get("cpu_max")
            trend = vm.get("trend", "flat")
            sc = vm.get("spike_count", 0)
            dom = f"MEM {mem:.0f}%" if mem and mem > (cpu or 0) else f"CPU {cpu:.0f}%"
            trend_txt = " ↑RISING" if trend == "rising" else (" ↓recovering" if trend == "recovering" else "")
            dd_lines.append(f"  • {vm_name} ({role}): {dom}, {sc} spikes{trend_txt}")
        narrative += " ".join(dd_lines)

        deep_dive_summary = {
            "total_critical": dd["total_critical"],
            "affected_vms": dd.get("affected_vms", 0),
            "hours_back": dd.get("hours_back", 24),
            "worst_vm": dd_vms[0].get("vm", "?") if dd_vms else None,
            "worst_metric": "MEM" if (dd_vms[0].get("mem_used_max") or 0) > (dd_vms[0].get("cpu_max") or 0) else "CPU" if dd_vms else None,
            "worst_value": max(dd_vms[0].get("mem_used_max") or 0, dd_vms[0].get("cpu_max") or 0) if dd_vms else 0,
        }

    # ── Breach calendar with SLA ceiling overlay ─────────────────
    # Built BEFORE the decision gate so both consume one authoritative
    # breach-day count — guarantees the blocker text and the calendar bars
    # can never report different numbers (e.g. an impossible "76/27").
    breach_calendar = _build_breach_calendar(window, sla_ceiling, top_jobs, _ceiling_map)  # Patch G

    # ── Decision Strip / Sign-off Gate (5-condition rule engine) ─
    decision = _compute_decision_gate(
        kpis=kpis,
        bk=bk,
        rk=rk,
        servers=servers,
        window=window,
        sla_ceiling=sla_ceiling,
        ceiling_map=_ceiling_map,          # Patch G
        findings=body.findings or [],
        customer=body.customer_name or "",
        breach_days=breach_calendar.get("breach_count"),
        total_days=breach_calendar.get("total_days"),
    )

    # ── Job concurrency timeline (worst day Gantt) ───────────────
    concurrency = _build_concurrency_timeline(
        body.daily_jobs or {}, window, sla_ceiling,
    )

    # ── SOW vs Actual panel ──────────────────────────────────────
    sow_panel = _build_sow_panel(body.sow_compare, bk, rk, servers)

    # ── Sub-App Summary table (replaces treemap) ─────────────────
    sub_app_summary = _build_sub_app_summary(top_jobs, sla_ceiling, _ceiling_map)  # Patch G

    # ── Session-cache enrichment: volume_vs_sow + resolved_workflow_df ──
    # These are written by sow.py and sla_matrix.py respectively.
    # The executive dashboard needs them for the SOW panel and KPI strip
    # without requiring the client to pass them explicitly.
    _volume_vs_sow: Dict[str, Any] = {}
    _workflow_kpis: Dict[str, Any] = {}
    try:
        from services import session_cache as _sc_exec
        _volume_vs_sow = _sc_exec.ac_get("volume_vs_sow") or {}
        _sla_kpis      = _sc_exec.ac_get("sla_matrix_kpis") or {}
        _workflow_kpis = {
            "compliance_pct":    _sla_kpis.get("compliance_pct"),
            "breaching_runs":    _sla_kpis.get("breaching_runs"),
            "window_breach_days": _sla_kpis.get("window_breach_days"),
            "window_total_days":  _sla_kpis.get("window_total_days"),
        }
        # Enrich sow_panel with volume_by_year data so the SOW section
        # can show contractual volume vs actuals without a separate API call
        if _volume_vs_sow and isinstance(sow_panel, dict):
            sow_panel["volume_by_year"]    = _volume_vs_sow.get("volume_by_year") or {}
            sow_panel["max_item_locations"] = _volume_vs_sow.get("max_item_locations")
        # BUG-M3 fix: always write canonical compliance when SLA Matrix has run —
        # never gate on sla_breaches == 0. Breach engagements need the canonical number most.
        if _workflow_kpis.get("compliance_pct") is not None:
            kpis["sla_compliance_canonical"] = _workflow_kpis["compliance_pct"]
            kpis["sla_breach_days_canonical"] = _workflow_kpis.get("window_breach_days")
    except Exception:
        pass

    return {
        "kpis":            kpis,
        "oshs":            oshs,
        "rfcs":            rfcs,
        "sub_app_metrics": sub_app_metrics,
        "sub_app_summary": sub_app_summary,
        "server_heatmap":  server_heatmap,
        "temporal":        temporal,
        "waterfall":       waterfall,
        "narrative":       narrative,
        "job_sla_bars":    job_sla_bars,
        "decision":        decision,
        "breach_calendar": breach_calendar,
        "concurrency":     concurrency,
        "sow_panel":       sow_panel,
        "volume_vs_sow":   _volume_vs_sow,
        "deep_dive_summary": deep_dive_summary,
    }


# ─────────────────────────────────────────────────────────────────
# Decision Gate — 5-condition sign-off rule engine
# ─────────────────────────────────────────────────────────────────
def _compute_decision_gate(
    *,
    kpis: Dict[str, Any],
    bk: Dict[str, Any],
    rk: Dict[str, Any],
    servers: List[Dict[str, Any]],
    window: List[Dict[str, Any]],
    sla_ceiling: float,
    ceiling_map: Dict[str, float] | None = None,
    findings: List[Dict[str, Any]],
    customer: str,
    breach_days: int | None = None,
    total_days: int | None = None,
) -> Dict[str, Any]:
    """Produces the unified Decision Strip data.

    Conditions (ALL must pass for READY):
      1. Job SLA rate == 100%
      2. Window compliance >= 95%
      3. No server in WARNING/CRITICAL state
      4. Evidence coverage >= 80% (via days covered or confidence)
      5. Zero unresolved critical findings
    """
    ceiling_map = ceiling_map or {}

    # ── 1. Job SLA rate
    job_sla = float(bk.get("compliance_pct") or 100.0)

    # ── 2. Window compliance — prefer the authoritative breach-day counts
    #    supplied by _build_breach_calendar (single source of truth so the
    #    blocker text and the Breach Calendar bars always agree). Fall back to
    #    a local re-derivation only when they were not passed in.
    def _win_hrs(w):
        e = _f(w.get("elapsed_hrs"))
        return e if e > 0 else _f(w.get("total_hrs"))
    def _win_ceiling(w: dict) -> float:
        _sa = str(w.get("sub_app") or w.get("Sub_Application") or "").upper()
        return ceiling_map.get(_sa, sla_ceiling)

    if total_days is None:
        total_days = len(window or [])
    if breach_days is None:
        breach_days = sum(
            1 for w in (window or [])
            if _win_hrs(w) > _win_ceiling(w)
        )
    # Invariant guard: a breach-day count can never exceed total days.
    if total_days and breach_days > total_days:
        breach_days = total_days
    compliant_days = total_days - breach_days
    if total_days > 0:
        window_compliance = round(compliant_days / total_days * 100.0, 1)
    else:
        window_compliance = float(kpis.get("window_compliance") or 0.0)

    # ── 3. Fleet health
    bad_servers = [
        (s.get("host") or s.get("label") or "?")
        for s in (servers or [])
        if str(s.get("status", "")).lower() in {"warning", "critical", "warn", "crit"}
    ]

    # ── 4. Evidence coverage
    cov_days = int(((bk.get("data_coverage") or {}).get("date_span_days")) or total_days or 0)
    cov_target = 30  # 30 days = 100% coverage signal
    cov_pct = min(100.0, (cov_days / cov_target) * 100.0) if cov_days else 0.0

    # ── 5. Critical findings
    crit_findings = [f for f in (findings or []) if str(f.get("level")).lower() == "critical"]

    # Build condition list with pass/fail + actual vs required
    conditions = [
        {
            "key":     "job_sla",
            "label":   "Job SLA rate",
            "pass":    job_sla >= 99.99,
            "actual":  f"{job_sla:.1f}%",
            "required":"100%",
            "blocker": f"Job SLA {job_sla:.1f}% (required 100%)" if job_sla < 99.99 else "",
        },
        {
            "key":     "window",
            "label":   "Window compliance",
            "pass":    window_compliance >= 95.0,
            "actual":  f"{window_compliance:.0f}%",
            "required":"≥95%",
            "blocker": (f"Window compliance {window_compliance:.0f}% "
                        f"({breach_days}/{total_days} days breached) — required ≥95%"
                        if window_compliance < 95.0 else ""),
        },
        {
            "key":     "fleet",
            "label":   "Fleet health",
            "pass":    len(bad_servers) == 0,
            "actual":  f"{len(bad_servers)} server(s) flagged" if bad_servers else "All OK",
            "required":"0 flagged",
            "blocker": (f"{len(bad_servers)} server(s) in warning/critical: "
                        f"{', '.join(bad_servers[:3])}" if bad_servers else ""),
        },
        {
            "key":     "evidence",
            "label":   "Evidence coverage",
            "pass":    cov_pct >= 80.0,
            "actual":  f"{cov_days} days ({cov_pct:.0f}%)",
            "required":"≥80% (24+ days)",
            "blocker": (f"Only {cov_days} days of data ({cov_pct:.0f}%) — required ≥24 days"
                        if cov_pct < 80.0 else ""),
        },
        {
            "key":     "findings",
            "label":   "Critical findings",
            "pass":    len(crit_findings) == 0,
            "actual":  f"{len(crit_findings)} unresolved" if crit_findings else "0",
            "required":"0",
            "blocker": (f"{len(crit_findings)} unresolved critical finding(s)"
                        if crit_findings else ""),
        },
    ]

    blockers = [c["blocker"] for c in conditions if not c["pass"] and c["blocker"]]
    all_pass = all(c["pass"] for c in conditions)

    # ── State machine: INCOMPLETE → IN_REVIEW → CONDITIONAL_HOLD → BLOCKED → APPROVED
    # Transition logic:
    #   - No data loaded at all → INCOMPLETE
    #   - Any condition fails   → BLOCKED  (with blocker reason)
    #   - All conditions pass but crit findings present → CONDITIONAL_HOLD
    #   - All conditions pass, no crit findings → APPROVED (ready for sign-off)
    has_any_data = bool(kpis.get("total_jobs") or servers or window)
    crit_count = len(crit_findings)

    if not has_any_data:
        status = "INCOMPLETE"
        reason = "Upload batch and resource data to begin the PE review."
    elif len(blockers) > 0:
        status = "BLOCKED"
        reason = blockers[0] if blockers else "Sign-off blocked — see conditions below"
    elif crit_count > 0:
        status = "CONDITIONAL_HOLD"
        reason = (f"{crit_count} critical finding(s) acknowledged — "
                  "conditional approval pending remediation plan")
    else:
        status = "APPROVED"
        reason = (
            f"All {int(bk.get('total_jobs') or 0)} jobs within SLA · "
            f"0 window breaches · fleet grade {kpis.get('fleet_grade', 'A')} · "
            f"{cov_days} days evidence · PE approved"
        )

    # Owner-tagged Next Actions (top 3 prioritised)
    next_actions = _derive_next_actions(conditions, findings, bad_servers, breach_days)

    return {
        "status":             status,
        "reason":             reason,
        "grade":              kpis.get("oshs_grade", "—"),
        "blockers_count":     len(blockers),
        "blockers":           blockers,
        "conditions":         conditions,
        "days_covered":       cov_days,
        "window_compliance":  window_compliance,
        "breach_days":        breach_days,
        "total_days":         total_days,
        "next_actions":       next_actions,
        "customer":           customer,
    }


def _derive_next_actions(
    conditions: List[Dict[str, Any]],
    findings: List[Dict[str, Any]],
    bad_servers: List[str],
    breach_days: int,
) -> List[Dict[str, str]]:
    """Build 3 owner-tagged next actions based on which conditions failed."""
    actions: List[Dict[str, str]] = []

    # PE Lead — first critical finding or window breach
    crit = next((f for f in (findings or []) if str(f.get("level")).lower() == "critical"), None)
    if crit:
        actions.append({
            "owner":  "PE Lead",
            "action": (crit.get("action") or crit.get("recommendation")
                       or f"Resolve: {crit.get('title') or crit.get('text', 'critical finding')}")[:60],
        })
    elif breach_days > 0:
        actions.append({
            "owner":  "PE Lead",
            "action": f"Investigate {breach_days} window breach day(s) before sign-off",
        })
    else:
        actions.append({
            "owner":  "PE Lead",
            "action": "Confirm sign-off with stakeholders",
        })

    # Infra Owner — server health
    if bad_servers:
        actions.append({
            "owner":  "Infra Owner",
            "action": f"Remediate {bad_servers[0]} (status: warning)"[:60],
        })
    else:
        actions.append({
            "owner":  "Infra Owner",
            "action": "Maintain capacity headroom monitoring",
        })

    # Customer — evidence or schedule
    cov_cond = next((c for c in conditions if c["key"] == "evidence"), None)
    if cov_cond and not cov_cond["pass"]:
        actions.append({
            "owner":  "Customer",
            "action": "Provide additional history (target 30 days)"[:60],
        })
    else:
        actions.append({
            "owner":  "Customer",
            "action": "Acknowledge PE review and schedule follow-up",
        })

    return actions[:3]


# ─────────────────────────────────────────────────────────────────
# Breach Calendar with SLA ceiling overlay
# ─────────────────────────────────────────────────────────────────
def _build_breach_calendar(
    window: List[Dict[str, Any]],
    sla_ceiling: float,
    top_jobs: List[Dict[str, Any]],
    ceiling_map: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    """Per-day window data + SLA ceiling line + summary stats.

    Returns:
        days:       [{date, day_of_week, hours, ceiling, over_by, status,
                      top_jobs: [job1, job2], sub_app}]
        ceiling:    global sla_ceiling reference
        summary:    "X of Y days breached contracted SLA ceiling | Worst: ... | Avg: ..."
    """
    ceiling_map = ceiling_map or {}
    if not window:
        return {"days": [], "ceiling": sla_ceiling, "summary": "No window data available."}

    import datetime as _dt
    days = []
    breach_count = 0
    worst_day = None
    worst_hrs = 0.0
    total_hrs = 0.0

    # Map top contributing jobs (top 2 globally)
    top_2_jobs = [j.get("Job_Name", "?") for j in (top_jobs or [])[:2]]

    for w in window:
        date_str = str(w.get("run_date") or w.get("date") or "")
        try:
            dt = _dt.date.fromisoformat(date_str)
            dow = dt.strftime("%a")
        except Exception:
            dow = ""
        # Prefer elapsed_hrs (wall-clock); only fall back to total_hrs
        _e = _f(w.get("elapsed_hrs"))
        _t = _f(w.get("total_hrs"))
        hrs = _e if _e > 0 else (_t if _t > 0 else 0.0)

        # Per-row contracted ceiling (Patch C key fix)
        _sa = str(w.get("sub_app") or w.get("Sub_Application") or "").upper()
        this_ceil = ceiling_map.get(_sa, sla_ceiling)
        over_by = round(hrs - this_ceil, 2)

        if hrs > this_ceil:
            status = "breach"
            breach_count += 1
        elif hrs > this_ceil * 0.9:
            status = "near"
        else:
            status = "ok"
        if hrs > worst_hrs:
            worst_hrs = hrs
            worst_day = date_str
        total_hrs += hrs
        days.append({
            "date":         date_str,
            "day_of_week":  dow,
            "hours":        round(hrs, 2),
            "ceiling":      round(this_ceil, 2),   # per-row contracted ceiling
            "over_by":      over_by,
            "status":       status,
            "top_jobs":     top_2_jobs,
            "sub_app":      _sa,
        })

    avg = round(total_hrs / len(days), 2) if days else 0.0
    summary = (
        f"{breach_count} of {len(days)} days breached contracted SLA ceiling | "
        f"Worst: {worst_day} at {worst_hrs:.1f}h | Avg: {avg}h"
    )
    return {
        "days":         days,
        "ceiling":      sla_ceiling,   # global reference value
        "summary":      summary,
        "breach_count": breach_count,
        "total_days":   len(days),
    }


# ─────────────────────────────────────────────────────────────────
# Job Concurrency Timeline (Gantt for worst day)
# ─────────────────────────────────────────────────────────────────
def _build_concurrency_timeline(
    daily_jobs: Dict[str, Any],
    window: List[Dict[str, Any]],
    sla_ceiling: float,
) -> Dict[str, Any]:
    """Build a Gantt-style timeline for the worst day.

    daily_jobs format: { "YYYY-MM-DD": [{"job": str, "start_hr": float, "end_hr": float}] }
    """
    if not daily_jobs:
        return {"available": False, "reason": "Per-day job timing data not loaded."}

    # Identify worst day from window data (prefer elapsed_hrs = actual wall-clock breach)
    worst_date = None
    worst_hrs = 0.0
    for w in (window or []):
        hrs = _f(w.get("elapsed_hrs") or w.get("total_hrs"))
        if hrs > worst_hrs:
            worst_hrs = hrs
            worst_date = str(w.get("run_date") or w.get("date") or "")

    available_dates = sorted(daily_jobs.keys())
    if worst_date not in daily_jobs:
        return {
            "available": False,
            "reason": (
                "Worst-day date is outside the loaded job-level timing range. "
                "Reload the batch file to clear stale summary state."
            ),
        }

    raw_jobs = daily_jobs[worst_date] or []

    # Compute window boundaries from daily_jobs (may be capped at 60)
    all_starts = [_f(j.get("start_hr")) for j in raw_jobs if _f(j.get("start_hr")) > 0]
    all_ends   = [_f(j.get("end_hr"))   for j in raw_jobs if _f(j.get("end_hr")) > 0]
    if all_starts and all_ends:
        window_start = min(all_starts)
        window_end   = max(all_ends)
        window_len   = round(window_end - window_start, 2)
    else:
        window_start = 0
        window_end = 0
        window_len = 0

    # Prefer authoritative elapsed_hrs from window data (covers ALL jobs,
    # not just the capped 60 in daily_jobs).  Adjust window_start accordingly.
    win_rec = next(
        (w for w in (window or [])
         if str(w.get("run_date") or w.get("date") or "") == worst_date),
        None,
    )
    if win_rec:
        true_elapsed = _f(win_rec.get("elapsed_hrs"))
        if true_elapsed > 0 and window_end > 0:
            window_len   = round(true_elapsed, 2)
            window_start = round(window_end - true_elapsed, 2)

    sla_deadline = round(window_start + sla_ceiling, 2)  # hour-of-day by which all jobs must finish

    # Sort by end_hr desc (jobs that push the window latest = highest impact)
    jobs_sorted = sorted(
        raw_jobs,
        key=lambda j: _f(j.get("end_hr", 0)),
        reverse=True,
    )[:15]
    top_3 = [j.get("job", "?") for j in jobs_sorted[:3]]

    # Concurrency density — how many jobs run at each integer hour (use ALL jobs)
    hour_buckets = {h: 0 for h in range(int(window_start), int(window_end) + 2)}
    for j in raw_jobs:
        s = int(_f(j.get("start_hr")))
        e = int(_f(j.get("end_hr"))) + 1
        for h in range(s, e):
            if h in hour_buckets:
                hour_buckets[h] += 1
    peak_concurrency = max(hour_buckets.values()) if hour_buckets else 0
    peak_hour = max(hour_buckets, key=hour_buckets.get) if hour_buckets else None

    # Format jobs for chart — exceeds_sla = job finishes AFTER the SLA deadline
    bars = [
        {
            "job":       j.get("job", "?"),
            "start_hr":  round(_f(j.get("start_hr")), 2),
            "end_hr":    round(_f(j.get("end_hr")), 2),
            "duration":  round(_f(j.get("end_hr")) - _f(j.get("start_hr")), 2),
            "exceeds_sla": _f(j.get("end_hr")) > sla_deadline,
        }
        for j in jobs_sorted
    ]

    summary = (
        f"On {worst_date}, {peak_concurrency} jobs ran concurrently around hour {peak_hour}, "
        f"window elapsed {window_len}h (SLA: {sla_ceiling}h). "
        f"Top 3 contributors: {', '.join(top_3) if top_3 else '—'}."
    )

    return {
        "available":      True,
        "selected_date":  worst_date,
        "available_dates": available_dates,
        "bars":           bars,
        "ceiling":        sla_ceiling,
        "window_start":   round(window_start, 2),
        "window_end":     round(window_end, 2),
        "window_length":  window_len,
        "peak_concurrency": peak_concurrency,
        "peak_hour":      peak_hour,
        "top_contributors": top_3,
        "summary":        summary,
    }


# ─────────────────────────────────────────────────────────────────
# SOW vs Actual panel (3-column composite)
# ─────────────────────────────────────────────────────────────────
def _build_sow_panel(
    sow_compare: Optional[Dict[str, Any]],
    bk: Dict[str, Any],
    rk: Dict[str, Any],
    servers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Returns {available: bool, volume: {...}, sla: {...}, capacity: {...}}.

    When SOW data is not loaded, available=False and the frontend collapses
    all 3 columns into a single 'Upload SOW to enable comparison' card.
    """
    if not sow_compare or not (sow_compare.get("metrics") or []):
        return {"available": False, "reason": "SOW file not uploaded."}

    metrics = sow_compare.get("metrics") or []

    # ── Volume comparison (DFU/SKU/Orders/Jobs)
    volume_keys = {"daily_dfu", "daily_sku", "daily_orders", "batch_jobs", "data_volume_gb"}
    volume_rows = [m for m in metrics if m.get("key") in volume_keys]

    # ── Capacity (CPU/MEM baselines)
    cap_keys = {"cpu_baseline_pct", "mem_baseline_pct", "disk_baseline_pct"}
    cap_rows = []
    cap_metrics = [m for m in metrics if m.get("key") in cap_keys]
    for m in cap_metrics:
        sow_val = _f(m.get("sow"))
        actual_val = _f(m.get("actual"))
        threshold = sow_val * 0.8
        if actual_val <= threshold:
            cap_status = "Within baseline"
        elif actual_val <= sow_val:
            cap_status = "Approaching limit"
        else:
            cap_status = "Exceeding contracted spec"
        cap_rows.append({
            "label":     m.get("label"),
            "sow":       sow_val,
            "actual":    actual_val,
            "threshold": round(threshold, 1),
            "status":    cap_status,
            "pct":       _f(m.get("pct")),
        })

    return {
        "available":    True,
        "volume":       volume_rows,
        "capacity":     cap_rows,
        "overall_status": sow_compare.get("overall_status"),
        "summary":      sow_compare.get("summary"),
    }


# ─────────────────────────────────────────────────────────────────
# Sub-App Summary table (replaces treemap)
# ─────────────────────────────────────────────────────────────────
def _build_sub_app_summary(
    top_jobs: List[Dict[str, Any]],
    sla_ceiling: float,
    ceiling_map: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    """Aggregate top jobs by Sub_Application for a compact 5-row summary."""
    ceiling_map = ceiling_map or {}
    by_app: Dict[str, Dict[str, Any]] = {}
    for j in top_jobs or []:
        app = (j.get("Sub_Application") or j.get("sub_app")
               or j.get("module") or "Unassigned")
        peak = _f(j.get("peak_hrs"))
        if app not in by_app:
            by_app[app] = {"sub_app": app, "job_count": 0, "peak_hrs": 0.0}
        by_app[app]["job_count"] += 1
        if peak > by_app[app]["peak_hrs"]:
            by_app[app]["peak_hrs"] = peak

    rows = []
    for app, info in by_app.items():
        peak = info["peak_hrs"]
        # Per-sub_app contracted ceiling (Patch E key fix)
        this_ceiling = ceiling_map.get(app.upper(), sla_ceiling)
        buffer_pct = round(((this_ceiling - peak) / this_ceiling) * 100.0, 1) if this_ceiling else 0.0
        if peak > this_ceiling:
            status = "BREACH"
        elif buffer_pct < 15:
            status = "AT_RISK"
        else:
            status = "OK"
        rows.append({
            "sub_app":      app,
            "job_count":    info["job_count"],
            "peak_hrs":     round(peak, 2),
            "ceiling":      round(this_ceiling, 2),   # per-sub_app
            "buffer_pct":   buffer_pct,
            "status":       status,
        })
    rows.sort(key=lambda r: r["peak_hrs"], reverse=True)
    rows = rows[:5]

    if not rows:
        return {"rows": [], "tip": ""}

    worst = rows[0]
    combined_peak = sum(r["peak_hrs"] for r in rows)
    tip = (
        f"Combined peak: {combined_peak:.1f}h — "
        f"review {worst['sub_app']} for parallelisation"
        if worst["status"] != "OK" else
        f"Combined peak: {combined_peak:.1f}h — all sub-apps within SLA"
    )
    return {"rows": rows, "tip": tip}


def _avg(items: list[dict], key: str) -> float:
    vals = [_f(x.get(key)) for x in items if _f(x.get(key)) > 0]
    return sum(vals) / len(vals) if vals else 0.0
