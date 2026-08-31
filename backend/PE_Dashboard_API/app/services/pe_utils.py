"""
pe_utils.py — Single source of truth for PE Dashboard business logic.

Implements all rules from the PE Generic Rules document:
  - job_status()          → BREACH | AT_RISK | CAUTION | OK | UNKNOWN
  - buffer_pct()          → SLA headroom (positive = within SLA, negative = over)
  - fmt_pct()             → display formatter preserving sub-1% precision
  - detect_batch_type()   → DAILY | WEEKLY | MONTHLY
  - fleet_grade()         → CRITICAL | WARNING | MODERATE | HEALTHY | N/A
  - STATUS_COLOR          → dict of hex colours for all status values
  - normalise_status()    → canonical status string from any Ctrl-M variant

RULE: Every tab must call these functions — NEVER compute status inline.
"""
from __future__ import annotations

import math
from typing import Any

# ── Status colour palette (aligned with Tailwind theme) ──────────────────────
STATUS_COLOR: dict[str, str] = {
    "BREACH":    "#f43f5e",   # Cred
    "AT_RISK":   "#f59e0b",   # Camber
    "CAUTION":   "#eab308",   # yellow
    "OK":        "#10d96e",   # Cgreen
    "HEALTHY":   "#10d96e",
    "EXCELLENT": "#10d96e",
    "UNKNOWN":   "#6b7280",
    "CRITICAL":  "#f43f5e",
    "WARNING":   "#f59e0b",
    "MODERATE":  "#3b82f6",
}

# Accepted success variants across all Ctrl-M versions
SUCCESS_STATUSES: frozenset[str] = frozenset({
    "ENDED OK", "ENDED_OK", "OK", "COMPLETED",
    "ENDED NORMAL", "SUCCESS", "SUCCESSFUL", "DONE",
    # Handle double-space and tab variants after re.sub normalisation
    "ENDED  OK",
})


def _normalise_status_str(raw: str) -> str:
    """Collapse internal whitespace + underscore, uppercase."""
    import re as _re
    return _re.sub(r"[\s_]+", " ", str(raw).upper()).strip()


# ── Core status function (RULE 1) ─────────────────────────────────────────────

def job_status(peak_hrs: float, sla_hrs: float) -> str:
    """
    Single source of truth for job SLA status.

    Use in ALL tabs — SLA Matrix, Correlation, PE Findings, Red Flags,
    Executive Summary.  NEVER compute status inline in any tab.

    Returns: BREACH | AT_RISK | CAUTION | OK | UNKNOWN
    """
    if not sla_hrs or sla_hrs <= 0:
        return "UNKNOWN"
    try:
        ratio = float(peak_hrs) / float(sla_hrs)
    except (TypeError, ZeroDivisionError, ValueError):
        return "UNKNOWN"
    if   ratio >= 1.00: return "BREACH"    # over SLA ceiling
    elif ratio >= 0.85: return "AT_RISK"   # within 15% of ceiling
    elif ratio >= 0.70: return "CAUTION"   # 70–85% — watch
    else:               return "OK"        # healthy headroom


def buffer_pct(peak_hrs: float, sla_hrs: float) -> float:
    """
    Remaining SLA headroom as a percentage of the SLA window.

    Sign convention (used everywhere in the dashboard):
        positive  → job ran within SLA (within = +n% of headroom remaining)
        zero      → job hit the ceiling exactly
        negative  → job exceeded SLA (e.g. -25%% means 25%% over the limit)

    Capped at +100 (no over-headroom inflation when peak == 0).
    """
    if not sla_hrs or float(sla_hrs) <= 0:
        return 0.0
    try:
        pct = (float(sla_hrs) - float(peak_hrs)) / float(sla_hrs) * 100.0
        # Cap upside at +100 (no inflation when peak ≈ 0), but preserve negative
        # values — a negative buffer_pct correctly signals the overrun magnitude.
        return round(min(100.0, max(-9999.0, pct)), 2)
    except (TypeError, ValueError):
        return 0.0


# ── Float formatting (RULE 5 — never lose sub-1% precision) ──────────────────

def fmt_pct(val: Any, decimals: int = 2) -> str:
    """
    Format a percentage value preserving sub-1% precision.

    Examples:  0.9625 → "0.96%"   45.3 → "45.3%"   100 → "100%"   None → "–"
    """
    if val is None:
        return "–"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "–"
    if math.isnan(v) or math.isinf(v):
        return "–"
    if v < 10:
        return f"{v:.{decimals}f}%"   # 0.96%, 1.25%, 9.87%
    if v < 100:
        return f"{v:.1f}%"            # 45.3%, 89.7%
    return f"{v:.0f}%"                # 100%


def safe_metric(val: Any, metric_type: str = "PCT") -> float | None:
    """
    Validate a metric value for storage.

    RULE 4 + RULE 5: Never store int() or 0 for a missing value.
    Returns None if val is missing/invalid; otherwise round(val, 2).
    """
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if metric_type in ("PCT", "CPU", "MEMORY", "DISK"):
        # Azure Monitor % charts: legitimately 0.01–100
        if 0.0 <= v <= 100.0:
            return round(v, 4)   # preserve 4 decimal places for sub-1% values
        return None
    if metric_type == "BANDWIDTH":
        return round(v, 4) if v >= 0 else None
    return round(v, 2)


# ── Batch type detection (RULE 2 / RULE 6) ───────────────────────────────────

def detect_batch_type(job_name: str) -> str:
    """
    Infer DAILY | WEEKLY | BIWEEKLY | MONTHLY from the job name suffix.

    Conventions: _D = DAILY, _W = WEEKLY, _BW or _2W = BIWEEKLY, _M = MONTHLY.
    Also handles names that contain DAILY/WEEKLY/BIWEEKLY/MONTHLY verbatim.
    """
    name = (job_name or "").upper()
    if name.endswith("_BW") or name.endswith("_2W") or "BIWEEKLY" in name or "BI-WEEKLY" in name or "FORTNIGHT" in name:
        return "BIWEEKLY"
    if name.endswith("_W") or "WEEKLY" in name:
        return "WEEKLY"
    if name.endswith("_M") or "MONTHLY" in name:
        return "MONTHLY"
    return "DAILY"


# ── Fleet grade (RULE 7) ──────────────────────────────────────────────────────

def fleet_grade(servers: list[dict[str, Any]]) -> str:
    """
    Compute fleet health grade from a list of server dicts.
    Each dict should have cpu_used and mem_used keys.

    Returns: CRITICAL | WARNING | MODERATE | HEALTHY | N/A
    """
    if not servers:
        return "N/A"
    peaks = []
    for s in servers:
        cpu = safe_metric(s.get("cpu_used"), "CPU")
        mem = safe_metric(s.get("mem_used"), "MEMORY")
        if cpu is not None:
            peaks.append(cpu)
        if mem is not None:
            peaks.append(mem)
    if not peaks:
        return "N/A"
    peak = max(peaks)
    if   peak >= 90: return "CRITICAL"
    elif peak >= 75: return "WARNING"
    elif peak >= 50: return "MODERATE"
    else:            return "HEALTHY"


# ── Status normalisation (RULE 3) ────────────────────────────────────────────

def normalise_status(raw: str) -> str:
    """
    Normalise any Ctrl-M completion status string to "OK" or "FAILED".

    Handles: ENDED OK, Ended OK, ended ok, ENDED_OK, ENDED  OK (double space), etc.
    Uses _normalise_status_str() to collapse whitespace variants before lookup.
    """
    if not raw:
        return "FAILED"
    cleaned = _normalise_status_str(raw)
    return "OK" if cleaned in SUCCESS_STATUSES else "FAILED"


# ── SLA ceilings resolution (RULE 2) ─────────────────────────────────────────

def get_sla_hrs(batch_type: str, sla_ceilings: dict[str, float] | None = None) -> float:
    """
    Get SLA ceiling for a given batch type.

    Priority:  uploaded sla_ceilings dict  >  pe_config defaults  >  6.0h fallback.
    NEVER read pe_config directly in a tab — always call this function.
    """
    from services.pe_config import (
        SLA_DAILY_HRS, SLA_WEEKLY_HRS, SLA_BIWEEKLY_HRS,
        SLA_MONTHLY_HRS, SLA_CUSTOM_HRS,
    )
    defaults = {
        "DAILY":    SLA_DAILY_HRS,
        "WEEKLY":   SLA_WEEKLY_HRS,
        "BIWEEKLY": SLA_BIWEEKLY_HRS,
        "MONTHLY":  SLA_MONTHLY_HRS,
        "CUSTOM":   SLA_CUSTOM_HRS,
    }
    if sla_ceilings:
        return float(sla_ceilings.get(batch_type, defaults.get(batch_type, 6.0)))
    return float(defaults.get(batch_type, 6.0))


# ── Generic numeric coercion helpers (shared by all routers) ─────────────────
# Import via:  from services.pe_utils import coerce_float as _f, coerce_int as _i

def coerce_float(val: Any, default: float = 0.0) -> float:
    """
    Safely coerce *val* to float.

    Unlike ``float(val or default)`` this handles legitimate zero values
    correctly (0.0 is not treated as falsy) and also guards against NaN.
    Returns *default* for None, NaN, inf, or any non-numeric input.
    """
    if val is None:
        return default
    try:
        v = float(val)
        # Reject NaN and infinity — neither is useful as a metric value
        if v != v or v == float("inf") or v == float("-inf"):
            return default
        return v
    except (TypeError, ValueError):
        return default


def coerce_int(val: Any, default: int = 0) -> int:
    """
    Safely coerce *val* to int.

    Works via float() first so strings like ``"3.7"`` round correctly
    rather than raising ValueError.  Returns *default* for None / invalid.
    """
    if val is None:
        return default
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default
