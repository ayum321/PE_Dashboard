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

    # ── Formula 2: SRI per job ───────────────────────────────────
    job_sla_bars = []
    for j in top_jobs[:20]:
        peak = _f(j.get("peak_hrs"))
        buf  = _f(j.get("buffer_pct"), 50.0)
        sri  = calc_sri(peak, sla_ceiling, avg_cpu)
        status = "BREACH" if buf < 0 else ("AT_RISK" if buf < 15 else "OK")
        job_sla_bars.append({
            "job_name":   j.get("Job_Name", "?"),
            "peak_hrs":   round(peak, 2),
            "sla_ceiling": sla_ceiling,
            "buffer_pct": round(buf, 1),
            "sri":        round(sri, 3),
            "status":     status,
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
        # Fallback heuristic when real hourly data not available
        total_batch_hrs = sum(_f(w.get("total_hrs")) for w in window)
        days = len(window) or 1
        for h in range(24):
            is_batch_window = h >= 20 or h <= 4
            hourly_jobs[h] = round(total_jobs / days * (0.12 if is_batch_window else 0.02))
            hourly_fails[h] = round(hourly_jobs[h] * fail_rate / 100.0)

    temporal = calc_jrtos(hourly_jobs, hourly_fails, peak_cpu)

    # ── Formula 4+5: Sub-app metrics (SRI, CRS, RFCS per group) ─
    sub_app_metrics = build_sub_app_metrics(top_jobs, servers, sla_ceiling)

    # ── OSHS computation ─────────────────────────────────────────
    batch_score    = derive_batch_score(compliance, fail_rate)
    resource_score = derive_resource_score(avg_cpu, avg_mem, avg_disk)

    sla_compliance = _f(sla_data.get("compliance_pct"), compliance)
    sla_breaches   = int(sla_data.get("breaching_runs") or bk.get("jobs_breach") or 0)
    sla_total      = int(sla_data.get("total_runs") or total_jobs or 1)
    sla_score      = derive_sla_score(sla_compliance, sla_breaches, sla_total)

    oshs = calc_oshs(batch_score, resource_score, sla_score)

    # ── Server Heatmap data ──────────────────────────────────────
    server_heatmap = []
    for s in known[:25]:
        server_heatmap.append({
            "host": s.get("host", s.get("label", "?")),
            "cpu":  round(_f(s.get("cpu_used")), 1),
            "mem":  round(_f(s.get("mem_used")), 1),
            "disk": round(_f(s.get("disk_used_max")), 1),
        })

    # ── Waterfall data ───────────────────────────────────────────
    waterfall = {
        "batch_contribution":    oshs["components"]["batch"]["contribution"],
        "resource_contribution": oshs["components"]["resource"]["contribution"],
        "sla_contribution":      oshs["components"]["sla"]["contribution"],
        "total":                 oshs["score"],
        "batch_target":    40.0 * 0.75,   # 75% of max weight = target
        "resource_target": 35.0 * 0.75,
        "sla_target":      25.0 * 0.75,
    }

    # ── KPI strip ────────────────────────────────────────────────
    window_comp = _f(bk.get("batch_window_compliance"))
    window_breach_days = int(bk.get("window_breach_days") or 0)

    kpis = {
        "oshs_score":          oshs["score"],
        "oshs_grade":          oshs["grade"],
        "oshs_label":          oshs["label"],
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

    # ── Decision Strip / Sign-off Gate (5-condition rule engine) ─
    decision = _compute_decision_gate(
        kpis=kpis,
        bk=bk,
        rk=rk,
        servers=servers,
        window=window,
        sla_ceiling=sla_ceiling,
        findings=body.findings or [],
        customer=body.customer_name or "",
    )

    # ── Breach calendar with SLA ceiling overlay ─────────────────
    breach_calendar = _build_breach_calendar(window, sla_ceiling, top_jobs)

    # ── Job concurrency timeline (worst day Gantt) ───────────────
    concurrency = _build_concurrency_timeline(
        body.daily_jobs or {}, window, sla_ceiling,
    )

    # ── SOW vs Actual panel ──────────────────────────────────────
    sow_panel = _build_sow_panel(body.sow_compare, bk, rk, servers)

    # ── Sub-App Summary table (replaces treemap) ─────────────────
    sub_app_summary = _build_sub_app_summary(top_jobs, sla_ceiling)

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
        # Enrich KPIs with canonical SLA matrix data when not in the body
        if _workflow_kpis.get("compliance_pct") is not None and kpis.get("sla_breaches") == 0:
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
    findings: List[Dict[str, Any]],
    customer: str,
) -> Dict[str, Any]:
    """Produces the unified Decision Strip data.

    Conditions (ALL must pass for READY):
      1. Job SLA rate == 100%
      2. Window compliance >= 95%
      3. No server in WARNING/CRITICAL state
      4. Evidence coverage >= 80% (via days covered or confidence)
      5. Zero unresolved critical findings
    """
    # ── 1. Job SLA rate
    job_sla = float(bk.get("compliance_pct") or 100.0)

    # ── 2. Window compliance (re-derive from window list to be authoritative)
    #    Prefer elapsed_hrs (wall-clock) over total_hrs (summed parallel jobs).
    total_days = len(window or [])
    def _win_hrs(w):
        e = _f(w.get("elapsed_hrs"))
        return e if e > 0 else _f(w.get("total_hrs"))
    breach_days = sum(1 for w in (window or [])
                      if _win_hrs(w) > sla_ceiling
                      or w.get("breach") is True)
    if total_days > 0:
        window_compliance = round(((total_days - breach_days) / total_days) * 100.0, 1)
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
) -> Dict[str, Any]:
    """Per-day window data + SLA ceiling line + summary stats.

    Returns:
        days:       [{date, day_of_week, hours, ceiling, over_by, status,
                      top_jobs: [job1, job2]}]
        ceiling:    sla_ceiling
        summary:    "X of Y days breached the Zh SLA ceiling | Worst: ... | Avg: ..."
    """
    if not window:
        return {"days": [], "ceiling": sla_ceiling, "summary": "No window data available."}

    import datetime as _dt
    days = []
    breach_count = 0
    worst_day = None
    worst_hrs = 0.0
    total_hrs = 0.0

    # Map top contributing jobs (top 2 globally — per-day attribution
    # would require raw row-level data which isn't always present)
    top_2_jobs = [j.get("Job_Name", "?") for j in (top_jobs or [])[:2]]

    for w in window:
        date_str = str(w.get("run_date") or w.get("date") or "")
        try:
            dt = _dt.date.fromisoformat(date_str)
            dow = dt.strftime("%a")
        except Exception:
            dow = ""
        hrs = _f(w.get("elapsed_hrs") or w.get("total_hrs"))
        over_by = round(hrs - sla_ceiling, 2)
        if hrs > sla_ceiling:
            status = "breach"
            breach_count += 1
        elif hrs > sla_ceiling * 0.9:
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
            "ceiling":      sla_ceiling,
            "over_by":      over_by,
            "status":       status,
            "top_jobs":     top_2_jobs,
        })

    avg = round(total_hrs / len(days), 2) if days else 0.0
    summary = (
        f"{breach_count} of {len(days)} days breached the {sla_ceiling}h SLA ceiling | "
        f"Worst: {worst_day} at {worst_hrs:.1f}h | Avg: {avg}h"
    )
    return {
        "days":     days,
        "ceiling":  sla_ceiling,
        "summary":  summary,
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
        worst_date = available_dates[-1] if available_dates else None

    if not worst_date or worst_date not in daily_jobs:
        return {"available": False, "reason": "No matching date with job-level timings."}

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
) -> Dict[str, Any]:
    """Aggregate top jobs by Sub_Application for a compact 5-row summary."""
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
        buffer_pct = round(((sla_ceiling - peak) / sla_ceiling) * 100.0, 1) if sla_ceiling else 0.0
        if peak > sla_ceiling:
            status = "BREACH"
        elif buffer_pct < 15:
            status = "AT_RISK"
        else:
            status = "OK"
        rows.append({
            "sub_app":      app,
            "job_count":    info["job_count"],
            "peak_hrs":     round(peak, 2),
            "ceiling":      sla_ceiling,
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
