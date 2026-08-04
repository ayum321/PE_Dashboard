"""
Correlation Engine — executive-level formulas connecting Batch, Resource, and SLA.

Five composite metrics:
  RFCS  — Resource-Failure Correlation Score  (0–100)
  SRI   — SLA Risk Index                      (0–∞, >1 = breach)
  JRTOS — Job-Resource Temporal Overlap Score  per hour (0-23)
  CRS   — Cascade Risk Score                  (0–1)
  OSHS  — Overall System Health Score         (0–100 → A/B/C/D/F)

The executive-narrative text generator lives in services/exec_narrative.py,
not here — this module is pure scoring math, no prose/business-decision logic.
All weights/thresholds are named constants in services/pe_config.py (RFCS_*,
SRI_*, CRS_*, OSHS_*, RESSCORE_*) — never hardcode a formula weight here.
"""
from __future__ import annotations

import math
from typing import Any

from services.pe_utils import coerce_float as _f, coerce_int as _i
from services import pe_config as _pc


# ── Grade table ──────────────────────────────────────────────────────────────
# Delegates to pe_config.score_to_grade() — single source of truth.
from services.pe_config import score_to_grade as _score_to_grade

def _grade(score: float) -> tuple[str, str]:
    return _score_to_grade(score)


def _resource_pressure(avg_cpu: float, avg_mem: float) -> float:
    """Shared CPU/mem weighted-pressure calc — used by calc_rfcs() AND
    build_sub_app_metrics() so the two can never drift onto different weights."""
    return avg_cpu * _pc.RFCS_CPU_WEIGHT + avg_mem * _pc.RFCS_MEM_WEIGHT


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
    RFCS = failure_rate × (avg_resource_pressure / 100) × (1 + RFCS_CRITSERVER_AMPLIFIER × critical_servers)

    Clamped to 0–100. Measures how much resource stress correlates with failures.
    Weights/amplifier/cap are the RFCS_ constants in pe_config.py — see _resource_pressure().
    """
    resource_pressure = _resource_pressure(avg_cpu, avg_mem)
    base = failure_rate * (resource_pressure / 100.0)
    amplifier = 1.0 + _pc.RFCS_CRITSERVER_AMPLIFIER * min(critical_server_count, _pc.RFCS_CRITSERVER_CAP)
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

    resource_amplifier = 1 + max(0, (avg_cpu - SRI_CPU_AMP_THRESHOLD) / 100)
    SRI > 1.0 → breach even with resource load factored in.
    """
    if sla_ceiling_hrs <= 0:
        return 0.0
    resource_amp = 1.0 + max(0.0, (avg_cpu - _pc.SRI_CPU_AMP_THRESHOLD) / 100.0)
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
    CRS = failed_flag × (downstream_count / (downstream_count + CRS_CHAIN_DENOM_OFFSET)) × (1 - sla_buffer / 100)

    Returns 0–1. A high CRS means this single job failure could collapse its chain.
    """
    if not is_failed or downstream_count <= 0:
        return 0.0
    chain_factor = downstream_count / (downstream_count + _pc.CRS_CHAIN_DENOM_OFFSET)
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
    W_BATCH, W_SLA, W_RES = _pc.OSHS_W_BATCH, _pc.OSHS_W_SLA, _pc.OSHS_W_RES
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
    return min(100.0, max(0.0, compliance_pct * _pc.DERIVE_BATCH_COMPLIANCE_WEIGHT
                          + (100.0 - fail_rate) * _pc.DERIVE_BATCH_FAILRATE_WEIGHT))

def derive_resource_score(avg_cpu: float, avg_mem: float, avg_disk: float) -> float:
    """0-100 resource health — higher is better (lower utilization).

    Weights are the RESSCORE_ constants in pe_config.py — numerically equal to
    OSHS_W_BATCH/SLA/RES by coincidence only; kept as separate named constants
    on purpose (see pe_config.py comment) since these two triads mean different things.
    """
    pressure = (avg_cpu * _pc.RESSCORE_CPU_WEIGHT + avg_mem * _pc.RESSCORE_MEM_WEIGHT
                + avg_disk * _pc.RESSCORE_DISK_WEIGHT)
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
    resource_pressure = _resource_pressure(avg_cpu, avg_mem)
    crit_count = sum(1 for s in servers if _f(s.get("cpu_used")) >= _pc.CPU_CRIT)

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
            "rfcs_band":        "red" if rfcs >= _pc.RFCS_BAND_RED else ("amber" if rfcs >= _pc.RFCS_BAND_AMBER else "green"),
            "resource_pressure": round(resource_pressure, 1),
            "crs":              round(crs, 3),
        })

    results.sort(key=lambda x: x["sri"], reverse=True)
    return results


def _avg_metric(servers: list[dict], key: str) -> float:
    vals = [_f(s.get(key)) for s in servers if _f(s.get(key)) > 0]
    return sum(vals) / len(vals) if vals else 0.0

