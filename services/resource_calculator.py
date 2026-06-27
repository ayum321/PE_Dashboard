"""
Resource utilization calculator — port of fleet_intelligence_engine
and the F7/F8 host/fleet health logic from app_v2.py.

Public API
----------
    build_resource_payload(servers: list[dict]) -> dict
        Takes parsed server records (output of services.resource_parser
        / the /api/upload endpoint) and returns the JSON-ready envelope
        consumed by the Resource Review tab.

The threshold constants, weights, and A-F grade boundaries are
unchanged from the original Streamlit monolith — only `st.*` calls
have been removed and numpy is used directly (no caching wrappers).
Image-only DOCX servers (all-zero metrics) are flagged with
`image_only=True` and skipped from averages / anomaly detection.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from services.resource_parser import (
    _calculate_host_health,
    _infer_environment,
    get_health_score,
)

# ── Threshold constants (verbatim from app_v2.py L25-31) ───────
CPU_OK    = 75.0
CPU_WARN  = 90.0
MEM_OK    = 75.0
MEM_WARN  = 90.0
DISK_OK   = 75.0
DISK_WARN = 90.0

# ── Role-specific CPU thresholds ───────────────────────────────
# APP servers should rarely exceed 60%; DB servers expect batch-time
# CPU spikes; SRE/batch servers only alarm on sustained collision.
_ROLE_CPU = {
    "APP": {"ok": 60.0, "warn": 80.0},
    "DB":  {"ok": 85.0, "warn": 95.0},
    "SRE": {"ok": 90.0, "warn": 100.0},
}

# ── Role-specific memory governing band ────────────────────────
# DB servers pre-allocate 80–92% of RAM to SGA/PGA by design, so memory inside
# this band is EXPECTED behaviour, not a warning. Other roles fall back to the
# global MEM_WARN / MEM_CRIT thresholds from pe_config. Centralised here so the
# narrative layer surfaces the SAME governing ceiling the grader actually used.
DB_MEM_EXPECTED_LO = 80.0
DB_MEM_EXPECTED_HI = 92.0

# ── Aggregation trap detection thresholds ──────────────────────
# When Max CPU is very high but Avg CPU is very low, the spike is a
# visual aggregation artifact (e.g., one 99% sample across a week),
# not sustained pressure.  The server is actually HEALTHY.
_AGG_TRAP_MAX_FLOOR = 85.0   # Max CPU must be ≥ this to trigger check
_AGG_TRAP_AVG_CEIL  = 20.0   # Avg CPU must be < this → false alarm

# Minimum floors for dynamic thresholds (won't go below these)
_FLOOR_WARN = {"cpu": 60.0, "mem": 60.0, "disk": 60.0}
_FLOOR_CRIT = {"cpu": 80.0, "mem": 80.0, "disk": 80.0}


# ── Helpers ────────────────────────────────────────────────────
def _f(v: Any) -> float:
    """Coerce-to-float that tolerates None / strings / NaN."""
    try:
        out = float(v)
        return 0.0 if (out != out) else out  # NaN check
    except (TypeError, ValueError):
        return 0.0


def is_unknown_server(s: dict) -> bool:
    """True if server data came from image-only DOCX (all zeros).
    Mirrors `is_unknown_server` from app_v2.py L5985."""
    return (
        _f(s.get("cpu_used")) == 0
        and _f(s.get("disk_used_max")) == 0
        and _f(s.get("mem_total_gb")) == 0
        and not s.get("disks")
    )


def status_band(cpu: float, mem: float, disk: float, server_type: str = "APP") -> str:
    """Map (cpu, mem, disk) → Critical / Warning / Healthy / Unknown.
    Aligned with calculate_host_health bands (HEALTHY≥80, WARNING≥60)."""
    score = get_health_score(cpu, mem, disk, server_type)
    if score < 0:
        return "Unknown"
    if score >= 80:
        return "Healthy"
    if score >= 60:
        return "Warning"
    return "Critical"


def detect_aggregation_trap(cpu_max: float, cpu_avg: float) -> bool:
    """Return True when Max CPU is high but Avg CPU is very low —
    this is a visual aggregation artifact, not sustained saturation."""
    return cpu_max >= _AGG_TRAP_MAX_FLOOR and cpu_avg < _AGG_TRAP_AVG_CEIL and cpu_avg > 0


def detect_dual_pressure(cpu: float, mem: float) -> bool:
    """Return True when CPU AND Memory are both under heavy pressure.
    High CPU alone = working hard.  High CPU + High Memory (>85%) =
    severe resource exhaustion / swapping / undersized server."""
    return cpu >= 80.0 and mem >= 85.0


def role_cpu_thresholds(server_type: str) -> Dict[str, float]:
    """Return the (ok, warn) CPU thresholds for a given server role."""
    return _ROLE_CPU.get(server_type.upper(), _ROLE_CPU["APP"])


def mem_threshold(server_type: str) -> float:
    """Governing memory ceiling per role: above this, memory is flagged.

    DB servers tolerate the SGA/PGA band up to DB_MEM_EXPECTED_HI; every other
    role uses the global MEM_CRIT threshold (single-sourced from pe_config). The
    narrative layer reads this so the threshold it prints next to a peak is the
    exact one the fleet grader applied — no flat invented number."""
    from services.pe_config import MEM_CRIT
    return DB_MEM_EXPECTED_HI if (server_type or "").upper() == "DB" else MEM_CRIT


# ── F8 — Fleet Health (verbatim port) ───────────────────────────
def calculate_fleet_health(servers: List[dict]) -> Dict[str, Any]:
    """F8 — Fleet Health: aggregate host scores into fleet grade A-F.
    Unknown (all-zero) servers are excluded from the average score.
    If fewer than 50% of servers have real CPU/MEM data, grade = 'N/A'."""
    if not servers:
        return {"fleet_score": 0.0, "grade": "N/A", "total": 0,
                "healthy": 0, "warning": 0, "critical": 0, "unknown": 0,
                "data_quality": "NO_DATA"}

    scores: List[float] = []
    healthy = warning = critical = unknown = 0
    for s in servers:
        cpu  = _f(s.get("cpu_used"))
        mem  = _f(s.get("mem_used"))
        disk = _f(s.get("disk_used_max"))
        server_type = (s.get("type") or "APP").upper()
        if cpu == 0 and mem == 0 and disk == 0:
            unknown += 1
            continue
        score = get_health_score(cpu, mem, disk, server_type)
        if score < 0:
            unknown += 1
            continue
        scores.append(score)
        if   score >= 80:  healthy  += 1
        elif score >= 60:  warning  += 1
        else:              critical += 1

    total = len(servers)
    known_pct = (len(scores) / total * 100) if total > 0 else 0.0

    # Grade N/A: if fewer than 50% of servers have real data
    if known_pct < 50:
        return {"fleet_score": 0.0, "grade": "N/A", "total": total,
                "healthy": healthy, "warning": warning, "critical": critical,
                "unknown": unknown, "data_quality": "INSUFFICIENT",
                "known_pct": round(known_pct, 1)}

    fleet_score = round(float(np.mean(scores)), 1) if scores else 0.0
    from services.pe_config import score_to_grade
    grade, _ = score_to_grade(fleet_score)

    return {"fleet_score": fleet_score, "grade": grade, "total": total,
            "healthy": healthy, "warning": warning, "critical": critical,
            "unknown": unknown, "data_quality": "VERIFIED",
            "known_pct": round(known_pct, 1)}


# ── Fleet Intelligence Engine — F8 + F6 anomalies ──────────────
def fleet_intelligence_engine(server_data: List[dict]) -> Optional[Dict[str, Any]]:
    """F8 fleet health + F6 z-score anomaly detection across the fleet.
    Returns None for empty input. Anomaly detection requires ≥3 known servers."""
    if not server_data:
        return None

    fleet = calculate_fleet_health(server_data)

    anomalies: List[Dict[str, Any]] = []
    try:
        known = [s for s in server_data if not is_unknown_server(s)]
        if len(known) >= 3:
            for metric, key in [
                ("CPU",    "cpu_used"),
                ("Disk",   "disk_used_max"),
                ("Memory", "mem_used"),
            ]:
                vals = [_f(s.get(key)) for s in known]
                mu  = float(np.mean(vals))
                # Use sample std (ddof=1) for small fleets — more statistically
                # appropriate when N < 30.  Falls back to ddof=0 if N < 3.
                ddof = 1 if len(vals) >= 3 else 0
                std = float(np.std(vals, ddof=ddof))
                if std < 1e-6:
                    continue
                for s, v in zip(known, vals):
                    z = (v - mu) / std
                    if abs(z) >= 2.0:
                        anomalies.append({
                            "host":   s.get("host", "?"),
                            "metric": metric,
                            "value":  round(float(v), 1),
                            "z":      round(z, 2),
                        })
            anomalies.sort(key=lambda x: abs(x["z"]), reverse=True)
            anomalies = anomalies[:10]  # cap at 10 to bound payload size
    except Exception:
        pass

    return {
        "score":     fleet["fleet_score"],
        "grade":     fleet["grade"],
        "healthy":   fleet["healthy"],
        "warning":   fleet["warning"],
        "critical":  fleet["critical"],
        "unknown":   fleet["unknown"],
        "anomalies": anomalies,
    }


# ── Row normalisation ──────────────────────────────────────────
def normalize_server(s: dict) -> Dict[str, Any]:
    """Convert a parsed server record into the row shape used by the
    Resource Review dashboard. Handles missing fields gracefully so
    image-only DOCX servers still render (with 0% values).

    Intelligence fields added:
      - agg_trap:       True if Max CPU ≥85% but Avg <20% (false alarm)
      - dual_pressure:  True if CPU ≥80% AND Mem ≥85% (severe exhaustion)
      - effective_cpu:  The CPU value used for health scoring
                        (uses cpu_avg when agg_trap detected)
      - role_cpu_ok:    Role-specific CPU OK threshold
      - role_cpu_warn:  Role-specific CPU warning threshold
    """
    cpu     = _f(s.get("cpu_used"))
    mem     = _f(s.get("mem_used"))
    disk    = _f(s.get("disk_used_max"))
    cpu_avg = _f(s.get("cpu_avg"))
    mem_gb  = _f(s.get("mem_total_gb"))
    server_type = (s.get("type") or "APP").upper()
    image_only  = is_unknown_server(s)
    vision_enriched = bool(s.get("_vision_enriched") or s.get("vision_enriched"))

    # ── Aggregation trap detection ──────────────────────────
    agg_trap = detect_aggregation_trap(cpu, cpu_avg) if not image_only else False

    # When aggregation trap detected, use cpu_avg for health scoring
    # because the Max spike is a visual artifact, not sustained load
    effective_cpu = cpu_avg if agg_trap else cpu

    # ── Dual pressure detection ─────────────────────────────
    dual = detect_dual_pressure(cpu, mem) if not image_only else False

    # ── Role-specific thresholds ────────────────────────────
    role_thresh = role_cpu_thresholds(server_type)

    score  = get_health_score(effective_cpu, mem, disk, server_type)
    status = "Unknown" if image_only else status_band(effective_cpu, mem, disk, server_type)

    host = s.get("host") or "?"

    # ── Environment inference ───────────────────────────────
    environment = s.get("environment") or _infer_environment(host)

    # ── Data-availability flags ─────────────────────────────
    # Don't show 0.0% for memory/disk unless we truly have source metrics.
    # If both the percentage and the absolute value are zero, mark as None
    # so the frontend shows "data unavailable" instead of fake precision.
    # Exception: vision-enriched servers have real measured values — 0% means
    # "no pressure", not "no data available".
    if vision_enriched:
        mem_available  = True
        disk_available = True
        cpu_available  = True
    else:
        mem_available  = not image_only and (mem > 0 or mem_gb > 0)
        disk_available = not image_only and disk > 0
        cpu_available  = not image_only and (cpu > 0 or cpu_avg > 0)

    # ── DB memory expected band — role-specific status override ─────────────
    # Oracle/SQL DB servers pre-allocate 80–92% of RAM to SGA/PGA by design.
    # The base health score (get_health_score) restores up to 20 pts of leniency
    # but does not always push the score above the 80-pt Healthy boundary when
    # CPU or disk metrics also contribute. Explicitly override Warning → Healthy
    # when the DB server's ONLY issue is memory inside the expected band.
    _db_mem_expected = (
        server_type == "DB"
        and mem_available
        and DB_MEM_EXPECTED_LO <= mem <= DB_MEM_EXPECTED_HI
        and not image_only
    )
    if _db_mem_expected and status == "Warning":
        _cpu_ok   = effective_cpu < role_thresh["ok"]
        _disk_ok  = not disk_available or disk < DISK_OK
        if _cpu_ok and _disk_ok:
            status = "Healthy"

    # mem_status: memory-specific classification for frontend tooltip/colour
    # "DB_NORMAL"  — DB in expected SGA/PGA band (8–20% available); do not alarm
    # "DB_HIGH"    — DB above expected band (> 92% used / < 8% available); flag
    # None         — use standard thresholds
    if server_type == "DB" and mem_available and not image_only:
        if DB_MEM_EXPECTED_LO <= mem <= DB_MEM_EXPECTED_HI:
            mem_status: Optional[str] = "DB_NORMAL"
        elif mem > DB_MEM_EXPECTED_HI:
            mem_status = "DB_HIGH"
        else:
            mem_status = None
    else:
        mem_status = None

    return {
        "host":           host,
        "server":         host.split(".")[0],
        "type":           server_type,
        "environment":    environment,
        "cpu_pct":        round(cpu, 1) if cpu_available else None,
        "cpu_avg_pct":    round(cpu_avg, 1) if cpu_available else None,
        "effective_cpu":  round(effective_cpu, 1) if cpu_available else None,
        "mem_pct":        round(mem, 1) if mem_available else None,
        "mem_gb":         round(mem_gb, 1) if mem_available else None,
        "disk_pct":       round(disk, 1) if disk_available else None,
        "disks":          s.get("disks") or {},
        "image_only":     image_only,
        "health_score":   round(float(score), 1) if score >= 0 else None,
        "status":         status,
        "mem_status":     mem_status,
        "source_env":     s.get("_source_file") or s.get("source_env") or "",
        # Intelligence flags
        "agg_trap":       agg_trap,
        "dual_pressure":  dual,
        "role_cpu_ok":    role_thresh["ok"],
        "role_cpu_warn":  role_thresh["warn"],
        # Data availability (frontend uses for "data unavailable" display)
        "cpu_available":  cpu_available,
        "mem_available":  mem_available,
        "disk_available": disk_available,
        # ── Aliases used by executive.py and correlation_engine.py ──────────
        # These files read cpu_used/mem_used/disk_used_max from server dicts.
        # Duplicate the same values under both names so both reading styles work.
        "cpu_used":      round(cpu, 1) if cpu_available else None,
        "mem_used":      round(mem, 1) if mem_available else None,
        "disk_used_max": round(disk, 1) if disk_available else None,
        # Passthrough fields for Azure re-fetch
        "resource_id":   s.get("resource_id") or None,
        "source":        s.get("source") or "",
    }


# ── Dynamic threshold engine ────────────────────────────────────
def compute_dynamic_thresholds(servers: List[dict]) -> Dict[str, Dict[str, float]]:
    """Compute fleet-adaptive thresholds: mean + 1σ = warning, mean + 2σ = critical.

    Falls back to static thresholds when the fleet is too small (<5 servers)
    or data is insufficient. Floors prevent thresholds from dropping below
    sensible minimums (60%/80%).
    """
    known = [s for s in (servers or []) if not is_unknown_server(s)]
    if len(known) < 5:
        return {
            "cpu":  {"warn": CPU_OK,   "crit": CPU_WARN},
            "mem":  {"warn": MEM_OK,   "crit": MEM_WARN},
            "disk": {"warn": DISK_OK,  "crit": DISK_WARN},
            "source": "static",
        }

    result = {"source": "dynamic"}
    for metric, key in [("cpu", "cpu_used"), ("mem", "mem_used"), ("disk", "disk_used_max")]:
        vals = np.array([_f(s.get(key)) for s in known])
        mu = float(np.mean(vals))
        sigma = float(np.std(vals))
        warn = max(_FLOOR_WARN[metric], round(mu + sigma, 1))
        crit = max(_FLOOR_CRIT[metric], round(mu + 2 * sigma, 1))
        # Ensure crit > warn
        if crit <= warn:
            crit = warn + 5.0
        result[metric] = {"warn": round(warn, 1), "crit": round(min(crit, 100.0), 1)}

    return result


# ── Top-level builder ──────────────────────────────────────────
def build_resource_payload(servers: List[dict]) -> Dict[str, Any]:
    """JSON-ready envelope returned by POST /api/process-resource."""
    rows = [normalize_server(s) for s in (servers or [])]
    fie  = fleet_intelligence_engine(servers or [])

    known   = [r for r in rows if not r["image_only"]]
    n_total = len(rows)
    n_known = len(known)

    if n_known:
        cpu_vals  = [r["cpu_pct"]  for r in known if r["cpu_pct"]  is not None]
        mem_vals  = [r["mem_pct"]  for r in known if r["mem_pct"]  is not None]
        disk_vals = [r["disk_pct"] for r in known if r["disk_pct"] is not None]
        avg_cpu  = round(sum(cpu_vals)  / max(len(cpu_vals), 1),  1)
        avg_mem  = round(sum(mem_vals)  / max(len(mem_vals), 1),  1)
        avg_disk = round(sum(disk_vals) / max(len(disk_vals), 1), 1)
    else:
        avg_cpu = avg_mem = avg_disk = 0.0

    n_crit = sum(1 for r in known if r["status"] == "Critical")
    n_warn = sum(1 for r in known if r["status"] == "Warning")
    n_ok   = sum(1 for r in known if r["status"] == "Healthy")

    n_app = sum(1 for r in rows if r["type"] == "APP")
    n_db  = sum(1 for r in rows if r["type"] == "DB")
    n_sre = sum(1 for r in rows if r["type"] == "SRE")

    n_prod = sum(1 for r in rows if r["environment"] == "PROD")
    n_test = sum(1 for r in rows if r["environment"] == "TEST")
    n_dev  = sum(1 for r in rows if r["environment"] == "DEV")

    grade = (fie or {}).get("grade", "?")
    score = (fie or {}).get("score", 0.0)

    # ── Intelligence counts ──────────────────────────────────
    agg_trap_servers  = [r for r in known if r.get("agg_trap")]
    dual_press_servers = [r for r in known if r.get("dual_pressure")]

    # ── 4-Part Executive Summary ─────────────────────────────
    exec_summary = _build_executive_summary(
        known, n_crit, n_warn, n_ok, grade, score,
        agg_trap_servers, dual_press_servers,
    )

    # Compute fleet-adaptive thresholds (mean + σ)
    dyn_thresholds = compute_dynamic_thresholds(servers or [])

    # ── Score decomposition: what cost the fleet each grade point ──
    score_decomp = _compute_score_decomposition(known, avg_cpu, avg_mem, avg_disk, score)

    return {
        "kpis": {
            "total_servers": n_total,
            "known_servers": n_known,
            "image_only":    n_total - n_known,
            "fleet_grade":   grade,
            "fleet_score":   score,
            "avg_cpu":       avg_cpu,
            "avg_mem":       avg_mem,
            "avg_disk":      avg_disk,
            "n_critical":    n_crit,
            "n_warning":     n_warn,
            "n_healthy":     n_ok,
            "n_app":         n_app,
            "n_db":          n_db,
            "n_sre":         n_sre,
            "n_prod":        n_prod,
            "n_test":        n_test,
            "n_dev":         n_dev,
            "n_agg_trap":    len(agg_trap_servers),
            "n_dual_pressure": len(dual_press_servers),
            "thresholds": {
                "cpu_ok":   CPU_OK,   "cpu_warn":  CPU_WARN,
                "mem_ok":   MEM_OK,   "mem_warn":  MEM_WARN,
                "disk_ok":  DISK_OK,  "disk_warn": DISK_WARN,
            },
            "dynamic_thresholds": dyn_thresholds,
            "score_decomposition": score_decomp,
        },
        "anomalies": (fie or {}).get("anomalies", []),
        "servers":   rows,
        "executive_summary": exec_summary,
    }


# ── Score Decomposition — what costs grade points ─────────────
def _compute_score_decomposition(
    known: List[dict],
    avg_cpu: float,
    avg_mem: float,
    avg_disk: float,
    fleet_score: float,
) -> Dict[str, Any]:
    """Break fleet score (100-point scale) into per-pillar point-loss
    using counterfactual analysis against the ACTUAL per-server scoring
    (including DB adjustments), so the decomposition sums correctly."""
    perfect = 100.0

    if not known:
        return {
            "perfect": perfect, "score": fleet_score,
            "total_lost": round(perfect - fleet_score, 1),
            "components": [], "dominant": "—", "dominant_lost": 0,
        }

    # Compute per-server actual + counterfactual scores
    actual_scores: List[float] = []
    cpu0_scores:   List[float] = []
    mem0_scores:   List[float] = []
    disk0_scores:  List[float] = []

    for s in known:
        cpu  = _f(s.get("cpu_used"))
        mem  = _f(s.get("mem_used"))
        disk = _f(s.get("disk_used_max"))
        stype = (s.get("type") or "APP").upper()

        actual = get_health_score(cpu, mem, disk, stype)
        if actual < 0:
            continue
        actual_scores.append(actual)
        # Counterfactual: what if this pillar were perfect (0%)?
        s_cpu0  = get_health_score(0, mem, disk, stype)
        s_mem0  = get_health_score(cpu, 0, disk, stype)
        s_disk0 = get_health_score(cpu, mem, 0, stype)
        cpu0_scores.append(s_cpu0 if s_cpu0 >= 0 else actual)
        mem0_scores.append(s_mem0 if s_mem0 >= 0 else actual)
        disk0_scores.append(s_disk0 if s_disk0 >= 0 else actual)

    n = len(actual_scores) or 1
    fleet_actual = sum(actual_scores) / n

    # Points lost per pillar = (fleet with pillar perfect) − fleet actual
    cpu_lost  = round(max(0, sum(cpu0_scores) / n - fleet_actual), 1)
    mem_lost  = round(max(0, sum(mem0_scores) / n - fleet_actual), 1)
    disk_lost = round(max(0, sum(disk0_scores) / n - fleet_actual), 1)

    # Interaction residual: DB adjustments couple CPU and memory,
    # so counterfactual contributions may not sum to total loss.
    # Show the residual transparently instead of hiding it.
    total_pillar = cpu_lost + mem_lost + disk_lost
    total_lost   = round(perfect - fleet_score, 1)
    residual     = round(max(0, total_lost - total_pillar), 1)

    components = [
        {"label": "CPU Load",   "weight": "30%", "avg": avg_cpu,
         "points_lost": cpu_lost,  "color": "blue"},
        {"label": "Memory",     "weight": "40%", "avg": avg_mem,
         "points_lost": mem_lost,  "color": "cyan"},
        {"label": "Disk",       "weight": "30%", "avg": avg_disk,
         "points_lost": disk_lost, "color": "purple"},
    ]
    if residual > 0.5:
        components.append({
            "label": "DB Adjustments", "weight": "—", "avg": None,
            "points_lost": residual, "color": "amber",
        })

    dominant = max(components, key=lambda c: c["points_lost"])

    return {
        "perfect": perfect,
        "score": fleet_score,
        "total_lost": total_lost,
        "components": components,
        "dominant": dominant["label"],
        "dominant_lost": dominant["points_lost"],
    }


# ── 4-Part Executive Summary Builder ──────────────────────────
def _build_executive_summary(
    known: List[dict],
    n_crit: int,
    n_warn: int,
    n_ok: int,
    grade: str,
    score: float,
    agg_trap_servers: List[dict],
    dual_press_servers: List[dict],
) -> Dict[str, Any]:
    """Generate the 4-part executive resource summary:
    1. True Health Verdict — real status after filtering false alarms
    2. False Alarms Detected — aggregation trap artifacts
    3. Actual Bottlenecks — servers with genuine pressure
    4. Executive Summary — 2-line executive-ready summary
    """
    n_total = len(known)
    n_agg   = len(agg_trap_servers)

    # ── Part 1: True Health Verdict ──────────────────────────
    # After excluding aggregation-trap false alarms,
    # what is the fleet's real status?
    real_crit = sum(
        1 for r in known
        if r["status"] == "Critical" and not r.get("agg_trap")
    )
    real_warn = sum(
        1 for r in known
        if r["status"] == "Warning" and not r.get("agg_trap")
    )

    if real_crit > 0:
        verdict = "CRITICAL"
        verdict_detail = (
            f"{real_crit} server(s) under genuine resource pressure "
            f"({n_agg} false alarm(s) already filtered). "
            f"Root cause likely: sustained workload exceeding provisioned capacity, "
            f"memory leak, or runaway process."
        )
    elif real_warn > 0:
        verdict = "WARNING"
        verdict_detail = (
            f"{real_warn} server(s) approaching threshold limits. "
            f"{n_agg} apparent alert(s) were aggregation artifacts (filtered). "
            f"Investigate whether load is scheduled (batch-driven) or organic growth."
        )
    elif n_total > 0:
        verdict = "HEALTHY"
        verdict_detail = (
            f"All {n_total} server(s) within acceptable operating range."
            + (f" {n_agg} high-Max-CPU reading(s) correctly identified as "
               f"aggregation artifacts (short spikes, not sustained load)."
               if n_agg else "")
        )
    else:
        verdict = "NO DATA"
        verdict_detail = "No server metrics available for analysis."

    # ── Part 2: False Alarms Detected ────────────────────────
    false_alarms = []
    for s in agg_trap_servers:
        _cpu = s["cpu_pct"] or 0.0
        _avg = s["cpu_avg_pct"] or 0.0
        false_alarms.append({
            "host":    s["host"],
            "type":    s["type"],
            "cpu_max": _cpu,
            "cpu_avg": _avg,
            "reason":  (
                f"Max CPU {_cpu:.1f}% but Avg only {_avg:.1f}% — "
                f"aggregation artifact, server is HEALTHY"
            ),
        })

    # ── Part 3: Actual Bottlenecks ───────────────────────────
    bottlenecks = []
    for s in known:
        if s.get("agg_trap"):
            continue  # Already classified as false alarm
        issues = []
        role_t = role_cpu_thresholds(s["type"])
        _ecpu = s["effective_cpu"] or 0.0
        _cpu  = s["cpu_pct"] or 0.0
        _mem  = s["mem_pct"] or 0.0
        if _ecpu >= role_t["warn"]:
            issues.append(f"CPU {_cpu:.1f}% (critical for {s['type']}) — check for runaway queries or batch overlap")
        elif _ecpu >= role_t["ok"]:
            issues.append(f"CPU {_cpu:.1f}% (elevated for {s['type']}) — monitor for sustained growth pattern")
        if _mem >= 85.0:
            is_db = s["type"].upper() == "DB"
            if is_db and _mem <= 92.0:
                # DB servers: 85-92% memory is expected behavior (SGA/PGA allocation)
                issues.append(f"Memory {_mem:.1f}% — expected range for DB (SGA/PGA allocation uses 80-92% by design). Monitor for growth above 93%.")
            elif is_db and _mem > 92.0:
                issues.append(f"Memory {_mem:.1f}% — exceeds expected DB range (>92%). Possible memory leak, PGA over-allocation, or VM needs more RAM.")
            else:
                issues.append(f"Memory {_mem:.1f}% — high for {s['type']} server. Check for memory leaks, large heap allocation, or undersized VM.")
        if s.get("disk_pct") is not None and s["disk_pct"] >= 85.0:
            issues.append(f"Disk {s['disk_pct']:.1f}% — check archive logs, temp tablespace, or log rotation policy")
        if s.get("dual_pressure"):
            issues.append("DUAL PRESSURE — CPU + Memory both saturated; likely swapping, check OOM killer logs")
        if issues:
            bottlenecks.append({
                "host":   s["host"],
                "type":   s["type"],
                "environment": s.get("environment", ""),
                "status": s["status"],
                "issues": issues,
            })

    # ── Part 4: Executive Summary (2 lines) ──────────────────
    line1_parts = [f"Fleet Grade {grade} ({score:.0f}/100)"]
    if n_agg:
        line1_parts.append(f"{n_agg} false alarm(s) filtered")
    if len(dual_press_servers):
        line1_parts.append(
            f"{len(dual_press_servers)} server(s) under dual CPU+Memory pressure"
        )
    line1 = " · ".join(line1_parts) + "."

    if bottlenecks:
        top_hosts = ", ".join(b["host"].split(".")[0] for b in bottlenecks[:3])
        # Identify dominant issue type for RCA direction
        all_issues = [iss for b in bottlenecks for iss in b["issues"]]
        mem_issues = sum(1 for i in all_issues if "Memory" in i or "memory" in i)
        cpu_issues = sum(1 for i in all_issues if "CPU" in i)
        dual_issues = sum(1 for i in all_issues if "DUAL" in i)

        if dual_issues:
            rca_hint = "Dual CPU+Memory saturation points to resource exhaustion under load."
        elif mem_issues > cpu_issues:
            # Check if ALL memory-flagged servers are DBs within expected range
            db_expected = sum(1 for b in bottlenecks
                             for iss in b["issues"]
                             if "Memory" in iss and "expected range for DB" in iss)
            if db_expected == mem_issues and db_expected > 0:
                rca_hint = "Memory usage is within expected DB range (SGA/PGA allocation). No action needed unless growth trend detected."
            else:
                rca_hint = "Memory is the primary constraint. Check for leaks or right-size VMs."
        elif cpu_issues > 0:
            rca_hint = "CPU pressure is the lead indicator. Review batch concurrency and query plans."
        else:
            rca_hint = "Multiple resource dimensions under pressure."

        line2 = (
            f"Action required on {len(bottlenecks)} server(s): {top_hosts}"
            f"{'...' if len(bottlenecks) > 3 else ''}. "
            f"{rca_hint}"
        )
    elif n_agg and real_crit == 0 and real_warn == 0:
        line2 = (
            "All apparent CPU alerts trace back to short-lived aggregation spikes. "
            "The fleet is operationally healthy."
        )
    else:
        line2 = "All servers within acceptable thresholds — PE audit ready."

    return {
        "verdict":       verdict,
        "verdict_detail": verdict_detail,
        "false_alarms":  false_alarms,
        "bottlenecks":   bottlenecks,
        "summary_line1": line1,
        "summary_line2": line2,
    }
