"""
Shared window compliance calculation AND ceiling-map construction.

Both batch_calculator.compute_metrics() and sla_matrix._compute_sla_matrix()
import from here.  No module builds its own ceiling map independently — the
numbers on the Batch Review tab and SLA Matrix tab are always identical for
the same data.

Formula (canonical):
    denominator = unique run_date values in scope
    numerator   = denominator days where actual_window_hrs <= sla_ceiling_hrs
    compliance% = numerator / denominator × 100

Canonical window rule:
    elapsed window for a day = max(end_time) - min(start_time) grouped by date
    compared against the resolved SLA ceiling for that day.

Ceiling map resolution (canonical, highest → lowest priority):
    1. XLSX workflow SLA  — fuzzy substring match against _batch_sla_xlsx workflows
    2. Schedule-type      — classify_schedule(sub_app) → DAILY/WEEKLY pe_config hours
    3. DAILY default      — pe_config.SLA_DAILY_HRS
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def compute_window_compliance(
    window_records: List[Dict[str, Any]],
    ceiling_map: Dict[str, Any],
    excluded_types: Optional[set] = None,
) -> Dict[str, Any]:
    """Compute canonical daily batch-window compliance.

    Parameters
    ----------
    window_records:
        List of dicts, each representing one day in the file.
        Required keys: ``run_date`` plus either ``elapsed_hrs`` or
        ``total_hrs``. Optional keys: ``schedule_type``, ``sla_ceil``,
        ``breach`` and ``sentinel_source``.
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
        warnings         list[str] — data-shape guardrails
    """
    try:
        from services import pe_config as _pc
        _excluded = excluded_types if excluded_types is not None else _pc.COMPLIANCE_EXCLUDED_TYPES
        _atrisk_pct = _pc.SLA_ATRISK_PCT
        _daily_default = _pc.SLA_DAILY_HRS
    except Exception:
        _excluded = {"CYCLIC", "CYCLIC_INTERVAL", "ADHOC", "CALENDAR_BASED",
                     "OUTBOUND", "PIPELINE_STAGE", "MONTHLY", "BIMONTHLY",
                     "QUARTERLY", "ANNUAL"}
        _atrisk_pct = 15.0
        _daily_default = 6.0

    daily: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    excluded_windows = 0

    for rec in window_records:
        date_str = str(rec.get("run_date") or rec.get("date") or "").strip()
        if not date_str:
            warnings.append("Skipped a window record without run_date.")
            continue

        sched = str(rec.get("schedule_type") or "").upper()

        # Skip CYCLIC/ADHOC/excluded schedule types.
        if sched in _excluded:
            excluded_windows += 1
            continue

        elapsed = float(rec.get("elapsed_hrs") or rec.get("total_hrs") or 0.0)
        ceil = rec.get("sla_ceil")
        if ceil is None:
            sub_app = str(rec.get("sub_app") or rec.get("Sub_Application") or "").upper()
            ceil = ceiling_map.get(sub_app)
        if ceil is None:
            ceil = _daily_default
        try:
            ceil_f = float(ceil)
        except (TypeError, ValueError):
            ceil_f = 0.0

        if ceil_f <= 0:
            excluded_windows += 1
            warnings.append(f"Skipped {date_str}: no usable SLA ceiling.")
            continue

        breach = rec.get("breach")
        if breach is None:
            breach = elapsed > ceil_f
        breach = bool(breach)

        current = daily.get(date_str)
        if current is None:
            daily[date_str] = {
                "run_date": date_str,
                "elapsed_hrs": elapsed,
                "sla_ceil": ceil_f,
                "breach": breach,
                "schedule_type": sched,
                "source_count": 1,
            }
        else:
            current["source_count"] += 1
            if elapsed >= float(current.get("elapsed_hrs") or 0.0):
                current["elapsed_hrs"] = elapsed
                current["sla_ceil"] = ceil_f
                current["breach"] = breach
                current["schedule_type"] = sched or str(current.get("schedule_type") or "")
            else:
                current["breach"] = bool(current.get("breach")) or breach

    duplicate_days = [day for day, row in daily.items() if int(row.get("source_count", 0)) > 1]
    if duplicate_days:
        warnings.append(
            "Duplicate run_date records were collapsed into one daily window: "
            + ", ".join(sorted(duplicate_days)[:8])
        )

    total_windows = len(daily)
    breach_count = sum(1 for row in daily.values() if bool(row.get("breach")))
    ok_count = 0
    at_risk_count = 0
    for row in daily.values():
        if row.get("breach"):
            continue
        ceil_f = float(row.get("sla_ceil") or 0.0)
        elapsed = float(row.get("elapsed_hrs") or 0.0)
        if ceil_f > 0 and ((ceil_f - elapsed) / ceil_f * 100) <= _atrisk_pct:
            at_risk_count += 1
        else:
            ok_count += 1

    compliance_pct = round((ok_count + at_risk_count) / total_windows * 100, 1) if total_windows > 0 else 0.0
    return {
        "compliance_pct":   compliance_pct,
        "breach_count":     breach_count,
        "ok_count":         ok_count,
        "at_risk_count":    at_risk_count,
        "total_windows":    total_windows,
        "excluded_windows": excluded_windows,
        "warnings":         warnings,
    }


def build_ceiling_map(
    sub_applications: List[str],
    xlsx_config: Optional[Dict[str, Any]] = None,
    pe_config_ref=None,
) -> Dict[str, float]:
    """Build a {sub_app_upper: sla_hrs} ceiling map.

    Single source of truth used by both batch_calculator.compute_metrics()
    and sla_matrix._compute_sla_matrix() so the two tabs never diverge.

    Resolution priority (highest → lowest):
        1. XLSX workflow SLA — fuzzy substring match against _batch_sla_xlsx workflows
        2. Schedule-type default — classify_schedule() → DAILY/WEEKLY pe_config hours
        3. DAILY default from pe_config

    Parameters
    ----------
    sub_applications:
        List of unique Sub_Application values from the Ctrl-M DataFrame.
    xlsx_config:
        Parsed _batch_sla_xlsx dict (from config_store). May be None when no
        BatchSLA XLSX has been uploaded — falls back to schedule-type defaults.
    pe_config_ref:
        Reference to services.pe_config module. If None, it is imported lazily.
        Passed explicitly so callers can inject a reloaded instance.

    Returns
    -------
    Dict mapping sub_app (UPPER) → contracted SLA hours (float).
    """
    if pe_config_ref is None:
        try:
            from services import pe_config as pe_config_ref  # type: ignore[assignment]
        except Exception:
            pe_config_ref = None  # type: ignore[assignment]

    # Safe schedule-type → hours lookup
    def _sched_hrs(sub_app: str) -> float:
        try:
            from services.sla_engine import classify_schedule as _cs
            stype = _cs(sub_app)
        except Exception:
            stype = "DAILY"
        defaults: Dict[str, float] = {
            "DAILY":         getattr(pe_config_ref, "SLA_DAILY_HRS",   6.0),
            "WEEKLY":        getattr(pe_config_ref, "SLA_WEEKLY_HRS",  8.0),
            "TWICE_DAILY":   getattr(pe_config_ref, "SLA_DAILY_HRS",   6.0),
            "BIWEEKLY":      getattr(pe_config_ref, "SLA_BIWEEKLY_HRS", 8.0),
            "MONTHLY":       getattr(pe_config_ref, "SLA_MONTHLY_HRS", 24.0),
            "SEQUENCING":    getattr(pe_config_ref, "SLA_DAILY_HRS",   3.0),  # shorter window
        }
        return defaults.get(stype, getattr(pe_config_ref, "SLA_DAILY_HRS", 6.0))

    # Step 1 — build XLSX pattern → sla_hrs lookup
    _xlsx_pairs: List[tuple] = []   # [(pattern_upper, sla_hrs)]
    if xlsx_config:
        for wf in xlsx_config.get("workflows") or []:
            # Accept all known field-name variants from parse_batch_sla_xlsx()
            pat = str(
                wf.get("workflow") or wf.get("sub_app_pattern") or ""
            ).upper().strip()
            sla_h = float(
                wf.get("sla_hours") or wf.get("window_sla_hrs") or wf.get("sla_hrs") or 0
            )
            if pat and sla_h > 0:
                _xlsx_pairs.append((pat, sla_h))

    ceiling_map: Dict[str, float] = {}
    for sa in sub_applications:
        sa_upper = str(sa).upper()
        # Priority 1: fuzzy substring match against XLSX workflow patterns
        matched: Optional[float] = None
        for pat, sla_h in _xlsx_pairs:
            if pat in sa_upper or sa_upper in pat:
                matched = sla_h
                break
        if matched is not None:
            ceiling_map[sa_upper] = matched
        else:
            # Priority 2 / 3: schedule-type default
            ceiling_map[sa_upper] = _sched_hrs(sa)

    return ceiling_map
