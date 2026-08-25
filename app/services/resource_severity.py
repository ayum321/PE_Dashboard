"""Canonical resource-metric severity and coverage rules.

This module is deliberately independent of upload format, customer name, and
UI.  Resource snapshots and Azure time-series detection call the same resolver
so a server/metric cannot be HEALTHY in the fleet summary while the anomaly
panel independently calls it CRITICAL.

Values passed to :func:`resolve_severity` are always *used percent* (higher is
worse).  Azure's ``Available Memory Percentage`` is converted once by its
adapter before it reaches this module.  The profile still exposes the raw
metric direction so renderers can label source data honestly.
"""
from __future__ import annotations

from math import isfinite
from typing import Any, Iterable, Optional


_RANK = {"unknown": -1, "healthy": 0, "notable": 0, "warning": 1, "critical": 2, "critical_sustained": 3}


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def canonical_metric(metric: str) -> str:
    """Normalize known metric labels without fuzzy schema matching."""
    label = (metric or "").strip().lower()
    if "cpu" in label or "processor" in label:
        return "cpu"
    if "mem" in label or "memory" in label:
        return "memory"
    if "disk" in label or "storage" in label or "i/o" in label or "io" in label:
        return "disk"
    return "other"


def metric_profile(metric: str, server_role: str = "APP", domain_rules: Optional[dict] = None) -> dict:
    """Return generic metric metadata plus role-aware operational bands.

    ``domain_rules`` is the intentionally small configuration seam: an
    engagement may override numbers or expected ranges, but never the
    resolution algorithm.  It uses ``{metric: {warn, crit, expected_min,
    expected_max}}`` and may additionally contain ``roles`` keyed by role.
    """
    from services import pe_config

    if domain_rules is None:
        # Settings may provide engagement-specific range profiles without a
        # code deployment. Invalid/missing configuration simply retains the
        # portable product defaults declared below.
        try:
            from services import config_store
            domain_rules = config_store.get("resource_domain_rules", {})
        except Exception:
            domain_rules = {}

    key = canonical_metric(metric)
    role = (server_role or "APP").upper()
    profiles = {
        "cpu": {
            "direction": "higher_is_worse", "raw_direction": "higher_is_worse",
            "warn": float(pe_config.CPU_WARN), "crit": float(pe_config.CPU_CRIT),
        },
        "memory": {
            # The product operates in memory-used space. Azure samples are
            # available memory, so the source direction is exposed separately.
            "direction": "higher_is_worse", "raw_direction": "lower_is_worse",
            "warn": float(pe_config.MEM_WARN), "crit": float(pe_config.MEM_CRIT),
        },
        "disk": {
            "direction": "higher_is_worse", "raw_direction": "higher_is_worse",
            "warn": float(pe_config.DISK_WARN), "crit": float(pe_config.DISK_CRIT),
        },
        "other": {
            "direction": "higher_is_worse", "raw_direction": "higher_is_worse",
            "warn": 80.0, "crit": 90.0,
        },
    }
    profile = {"metric": key, "server_role": role, **profiles[key]}

    # Product default role profiles.  They are role-aware, not customer-aware.
    if key == "cpu":
        profile.update({
            "APP": {"warn": 60.0, "crit": 80.0},
            "DB": {"warn": 85.0, "crit": 95.0},
            "SRE": {"warn": 90.0, "crit": 100.0},
        }.get(role, {}))
    elif key == "memory" and role == "DB":
        # Oracle SGA/PGA allocation is expected in this entire band.  It is
        # context, not a warning; genuine pressure starts above the band.
        profile.update({
            "expected_min": float(pe_config.DB_MEM_BAND_LOW),
            "expected_max": float(pe_config.DB_MEM_BAND_HIGH),
            "warn": float(pe_config.DB_MEM_BAND_HIGH),
            "crit": float(pe_config.DB_MEM_CRIT),
        })

    overrides = (domain_rules or {}).get(key, {}) if isinstance(domain_rules, dict) else {}
    role_overrides = (overrides.get("roles", {}) if isinstance(overrides, dict) else {}).get(role, {})
    for source in (overrides, role_overrides):
        if not isinstance(source, dict):
            continue
        for field in ("warn", "crit", "expected_min", "expected_max"):
            value = _number(source.get(field))
            if value is not None:
                profile[field] = value
    profile["invert"] = profile["raw_direction"] == "lower_is_worse"
    return profile


def resolve_severity(
    metric: str,
    value: Any,
    server_role: str = "APP",
    domain_rules: Optional[dict] = None,
    anomaly_result: Optional[dict] = None,
    duration_min: int = 0,
) -> dict:
    """Resolve one server/metric verdict from the canonical profile.

    Statistical detection can identify a point worth inspecting, but cannot
    upgrade an expected or non-material value into an operational warning.
    """
    profile = metric_profile(metric, server_role, domain_rules)
    numeric = _number(value)
    if numeric is None:
        return {**profile, "severity": "unknown", "status": "Unknown", "reason_code": "metric_not_emitted", "is_expected": False}

    expected_min, expected_max = profile.get("expected_min"), profile.get("expected_max")
    if expected_min is not None and expected_max is not None and expected_min <= numeric <= expected_max:
        return {**profile, "severity": "healthy", "status": "Healthy", "reason_code": "expected_range", "is_expected": True}

    z = _number((anomaly_result or {}).get("z")) or 0.0
    z_critical = _number((anomaly_result or {}).get("z_critical")) or 2.0
    warning, critical = float(profile["warn"]), float(profile["crit"])
    duration = max(0, int(duration_min or 0))
    base = {**profile, "is_expected": False, "value": round(numeric, 2), "z_score": round(z, 2)}

    if numeric >= critical:
        if duration > 30:
            return {**base, "severity": "critical_sustained", "status": "Critical", "reason_code": "abs_crit_sustained", "threshold": critical}
        if anomaly_result and duration < 5:
            return {**base, "severity": "warning", "status": "Warning", "reason_code": "abs_crit_brief", "threshold": critical}
        return {**base, "severity": "critical", "status": "Critical", "reason_code": "abs_crit", "threshold": critical}
    if numeric >= warning:
        return {**base, "severity": "warning", "status": "Warning", "reason_code": "abs_warn", "threshold": warning}
    if anomaly_result and z >= z_critical:
        return {**base, "severity": "notable", "status": "Healthy", "reason_code": "stat_anomaly_immaterial", "threshold": warning}
    return {**base, "severity": "healthy", "status": "Healthy", "reason_code": "within_range", "threshold": warning}


def resolve_server_severity(metrics: dict, server_role: str = "APP", domain_rules: Optional[dict] = None) -> dict:
    """Return the worst canonical result across the metrics actually emitted."""
    resolved = {
        canonical_metric(metric): resolve_severity(metric, value, server_role, domain_rules)
        for metric, value in (metrics or {}).items()
    }
    emitted = [result for result in resolved.values() if result["severity"] != "unknown"]
    if not emitted:
        return {"status": "Unknown", "severity": "unknown", "metrics": resolved, "expected_metrics": []}
    worst = max(emitted, key=lambda result: _RANK.get(result["severity"], -1))
    return {
        "status": worst["status"], "severity": worst["severity"], "metrics": resolved,
        "expected_metrics": [name for name, result in resolved.items() if result.get("is_expected")],
    }


def safe_avg(values: Iterable[Any]) -> tuple[Optional[float], float, int, int]:
    """Average valid values only and report coverage; never turn missing into 0."""
    raw = list(values or [])
    valid = [value for item in raw if (value := _number(item)) is not None]
    total = len(raw)
    coverage = (len(valid) / total) if total else 0.0
    return ((sum(valid) / len(valid)) if valid else None, coverage, len(valid), total)
