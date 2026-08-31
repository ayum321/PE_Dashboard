"""
Executive narrative generator — builds the 5-step Coverage→Risk→Cause→Impact→
Action prose panel from already-computed correlation scores.

Split out of services/correlation_engine.py on purpose: that module is pure
scoring math (no I/O, no business-decision prose); this module is the opposite
— it makes narrative/wording decisions (which branch of prose to show, what
counts as "at risk" for a sentence) and has its own thresholds, so a wording
change here never requires touching a formula file. Its 4 decision thresholds
(NARRATIVE_SRI_AT_RISK, NARRATIVE_CRS_CAUSE_THRESHOLD, plus the shared
CPU_CRIT / RFCS_BAND_AMBER) are named constants in services/pe_config.py.
"""
from __future__ import annotations

from services.pe_utils import coerce_float as _f
from services import pe_config as _pc
from services.correlation_engine import _avg_metric


def generate_narrative(
    rfcs: float,
    oshs: dict,
    batch_kpis: dict,
    resource_kpis: dict,
    servers: list[dict],
    top_jobs: list[dict],
    sla_data: dict | None,
    sub_app_metrics: list[dict],
) -> list[dict[str, str]]:
    """Auto-generate executive narrative findings.

    Always returns exactly 5 dicts — one per step in the Coverage→Risk→Cause→
    Impact→Action framework — so the UI 5-step renderer always has clean data.
    Each dict carries: {key, icon, level, text}.
    """

    score  = oshs.get("score", 0)
    grade  = oshs.get("grade", "?")
    label  = oshs.get("label", "")
    comps  = oshs.get("components", {})
    res_avail = oshs.get("resource_available", True)

    # ── 1. COVERAGE — what we measured ───────────────────────────
    total_runs  = batch_kpis.get("total_runs", 0) or 0
    total_jobs  = batch_kpis.get("total_jobs", 0) or 0
    srv_count   = len(servers)
    sub_count   = len(sub_app_metrics)
    sla_ceiling = batch_kpis.get("daily_limit_hrs") or batch_kpis.get("sla_daily_hrs") or 0
    b_score = round(_f(comps.get("batch",    {}).get("contribution", 0)), 1)
    r_score = round(_f(comps.get("resource", {}).get("contribution", 0)), 1)
    s_score = round(_f(comps.get("sla",      {}).get("contribution", 0)), 1)
    _split = (
        f"Score split — batch {b_score}pts · resource {r_score}pts · SLA {s_score}pts."
        if res_avail else
        f"Score split — batch {b_score}pts · SLA {s_score}pts "
        f"(resource pillar excluded — no measured utilization; weight re-normalised over batch + SLA)."
    )
    # Only claim servers were "analysed" when resource metrics are actually usable.
    # An image-only / all-zero resource doc yields no measured utilization, so the
    # coverage line must not imply a server was assessed (the score split below
    # already explains the resource pillar was excluded).
    _srv_clause = f"{srv_count} server(s), " if (res_avail and srv_count) else ""
    coverage_text = (
        f"Overall posture: OSHS {score:.1f}/100 → Grade {grade} ({label}). "
        f"Analysed {total_runs} batch runs across {total_jobs} jobs, "
        f"{_srv_clause}{sub_count} sub-application(s). "
        f"SLA ceiling {sla_ceiling}h. "
        f"{_split}"
    )

    # ── 2. RISK — what's at stake ─────────────────────────────────
    breach_days = batch_kpis.get("window_breach_days", 0) or 0
    total_days  = batch_kpis.get("window_total_days", 1) or 1
    # Day-level window compliance — derived from the breach/total days shown beside it
    # so the headline % and the "(breach/total breach days)" fraction always reconcile
    # (e.g. 2/28 clean days == 7%). Pair-level is intentionally NOT used in this prose.
    win_comp    = round((total_days - breach_days) / total_days * 100, 1) if total_days else (
        batch_kpis.get("batch_window_compliance", 100) or 100
    )
    at_risk_subs = sorted(
        [s for s in sub_app_metrics if s.get("sri", 0) > _pc.NARRATIVE_SRI_AT_RISK],
        key=lambda x: x.get("sri", 0), reverse=True,
    )
    if at_risk_subs:
        worst = at_risk_subs[0]
        risk_text = (
            f"Batch window compliance {win_comp:.0f}% ({breach_days}/{total_days} breach days). "
            f"{len(at_risk_subs)} sub-app(s) at SRI > {_pc.NARRATIVE_SRI_AT_RISK} — worst: "
            f"'{worst['sub_app']}' SRI {worst['sri']:.2f} "
            f"({'WILL BREACH' if worst['sri'] > 1.0 else 'AT RISK'}). "
            f"RFCS = {rfcs:.1f}."
        )
    else:
        risk_text = (
            f"Batch window compliance {win_comp:.0f}% ({breach_days}/{total_days} breach days). "
            f"No sub-applications currently at SRI risk threshold. "
            f"RFCS = {rfcs:.1f}."
        )

    # ── 3. CAUSE — why it's happening ────────────────────────────
    critical_servers = [s for s in servers if _f(s.get("cpu_used")) >= _pc.CPU_CRIT]
    zero_dur = sum(1 for j in top_jobs if _f(j.get("avg_hrs")) == 0)
    if critical_servers and rfcs >= _pc.RFCS_BAND_AMBER:
        names = ", ".join(s.get("host", "?") for s in critical_servers[:3])
        cause_text = (
            f"Resource saturation is a primary driver: {len(critical_servers)} server(s) "
            f"({names}) at ≥{_pc.CPU_CRIT:.0f}% CPU. RFCS {rfcs:.1f} confirms resource→failure coupling. "
        )
        if zero_dur:
            cause_text += f"Additionally {zero_dur} jobs show zero-duration (pre-execution failure — Ctrl-M config issue)."
    elif zero_dur:
        cause_text = (
            f"{zero_dur} job(s) show zero-second duration — pre-execution termination "
            f"(Ctrl-M timeout/dependency config, NOT resource pressure). "
        )
        cause_text += (
            f"Average fleet CPU {_avg_metric(servers, 'cpu_used'):.0f}%."
            if res_avail else
            "Resource utilization evidence not available — saturation not assessed."
        )
    else:
        high_crs = sorted(
            [s for s in sub_app_metrics if s.get("crs", 0) > _pc.NARRATIVE_CRS_CAUSE_THRESHOLD],
            key=lambda x: x.get("crs", 0), reverse=True,
        )
        if high_crs:
            top = high_crs[0]
            _cpu_clause = (
                f"Fleet CPU avg {_avg_metric(servers, 'cpu_used'):.0f}% — "
                f"no critical saturation detected."
                if res_avail else
                "Resource utilization evidence not available — saturation not assessed."
            )
            cause_text = (
                f"Cascade risk in '{top['sub_app']}' (CRS {top['crs']:.2f}, "
                f"{top['job_count']} jobs). {_cpu_clause}"
            )
        else:
            cause_text = (
                (
                    f"No critical resource saturation (fleet CPU avg "
                    f"{_avg_metric(servers, 'cpu_used'):.0f}%). "
                    f"Compliance issues driven by schedule/volume, not hardware pressure."
                )
                if res_avail else
                (
                    "Resource utilization evidence not available — hardware pressure "
                    "could not be evaluated. Compliance issues attributable to "
                    "schedule/volume."
                )
            )

    # ── 4. IMPACT — business effect ──────────────────────────────
    fail_rate = _f(batch_kpis.get("fail_rate_pct", 0))
    failed_runs = int(batch_kpis.get("failed_runs", 0) or 0)
    ok_runs     = int(batch_kpis.get("ok_runs", 0) or 0)
    worst_job_name = batch_kpis.get("worst_job_name") or (top_jobs[0].get("Job_Name") if top_jobs else "?")
    worst_job_peak = _f(batch_kpis.get("worst_job_peak") or (top_jobs[0].get("peak_hrs") if top_jobs else 0))
    impact_text = (
        f"{failed_runs} failed runs ({fail_rate:.1f}% fail rate) vs {ok_runs} OK. "
    )
    if breach_days:
        impact_text += (
            f"SLA breach on {breach_days}/{total_days} run day(s) creates "
            f"downstream delivery risk for business processes depending on batch completion. "
        )
    else:
        impact_text += "All measured run days within SLA window — no immediate delivery impact. "
    if worst_job_peak > 0:
        impact_text += f"Longest job: '{worst_job_name}' peaked at {worst_job_peak:.2f}h."

    # ── 5. ACTION — recommended decision ─────────────────────────
    actions = []
    if grade in ("D", "F"):
        actions.append("escalate to emergency remediation")
    elif grade == "C":
        actions.append("schedule remediation sprint within 2 weeks")
    if breach_days:
        actions.append(f"investigate {breach_days} SLA breach day(s) — review elapsed window vs ceiling")
    if critical_servers:
        actions.append(f"right-size / scale {len(critical_servers)} CPU-saturated server(s)")
    if zero_dur:
        actions.append("audit Ctrl-M job pre-conditions causing zero-duration terminations")
    if at_risk_subs:
        actions.append(f"prioritise load testing for '{at_risk_subs[0]['sub_app']}'")
    if not actions:
        actions.append("maintain current monitoring cadence — posture is healthy")
    action_text = "; ".join(actions[:3]).capitalize() + "."

    level_map = {
        "A": "info", "B": "info", "C": "warning", "D": "critical", "F": "critical",
    }
    overall_level = level_map.get(grade, "warning")

    return [
        {"key": "coverage", "icon": "🛡️", "level": "info",         "text": coverage_text},
        {"key": "risk",     "icon": "⚠️", "level": overall_level,  "text": risk_text},
        {"key": "cause",    "icon": "🔍", "level": overall_level,  "text": cause_text},
        {"key": "impact",   "icon": "📉", "level": overall_level,  "text": impact_text},
        {"key": "action",   "icon": "🎯", "level": "info",         "text": action_text},
    ]
