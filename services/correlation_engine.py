"""
Correlation Engine — executive-level formulas connecting Batch, Resource, and SLA.

Five composite metrics:
  RFCS  — Resource-Failure Correlation Score  (0–100)
  SRI   — SLA Risk Index                      (0–∞, >1 = breach)
  JRTOS — Job-Resource Temporal Overlap Score  per hour (0-23)
  CRS   — Cascade Risk Score                  (0–1)
  OSHS  — Overall System Health Score         (0–100 → A/B/C/D/F)

Plus a narrative generator for the executive dashboard text panel.
"""
from __future__ import annotations

import math
from typing import Any

from services.pe_utils import coerce_float as _f, coerce_int as _i


# ── Grade table ──────────────────────────────────────────────────────────────
# Delegates to pe_config.score_to_grade() — single source of truth.
from services.pe_config import score_to_grade as _score_to_grade

def _grade(score: float) -> tuple[str, str]:
    return _score_to_grade(score)


# ─────────────────────────────────────────────────────────────────────────────
# Formula 1 — RFCS (Resource-Failure Correlation Score)
# ─────────────────────────────────────────────────────────────────────────────
def calc_rfcs(
    failure_rate: float,
    avg_cpu: float,
    avg_mem: float,
    critical_server_count: int,
) -> float:
    """
    RFCS = failure_rate × (avg_resource_pressure / 100) × (1 + 0.15 × critical_servers)

    Clamped to 0–100. Measures how much resource stress correlates with failures.
    """
    resource_pressure = (avg_cpu * 0.6 + avg_mem * 0.4)  # weighted avg
    base = failure_rate * (resource_pressure / 100.0)
    amplifier = 1.0 + 0.15 * min(critical_server_count, 10)
    return round(min(100.0, max(0.0, base * amplifier)), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Formula 2 — SRI (SLA Risk Index) per job
# ─────────────────────────────────────────────────────────────────────────────
def calc_sri(
    peak_hrs: float,
    sla_ceiling_hrs: float,
    avg_cpu: float,
) -> float:
    """
    SRI = (peak_hrs / sla_ceiling) × resource_amplifier

    resource_amplifier = 1 + max(0, (avg_cpu - 70) / 100)
    SRI > 1.0 → breach even with resource load factored in.
    """
    if sla_ceiling_hrs <= 0:
        return 0.0
    resource_amp = 1.0 + max(0.0, (avg_cpu - 70.0) / 100.0)
    return round(peak_hrs / sla_ceiling_hrs * resource_amp, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Formula 3 — JRTOS (Job-Resource Temporal Overlap) per hour of day
# ─────────────────────────────────────────────────────────────────────────────
def calc_jrtos(
    hourly_job_counts: dict[int, int],
    hourly_failure_counts: dict[int, int],
    peak_cpu: float,
) -> list[dict[str, Any]]:
    """
    For each hour h (0-23):
      JRTOS[h] = (job_count[h] / max_jobs) × (fail_rate[h]) × (peak_cpu / 100)

    Returns list of {hour, jobs, failures, fail_rate, jrtos} sorted by hour.
    """
    max_jobs = max(hourly_job_counts.values()) if hourly_job_counts else 1
    max_jobs = max(max_jobs, 1)
    result = []
    for h in range(24):
        jobs = hourly_job_counts.get(h, 0)
        fails = hourly_failure_counts.get(h, 0)
        fail_rate = fails / max(jobs, 1) * 100.0
        jrtos = (jobs / max_jobs) * (fail_rate / 100.0) * (peak_cpu / 100.0)
        result.append({
            "hour": h,
            "jobs": jobs,
            "failures": fails,
            "fail_rate": round(fail_rate, 1),
            "jrtos": round(jrtos, 3),
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Formula 4 — CRS (Cascade Risk Score) per job
# ─────────────────────────────────────────────────────────────────────────────
def calc_crs(
    is_failed: bool,
    downstream_count: int,
    sla_buffer_pct: float,
) -> float:
    """
    CRS = failed_flag × (downstream_count / (downstream_count + 5)) × (1 - sla_buffer / 100)

    Returns 0–1. A high CRS means this single job failure could collapse its chain.
    """
    if not is_failed or downstream_count <= 0:
        return 0.0
    chain_factor = downstream_count / (downstream_count + 5.0)
    buffer_risk = 1.0 - min(max(sla_buffer_pct, 0.0), 100.0) / 100.0
    return round(min(1.0, chain_factor * buffer_risk), 3)


# ─────────────────────────────────────────────────────────────────────────────
# Formula 5 — OSHS (Overall System Health Score)
# ─────────────────────────────────────────────────────────────────────────────
def calc_oshs(
    batch_score: float,
    resource_score: float,
    sla_score: float,
    resource_available: bool = True,
) -> dict[str, Any]:
    """
    OSHS = batch_score × 0.40 + sla_score × 0.35 + resource_score × 0.25

    Each component is 0–100. When resource evidence is missing or unusable
    (e.g. an image-only utilization report with no parseable metrics, or no
    resource upload at all), the resource pillar is dropped and its 0.25 weight
    is re-normalised across batch and SLA. This keeps the score grounded in
    measured evidence instead of awarding a fabricated 100 for "no pressure"
    when the truth is "no data". Returns {score, grade, label,
    resource_available, components}.
    """
    W_BATCH, W_SLA, W_RES = 0.40, 0.35, 0.25
    if resource_available:
        w_batch, w_sla, w_res = W_BATCH, W_SLA, W_RES
        oshs = batch_score * w_batch + sla_score * w_sla + resource_score * w_res
        res_component = {
            "score": round(resource_score, 1),
            "weight": round(w_res, 4),
            "contribution": round(resource_score * w_res, 1),
            "available": True,
        }
    else:
        _avail = W_BATCH + W_SLA            # 0.75 — re-normalise over measured pillars
        w_batch, w_sla, w_res = W_BATCH / _avail, W_SLA / _avail, 0.0
        oshs = batch_score * w_batch + sla_score * w_sla
        res_component = {
            "score": None,                 # not measured — never fabricate a value
            "weight": 0.0,
            "contribution": 0.0,
            "available": False,
        }
    oshs = min(100.0, max(0.0, oshs))
    letter, label = _grade(oshs)
    return {
        "score": round(oshs, 1),
        "grade": letter,
        "label": label,
        "resource_available": resource_available,
        "components": {
            "batch":    {"score": round(batch_score, 1), "weight": round(w_batch, 4), "contribution": round(batch_score * w_batch, 1)},
            "sla":      {"score": round(sla_score, 1),   "weight": round(w_sla, 4),   "contribution": round(sla_score * w_sla, 1)},
            "resource": res_component,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Component score derivation helpers
# ─────────────────────────────────────────────────────────────────────────────
def derive_batch_score(compliance_pct: float, fail_rate: float) -> float:
    """0-100 batch health from compliance + inverse fail rate."""
    return min(100.0, max(0.0, compliance_pct * 0.7 + (100.0 - fail_rate) * 0.3))

def derive_resource_score(avg_cpu: float, avg_mem: float, avg_disk: float) -> float:
    """0-100 resource health — higher is better (lower utilization)."""
    pressure = avg_cpu * 0.40 + avg_mem * 0.35 + avg_disk * 0.25
    return min(100.0, max(0.0, 100.0 - pressure))

def derive_sla_score(compliance_pct: float, breach_count: int, total_jobs: int) -> float:
    """0-100 SLA health."""
    if total_jobs <= 0:
        return 50.0  # unknown
    breach_penalty = min(30.0, breach_count / max(total_jobs, 1) * 100.0)
    return min(100.0, max(0.0, compliance_pct - breach_penalty))


# ─────────────────────────────────────────────────────────────────────────────
# Sub-application aggregator
# ─────────────────────────────────────────────────────────────────────────────
def build_sub_app_metrics(
    top_jobs: list[dict],
    servers: list[dict],
    sla_ceiling_hrs: float,
    ceiling_map: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Aggregate per-sub-application metrics for the 3-Way Risk Matrix bubble chart.
    Returns list of {sub_app, avg_peak_hrs, max_peak_hrs, job_count, breach_count,
                     avg_buffer_pct, sri, rfcs_band, resource_pressure, crs}.

    BUG-W5 fix: ceiling_map provides per-sub-app contracted SLA so SRI/CRS are
    computed against the right ceiling, not the global default for all sub-apps.
    """
    from collections import defaultdict

    # group jobs by sub_application
    groups: dict[str, list] = defaultdict(list)
    for j in (top_jobs or []):
        sa = j.get("Sub_Application") or j.get("sub_application") or "Unknown"
        groups[sa].append(j)

    avg_cpu = _avg_metric(servers, "cpu_used")
    avg_mem = _avg_metric(servers, "mem_used")
    resource_pressure = avg_cpu * 0.6 + avg_mem * 0.4
    crit_count = sum(1 for s in servers if _f(s.get("cpu_used")) >= 90)

    results = []
    for sa, jobs in groups.items():
        # Resolve per-sub-app ceiling: ceiling_map first, then global fallback
        sa_key = sa.upper().strip()
        sa_ceiling = float((ceiling_map or {}).get(sa_key) or sla_ceiling_hrs)

        peaks   = [_f(j.get("peak_hrs")) for j in jobs]
        # BUG-M2 companion: None buffers are unknown, not healthy — exclude from avg
        buffers = [float(j["buffer_pct"]) for j in jobs if j.get("buffer_pct") is not None]
        breach_count = sum(1 for j in jobs
                           if j.get("buffer_pct") is not None and float(j["buffer_pct"]) < 0)
        fail_rate = breach_count / max(len(jobs), 1) * 100.0

        max_peak = max(peaks) if peaks else 0.0
        avg_peak = sum(peaks) / len(peaks) if peaks else 0.0
        avg_buf  = sum(buffers) / len(buffers) if buffers else None   # None = all unknown

        sri  = calc_sri(max_peak, sa_ceiling, avg_cpu)
        rfcs = calc_rfcs(fail_rate, avg_cpu, avg_mem, crit_count)
        crs  = calc_crs(breach_count > 0, len(jobs), avg_buf if avg_buf is not None else 50.0)

        results.append({
            "sub_app":          sa,
            "job_count":        len(jobs),
            "avg_peak_hrs":     round(avg_peak, 2),
            "max_peak_hrs":     round(max_peak, 2),
            "breach_count":     breach_count,
            "avg_buffer_pct":   round(avg_buf, 1) if avg_buf is not None else None,
            "sla_ceiling":      round(sa_ceiling, 3),
            "sri":              round(sri, 3),
            "rfcs":             round(rfcs, 1),
            "rfcs_band":        "red" if rfcs >= 60 else ("amber" if rfcs >= 30 else "green"),
            "resource_pressure": round(resource_pressure, 1),
            "crs":              round(crs, 3),
        })

    results.sort(key=lambda x: x["sri"], reverse=True)
    return results


def _avg_metric(servers: list[dict], key: str) -> float:
    vals = [_f(s.get(key)) for s in servers if _f(s.get(key)) > 0]
    return sum(vals) / len(vals) if vals else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Narrative generator
# ─────────────────────────────────────────────────────────────────────────────
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
    coverage_text = (
        f"Overall posture: OSHS {score:.1f}/100 → Grade {grade} ({label}). "
        f"Analysed {total_runs} batch runs across {total_jobs} jobs, "
        f"{srv_count} server(s), {sub_count} sub-application(s). "
        f"SLA ceiling {sla_ceiling}h. "
        f"{_split}"
    )

    # ── 2. RISK — what's at stake ─────────────────────────────────
    breach_days = batch_kpis.get("window_breach_days", 0) or 0
    total_days  = batch_kpis.get("window_total_days", 1) or 1
    win_comp    = batch_kpis.get("batch_window_compliance", 100) or 100
    at_risk_subs = sorted(
        [s for s in sub_app_metrics if s.get("sri", 0) > 0.85],
        key=lambda x: x.get("sri", 0), reverse=True,
    )
    if at_risk_subs:
        worst = at_risk_subs[0]
        risk_text = (
            f"Batch window compliance {win_comp:.0f}% ({breach_days}/{total_days} breach days). "
            f"{len(at_risk_subs)} sub-app(s) at SRI > 0.85 — worst: "
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
    critical_servers = [s for s in servers if _f(s.get("cpu_used")) >= 90]
    zero_dur = sum(1 for j in top_jobs if _f(j.get("avg_hrs")) == 0)
    if critical_servers and rfcs >= 30:
        names = ", ".join(s.get("host", "?") for s in critical_servers[:3])
        cause_text = (
            f"Resource saturation is a primary driver: {len(critical_servers)} server(s) "
            f"({names}) at ≥90% CPU. RFCS {rfcs:.1f} confirms resource→failure coupling. "
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
            [s for s in sub_app_metrics if s.get("crs", 0) > 0.3],
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

