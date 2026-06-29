"""Canonical spike-record schema — the field contract for anomaly events.

Locked in BEFORE the baseline-persistence layer so the table definition, the
findings-export field contract, and the historical comparator all read ONE
shape. Every spike-append path (z-score classifier, absolute-breach classifier)
must produce a record through ``make_spike_record`` so no path can silently
omit a field and break a downstream KeyError-or-zero comparison.

FIELD CONTRACT
--------------
Guaranteed-present on every path (never None):
    start, end          ISO8601 window bounds
    peak                worst raw value in window
    peak_time           ISO8601 of the peak
    duration_min        int minutes
    severity            critical_sustained|critical|warning|notable
    reason_code         typed enum (see SEVERITY_REASON_CODES) — machine-readable
    severity_reason     human text (display only, never parsed)
    confidence          high|medium|low
    detection           z_score|absolute_threshold

Contextual (path-dependent, None when the path can't supply it):
    z_score             present on both paths today; None means "not computed"
    mean, std           baseline stats; None on paths without a baseline
    threshold           absolute band crossed; None for pure stat anomalies
    peak_pct            used-% peak; None when not applicable

Optional enrichment (frontend/findings only, default None/False):
    source_metric, aggregation, grain, formula, is_derived

PERSISTENCE — ADR-001: SQLite (WAL) over Redis for the baseline store
---------------------------------------------------------------------
Chosen: SQLite sidecar, WAL mode, customer-namespaced keys
        (customer_id:vm_id:metric). Decision date 2026-06-29.
Why:  team-scale concurrency (≤10-20 PE leads) is well inside SQLite-WAL
      capacity; no external dependency; file-based + trivially backupable;
      the store doubles as the audit-history log → repeat customers get
      trend-over-time for free. The findings export reads this same schema.
Limit: breaks under multi-instance (load-balanced) FastAPI workers. If/when
       deployment moves multi-instance, migrate to Postgres/Redis — keys are
       already namespaced and the row shape equals this contract, so it's a
       backend swap, not a schema migration. Define the table from this
       contract verbatim; change here first, never in the store.
"""
from __future__ import annotations

SEVERITY_REASON_CODES = (
    "abs_crit_sustained", "abs_crit", "abs_crit_brief", "abs_warn",
    "stat_anomaly_immaterial", "abs_sustained", "abs_breach",
)

_GUARANTEED = ("start", "end", "peak", "peak_time", "duration_min",
               "severity", "reason_code", "severity_reason", "confidence", "detection")


def make_spike_record(*, start, end, peak, peak_time, duration_min, severity,
                      reason_code, severity_reason, confidence, detection,
                      z_score=None, mean=None, std=None, threshold=None, peak_pct=None,
                      source_metric=None, aggregation=None, grain=None,
                      formula=None, is_derived=False) -> dict:
    """Build a spike record with the full key set always present, contextual
    fields defaulting to None. Keeps all append paths schema-identical."""
    return {
        "start": start, "end": end, "peak": peak, "peak_time": peak_time,
        "duration_min": duration_min, "severity": severity, "reason_code": reason_code,
        "severity_reason": severity_reason, "confidence": confidence, "detection": detection,
        "z_score": z_score, "mean": mean, "std": std, "threshold": threshold,
        "peak_pct": peak_pct, "source_metric": source_metric, "aggregation": aggregation,
        "grain": grain, "formula": formula, "is_derived": is_derived,
    }
