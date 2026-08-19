"""
Shared TypedDict schemas for the two digest shapes used in the PE pipeline.

NarrativeContext  — produced by pe_narrative._build_narrative_context()
                    consumed by pe_narrative._deterministic_fallback()

ConsultantDigest  — produced by pe_consultant._build_consultant_digest()
                    consumed by pe_consultant._consultant_llm()

Keeping these in a single module prevents the easy mistake of passing the
wrong digest shape to the wrong function — both are called "digest" historically
but have fundamentally different key layouts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
try:
    from typing import TypedDict
except ImportError:          # Python 3.7 fallback
    from typing_extensions import TypedDict


# ── NarrativeContext ──────────────────────────────────────────────────────────
# Shape returned by pe_narrative._build_narrative_context()
# Top-level keys match the 4 report sections: batch, sla_matrix, resource, uat

class _BatchContext(TypedDict, total=False):
    kpis:         Dict[str, Any]   # BatchKPIs dict from session_cache.batch_kpis
    top_jobs:     List[Dict]
    top_breaches: List[Dict]
    sub_stats:    List[Dict]
    window:       List[Dict]       # daily_window_series entries
    anomalies:    List[Dict]
    data_coverage: Optional[Dict]


class _SlaMatrixContext(TypedDict, total=False):
    kpis:         Dict[str, Any]   # sla_matrix_kpis dict
    job_summary:  List[Dict]
    breaches:     List[Dict]
    adaptive_sla: List[Dict]
    outliers:     List[Dict]


class _ResourceContext(TypedDict, total=False):
    kpis:    Optional[Dict[str, Any]]
    servers: List[Dict]


class NarrativeContext(TypedDict, total=False):
    """Output of pe_narrative._build_narrative_context().

    Consumed ONLY by pe_narrative._deterministic_fallback() and
    pe_narrative._validate_and_merge().  Never pass to pe_consultant functions.
    """
    batch:          _BatchContext
    sla_matrix:     _SlaMatrixContext
    resource:       _ResourceContext
    sow_compare:    Dict[str, Any]
    sow_contract:   Dict[str, Any]
    volume_vs_sow:  Dict[str, Any]
    uat:            List[Dict]
    sla_intel:      Dict[str, Any]
    smart_findings: Dict[str, Any]
    benchmark:      Dict[str, Any]
    red_flags:      Dict[str, Any]
    customer_name:  str


# ── ConsultantDigest ──────────────────────────────────────────────────────────
# Shape returned by pe_consultant._build_consultant_digest()
# Flatter structure — all pillar outputs collapsed into a single scoring dict

class _SlaDigest(TypedDict, total=False):
    mode:                   Optional[str]
    limit_hrs:              Optional[float]
    compliance_pct:         Optional[float]
    run_sla_compliance_pct: Optional[float]
    window_compliance_pct:  Optional[float]
    batch_window_compliance: Optional[float]
    breach:                 Optional[int]
    at_risk:                Optional[int]
    ok:                     Optional[int]
    worst_job:              Optional[str]
    worst_hrs:              Optional[float]
    worst_margin:           Optional[float]
    top_breaches:           List[Dict]
    workflow_breaches:      List[Dict]
    workflow_count:         Optional[int]


class _FleetDigest(TypedDict, total=False):
    grade:           Optional[str]
    score:           Optional[float]
    n_critical:      Optional[int]
    n_warning:       Optional[int]
    total:           Optional[int]
    known_pct:       Optional[float]
    top_stressed:    List[Dict]


class ConsultantDigest(TypedDict, total=False):
    """Output of pe_consultant._build_consultant_digest().

    Consumed ONLY by pe_consultant._consultant_llm() and related scoring
    functions.  Never pass to pe_narrative functions.
    """
    customer:          str
    sla:               _SlaDigest
    findings_summary:  Dict[str, Any]   # {critical, warning, total}
    top_findings:      List[Dict]
    red_flags_by_risk: Dict[str, int]   # {CRITICAL: n, HIGH: n, ...}
    top_red_flags:     List[Dict]
    cross_links:       List[Dict]
    batch_kpis:        Dict[str, Any]
    batch_window:      Dict[str, Any]   # {breach_days, breach_day_count}
    anomalies:         Dict[str, Any]   # {count, top}
    fleet:             _FleetDigest
