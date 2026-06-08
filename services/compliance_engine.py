"""
Shared window compliance calculation.

Both batch_calculator.compute_metrics() and sla_matrix._compute_sla_matrix()
import and call compute_window_compliance() here.  No file computes compliance
independently — the numbers on the Batch Review tab and SLA Matrix tab are
always identical for the same data.

Formula (canonical):
    denominator = unique (sub_app, run_date) pairs where schedule_type is NOT
                  in pe_config.COMPLIANCE_EXCLUDED_TYPES
    numerator   = denominator rows where actual_window_hrs <= sla_ceiling_hrs
    compliance% = numerator / denominator × 100
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def compute_window_compliance(
    window_records: List[Dict[str, Any]],
    ceiling_map: Dict[str, Any],
    excluded_types: Optional[set] = None,
) -> Dict[str, Any]:
    """Compute batch window compliance from sentinel-measured window records.

    Parameters
    ----------
    window_records:
        List of dicts, each representing one (sub_app, run_date) observation.
        Required keys: ``sub_app``, ``run_date``, ``elapsed_hrs``.
        Optional keys: ``schedule_type``, ``sla_ceil``, ``sentinel_source``.
    ceiling_map:
        Dict mapping sub_app name → SLA ceiling in hours (or None = excluded).
        Produced by batch_calculator._build_sla_ceiling_map().
    excluded_types:
        Set of schedule type strings that are NEVER counted in the denominator.
        Defaults to pe_config.COMPLIANCE_EXCLUDED_TYPES when None.

    Returns
    -------
    dict with keys:
        compliance_pct   float — 0-100
        breach_count     int
        ok_count         int
        at_risk_count    int   — elapsed within 15% of ceiling
        total_windows    int   — denominator
        excluded_windows int   — rows skipped (CYCLIC, ADHOC, etc.)
    """
    try:
        from services import pe_config as _pc
        _excluded = excluded_types if excluded_types is not None else _pc.COMPLIANCE_EXCLUDED_TYPES
        _atrisk_pct = _pc.SLA_ATRISK_PCT
        _daily_default = _pc.SLA_DAILY_HRS
    except Exception:
        _excluded = {"CYCLIC", "CYCLIC_INTERVAL", "ADHOC", "CALENDAR_BASED",
                     "MONTHLY", "ANNUAL", "UNKNOWN"}
        _atrisk_pct = 15.0
        _daily_default = 6.0

    total_windows = 0
    excluded_windows = 0
    breach_count = 0
    ok_count = 0
    at_risk_count = 0

    for rec in window_records:
        sub_app  = str(rec.get("sub_app") or rec.get("Sub_Application") or "")
        sched    = str(rec.get("schedule_type") or "").upper()
        elapsed  = float(rec.get("elapsed_hrs") or 0.0)

        # Skip CYCLIC/ADHOC/excluded schedule types
        if sched in _excluded:
            excluded_windows += 1
            continue

        # Determine ceiling: from record → ceiling_map → default
        ceil = rec.get("sla_ceil")
        if ceil is None:
            ceil = ceiling_map.get(sub_app)
        if ceil is None:
            # Unknown sub_app — check if it appears cyclic via ceiling_map None sentinel
            ceil = _daily_default
        if ceil is None or ceil <= 0:
            excluded_windows += 1
            continue

        total_windows += 1
        if elapsed > ceil:
            breach_count += 1
        else:
            buffer_pct = (ceil - elapsed) / ceil * 100
            if buffer_pct <= _atrisk_pct:
                at_risk_count += 1
            else:
                ok_count += 1

    compliance_pct = (
        round((ok_count + at_risk_count) / total_windows * 100, 1)
        if total_windows > 0 else 0.0
    )
    return {
        "compliance_pct":   compliance_pct,
        "breach_count":     breach_count,
        "ok_count":         ok_count,
        "at_risk_count":    at_risk_count,
        "total_windows":    total_windows,
        "excluded_windows": excluded_windows,
    }
