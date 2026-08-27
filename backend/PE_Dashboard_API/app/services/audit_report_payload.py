"""Versioned, evidence-only payload for the standalone PE audit report.

The dashboard engines calculate SLA, severity and fleet grade.  This module
does not repeat those algorithms; it freezes their already-calculated output
into one explicit contract that both the archive and Jinja report consume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = "2.0"
HEALTHY_STATUSES = {"healthy", "ok", "normal"}
SEVERITY_ORDER = {"critical": 4, "critical_sustained": 4, "warning": 3, "unknown": 2, "no_data": 1}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _status(value: Any) -> str:
    return _text(value).lower().replace(" ", "_")


def _server_name(server: dict[str, Any]) -> str:
    return _text(server.get("host") or server.get("hostname") or server.get("server") or server.get("name"), "Unknown host")


def _source_record(name: str, payload: Any, timestamp: str | None = None) -> dict[str, Any]:
    return {"name": name, "loaded": bool(payload), "timestamp": timestamp or None}


def _audit_window(batch: dict[str, Any]) -> dict[str, str | None]:
    coverage = _as_dict(batch.get("data_coverage"))
    dates = _as_list(coverage.get("date_range"))
    if len(dates) >= 2:
        return {"start": _text(dates[0]) or None, "end": _text(dates[1]) or None}
    window = _as_list(batch.get("window"))
    date_keys = ("run_date", "date", "day")
    resolved = [
        _text(next((row.get(key) for key in date_keys if _text(row.get(key))), ""))
        for row in window if isinstance(row, dict)
    ]
    resolved = sorted(day for day in resolved if day)
    return {"start": resolved[0] if resolved else None, "end": resolved[-1] if resolved else None}


def _resource_summary(resource: dict[str, Any], servers: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Use precomputed fleet KPIs only; never invent a grade from row values."""
    flags: list[str] = []
    kpis = _as_dict(resource.get("kpis"))
    if not kpis:
        if servers:
            flags.append("Resource rows exist but the upstream fleet KPI payload is absent; grade and sign-off are unavailable.")
        return None, flags
    total = int(_number(kpis.get("total_servers")) or len(servers))
    critical = int(_number(kpis.get("n_critical")) or 0)
    warning = int(_number(kpis.get("n_warning")) or 0)
    no_data = int(_number(kpis.get("n_no_data")) or 0)
    # `n_healthy` is already resolved by the resource engine.  Do not derive
    # it from the other counts: the engine can distinguish image-only/no-data
    # rows and future states without this report silently changing the result.
    resolved_healthy = _number(kpis.get("n_healthy"))
    healthy = int(resolved_healthy) if resolved_healthy is not None else max(0, total - critical - warning - no_data)
    return {
        "grade": _text(kpis.get("fleet_grade"), "N/A"),
        "score": _number(kpis.get("fleet_score")),
        "servers_total": total,
        "critical": critical,
        "warning": warning,
        "ok": healthy,
        "no_data": no_data,
        "source": "resource.kpis",
    }, flags


def _exception_table(servers: list[dict[str, Any]], resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter only preclassified rows. z-score is supplemental evidence, not a new severity classifier."""
    spikes_by_host: dict[str, float] = {}
    deep = _as_dict(resource.get("deep_dive"))
    for host, detail in _as_dict(deep.get("vms")).items():
        z_values: list[float] = []
        for events in _as_dict(detail).get("spikes", {}).values():
            for event in _as_list(events):
                z = _number(_as_dict(event).get("z_score") or _as_dict(event).get("z"))
                if z is not None:
                    z_values.append(abs(z))
        if z_values:
            spikes_by_host[str(host).lower()] = max(z_values)

    rows: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        host = _server_name(server)
        status = _status(server.get("status") or server.get("health"))
        z_score = spikes_by_host.get(host.lower())
        is_exception = status not in HEALTHY_STATUSES or (z_score is not None and z_score >= 2.0)
        if not is_exception:
            continue
        reason = "health status is not HEALTHY" if status not in HEALTHY_STATUSES else "z-score is at least 2.0"
        rows.append({
            "host": host,
            "role": _text(server.get("type") or server.get("role"), "Unknown"),
            "environment": _text(server.get("environment") or server.get("env"), "Unknown"),
            "status": status.upper() or "UNKNOWN",
            "cpu_pct": _number(server.get("cpu_used") or server.get("cpu_avg")),
            "memory_pct": _number(server.get("mem_used") or server.get("memory_used") or server.get("memory_available_pct")),
            "disk_pct": _number(server.get("disk_used_max") or server.get("disk_used") or server.get("disk_pct")),
            "max_anomaly_z": z_score,
            "exception_reason": reason,
        })
    return sorted(rows, key=lambda row: (-SEVERITY_ORDER.get(row["status"].lower(), 0), row["host"]))


def _sow_interpretation(metrics: list[dict[str, Any]]) -> str:
    statuses = {_status(metric.get("status")) for metric in metrics}
    if any(status in {"over", "critical_over"} for status in statuses):
        return "commercial_review"
    if "low" in statuses:
        return "testing_coverage_risk"
    return "none"


def _priority_actions(batch: dict[str, Any], exceptions: list[dict[str, Any]], sow_metrics: list[dict[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    kpis = _as_dict(batch.get("kpis"))
    breach_count = int(_number(kpis.get("jobs_breach") or kpis.get("breach_count")) or 0)
    if breach_count:
        actions.append({"id": "batch-breach", "priority": "P1", "source_type": "generated", "observation": f"{breach_count} batch SLA breach(es) were reported.", "likely_cause": "Batch schedule, dependency, or runtime regression.", "recommended_action": "Investigate the breached day and its longest contributing jobs.", "status": "open"})
    for exception in exceptions[:2]:
        actions.append({"id": f"resource-{exception['host']}", "priority": "P1" if exception["status"] == "CRITICAL" else "P2", "source_type": "generated", "observation": f"{exception['host']} is {exception['status']} ({exception['exception_reason']}).", "likely_cause": "Validate capacity, workload, and concurrent batch activity.", "recommended_action": f"Review {exception['host']} time-series and owning workload.", "status": "open"})
    if _sow_interpretation(sow_metrics) != "none":
        actions.append({"id": "sow-capacity", "priority": "P3", "source_type": "generated", "observation": "SOW volume is outside the expected contract operating band.", "likely_cause": "Observed workload does not represent the contracted operating volume.", "recommended_action": "Confirm expected ramp and validate the performance conclusion at representative volume.", "status": "open"})
    for issue in issues:
        if not isinstance(issue, dict) or _status(issue.get("status")) in {"resolved", "closed"}:
            continue
        actions.append({"id": _text(issue.get("ID"), f"issue-{len(actions)+1}"), "priority": _text(issue.get("Severity"), "P3").upper(), "source_type": "manually_logged", "observation": _text(issue.get("Description"), "Open issue logged by the reviewer."), "likely_cause": "Manual issue register entry.", "recommended_action": _text(issue.get("Mitigation"), "Confirm owner and closure evidence."), "status": _text(issue.get("Status"), "open")})
    return actions[:12]


def _current_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """Small, display-ready snapshot used solely for prior-audit deltas.

    Values are read from the already-computed payload.  This deliberately does
    not calculate a new verdict, grade, or severity during report rendering.
    """
    batch = _as_dict(payload.get("batch_sla"))
    summary = _as_dict(batch.get("buffer_summary"))
    resource = _as_dict(payload.get("resource_review"))
    fleet = _as_dict(resource.get("fleet_summary"))
    top_jobs = _as_list(batch.get("top_jobs_table"))
    peak_values = [_number(_as_dict(row).get("peak_hrs") or _as_dict(row).get("peak")) for row in top_jobs]
    return {
        "compliance_pct": _number(summary.get("compliance_pct") or summary.get("window_compliance_pct")),
        "breach_count": int(_number(summary.get("jobs_breach") or summary.get("breach_count")) or 0),
        "worst_long_pole_hrs": max((value for value in peak_values if value is not None), default=None),
        "fleet_grade": _text(fleet.get("grade"), "N/A"),
        "anomaly_count": len(_as_list(payload.get("correlation_rca"))),
        "missing_coverage": len(_as_list(_as_dict(payload.get("meta")).get("missing_metrics"))),
        "sow_interpretation": _text(_as_dict(payload.get("sow_capacity")).get("interpretation"), "none"),
    }


def attach_prior_audit(payload: dict[str, Any], prior_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Attach a chronological prior-audit reference and non-verdict deltas.

    Snapshot identity is the immutable audit ID.  Calendar dates remain report
    metadata, because a customer can legitimately have more than one audit of
    the same calendar period.
    """
    if not prior_payload:
        payload["prior_audit_ref"] = None
        payload["deltas"] = {"state": "first_audit", "message": "First archived audit for this customer; no prior baseline is available."}
        return payload
    current = _current_metrics(payload)
    prior = _current_metrics(prior_payload)
    numeric_keys = ("compliance_pct", "breach_count", "worst_long_pole_hrs", "anomaly_count", "missing_coverage")
    deltas: dict[str, Any] = {}
    for key in numeric_keys:
        now_value, before_value = current.get(key), prior.get(key)
        deltas[key] = None if now_value is None or before_value is None else round(float(now_value) - float(before_value), 3)
    deltas.update({
        "fleet_grade": {"current": current["fleet_grade"], "prior": prior["fleet_grade"]},
        "sow_interpretation": {"current": current["sow_interpretation"], "prior": prior["sow_interpretation"]},
    })
    prior_meta = _as_dict(prior_payload.get("meta"))
    payload["prior_audit_ref"] = {
        "audit_id": _text(prior_meta.get("audit_id"), "Unknown"),
        "generated_at": prior_meta.get("generated_at"),
        "audit_window": _as_dict(prior_meta.get("audit_window")),
    }
    payload["deltas"] = deltas
    return payload


def build_audit_report_payload(body: dict[str, Any], *, audit_id: str | None = None) -> dict[str, Any]:
    """Build the immutable export contract from precomputed dashboard payloads."""
    batch = _as_dict(body.get("batch"))
    resource = _as_dict(body.get("resource"))
    sow = _as_dict(body.get("sow"))
    benchmark = _as_dict(body.get("benchmark"))
    approvals = _as_dict(body.get("approvals"))
    servers = [row for row in _as_list(body.get("servers")) or _as_list(resource.get("servers")) if isinstance(row, dict)]
    issues = [row for row in _as_list(body.get("issues")) if isinstance(row, dict)]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    customer = _text(approvals.get("customer_name") or body.get("customer_name"), "Customer not specified")
    resolved_id = _text(audit_id or body.get("audit_id") or _as_dict(body.get("meta")).get("audit_id"))
    if not resolved_id:
        resolved_id = f"AUD-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8].upper()}"
    resource_summary, quality_flags = _resource_summary(resource, servers)
    exceptions = _exception_table(servers, resource)
    sow_metrics = [row for row in _as_list(sow.get("metrics")) if isinstance(row, dict)]
    pe = _as_dict(approvals.get("pe"))
    customer_approval = _as_dict(approvals.get("customer"))
    requested_customer_approval = bool(customer_approval.get("approved"))
    requested_pe_review = bool(pe.get("approved"))
    if requested_customer_approval and resource_summary is None:
        quality_flags.append("Customer-approved sign-off was blocked because the resource fleet summary is missing.")
    sign_off = "customer_approved" if requested_customer_approval and requested_pe_review and resource_summary is not None else ("reviewed" if requested_pe_review else "draft")
    batch_kpis = _as_dict(batch.get("kpis"))
    final = _as_dict(body.get("final_judgment") or body.get("finalJudgment"))
    headline = _text(final.get("verdict_line") or final.get("summary") or final.get("narrative"))
    if not headline:
        headline = "Evidence has been frozen from the loaded dashboard panels; see the section-level findings and data-quality flags."
    deep_dive = _as_dict(resource.get("deep_dive"))
    correlation_events = _as_list(deep_dive.get("patterns"))
    if correlation_events and not any(_number(_as_dict(event).get("confidence_pct")) is not None for event in correlation_events):
        quality_flags.append(
            "Correlation events are present without numeric confidence. They remain evidence of time overlap only and are not chart-ranked."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "customer": customer, "audit_id": resolved_id, "audit_window": _audit_window(batch), "generated_at": now,
            "engine_version": _text(body.get("engine_version"), "PE Dashboard export 2.0"),
            "sources": [_source_record("Ctrl-M batch", batch), _source_record("Azure Monitor", resource), _source_record("SOW volume", sow), _source_record("Benchmark", benchmark)],
            "data_coverage_pct": _number(_as_dict(batch.get("data_coverage")).get("confidence")),
            "missing_metrics": ["resource fleet summary"] if resource_summary is None else [],
            "sign_off_status": sign_off,
        },
        "executive_verdict": {"headline": headline, "confidence_pct": _number(final.get("confidence_pct") or batch_kpis.get("confidence")), "data_quality_flags": quality_flags},
        "priority_actions": _priority_actions(batch, exceptions, sow_metrics, issues),
        "batch_sla": {
            "window_chart_series": _as_list(batch.get("window")), "breach_days": [row for row in _as_list(batch.get("window")) if _status(_as_dict(row).get("status")) in {"breach", "failed"}],
            "tight_days": [row for row in _as_list(batch.get("window")) if _status(_as_dict(row).get("status")) in {"at_risk", "long_job", "tight"}],
            "buffer_summary": batch_kpis, "long_pole_trend_series": _as_dict(batch.get("longpole_matrix")),
            "top_jobs_table": _as_list(batch.get("top_jobs") or batch.get("top_breaches")), "excluded_jobs": _as_list(_as_dict(batch.get("data_coverage")).get("excluded_jobs")),
        },
        "resource_review": {
            "fleet_summary": resource_summary, "exception_rule": "status != HEALTHY, with z-score >= 2.0 included as additional anomaly evidence", "exception_table": exceptions,
            "fleet_heatmap_series": _as_dict(deep_dive.get("heatmap")), "timeseries_by_host": _as_dict(deep_dive.get("vms")),
            "all_servers_table": servers,
            "unit_semantics": {"cpu": "CPU utilisation % (higher = more pressure)", "memory": "available memory % (lower = more pressure)", "disk": "disk bandwidth consumed % (higher = more pressure)"},
        },
        "correlation_rca": correlation_events,
        "sow_capacity": {"metrics": sow_metrics, "interpretation": _sow_interpretation(sow_metrics)},
        "methodology": {"job_sla_def": "Job SLA measures each job run against its resolved ceiling.", "window_sla_def": "Window SLA measures each daily effective batch window against its resolved ceiling.", "buffer_formula": "buffer_pct = (SLA hours - runtime hours) / SLA hours * 100", "status_bands": {"OK": ">40% buffer", "LONG_JOB": "15-40% buffer", "AT_RISK": "0-15% buffer", "BREACH": "<=0% buffer"}, "exclusion_rules": _as_list(_as_dict(batch.get("data_coverage")).get("excluded_jobs")), "source_badges": [source["name"] for source in [_source_record("Ctrl-M batch", batch), _source_record("Azure Monitor", resource), _source_record("SOW volume", sow)] if source["loaded"]]},
        "prior_audit_ref": None,
        "deltas": None,
        "validation": {"errors": [], "warnings": quality_flags},
    }
