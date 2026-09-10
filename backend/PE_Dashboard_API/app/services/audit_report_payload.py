"""Versioned, evidence-only payload for the standalone PE audit report.

The dashboard engines calculate SLA, severity and fleet grade.  This module
does not repeat those algorithms; it freezes their already-calculated output
into one explicit contract that both the archive and Jinja report consume.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from services import pe_config


SCHEMA_VERSION = "2.0"
HEALTHY_STATUSES = {"healthy", "ok", "normal"}
SEVERITY_ORDER = {"critical": 4, "critical_sustained": 4, "warning": 3, "unknown": 2, "no_data": 1}
SCORED_SLA_STATUSES = {"OK", "LONG_JOB", "AT_RISK", "BREACH"}
UNRESOLVED_SLA_STATUSES = {"MEASUREMENT_UNRESOLVED", "NOT_OBSERVED", "RUNTIME_MISSING", "SLA_CONTRACT_CONFLICT"}
CLOSED_ISSUE_STATUSES = {"resolved", "closed"}


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


def _first_present(*values: Any) -> Any:
    """Return the first supplied value while preserving legitimate zeroes."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalise_job_row(value: Any) -> dict[str, Any]:
    """Freeze legacy and current batch fields into one display contract.

    The batch engine has already calculated buffer and status.  This function
    only carries those values forward; it must never derive a replacement
    status in the export layer.
    """
    row = _as_dict(value)
    return {
        "job_name": _text(_first_present(row.get("job_name"), row.get("job"), row.get("Job_Name")), "Unknown job"),
        "sub_app": _text(_first_present(row.get("sub_app"), row.get("sub_application"), row.get("Sub_Application"), row.get("Sub_App")), "—"),
        "schedule_type": _text(_first_present(row.get("schedule_type"), row.get("schedule"), row.get("Schedule")), "Unclassified"),
        "peak_hrs": _number(_first_present(row.get("peak_hrs"), row.get("peak_runtime"), row.get("peak"))),
        "avg_hrs": _number(_first_present(row.get("avg_hrs"), row.get("average_hrs"), row.get("avg_runtime"), row.get("average"))),
        "sla_hrs": _number(_first_present(row.get("sla_hrs"), row.get("sla"), row.get("SLA"))),
        "buffer_pct": _number(_first_present(row.get("buffer_pct"), row.get("buffer"))),
        "sla_used_pct": _number(_first_present(row.get("sla_used_pct"), row.get("sla_used"))),
        "status": _text(_first_present(row.get("buffer_status"), row.get("status"), row.get("Status")), "NOT_ASSESSED").upper(),
        "sla_source": _text(_first_present(row.get("sla_source"), row.get("source")), "Not supplied"),
    }


def _normalise_workflow_row(value: Any) -> dict[str, Any]:
    """Freeze a canonical workflow result without recalculating it.

    In particular, explicit null SLA/buffer values are evidence.  Replacing
    them with a global default in the report would turn an unresolved or
    undeclared contract into a false pass/fail result.
    """
    row = _as_dict(value)
    status = _text(row.get("status"), "NOT_ASSESSED").upper()
    return {
        "workflow_key": _text(row.get("workflow_key")),
        "workflow_name": _text(_first_present(row.get("workflow_name"), row.get("sub_application")), "Unknown workflow"),
        "sub_application": _text(_first_present(row.get("sub_application"), row.get("workflow_name")), "—"),
        "batch_type": _text(row.get("batch_type"), "UNKNOWN").upper(),
        "tier": _text(row.get("tier"), "UNKNOWN").upper(),
        "sla_h": _number(row.get("sla_h")),
        "runtime_h": _number(_first_present(row.get("runtime_h"), row.get("elapsed_duration_h"))),
        "buffer_pct": _number(row.get("buffer_pct")),
        "duration_headroom_h": _number(row.get("duration_headroom_h")),
        "duration_headroom_mins": _number(row.get("duration_headroom_mins")),
        "indicative_buffer_pct": _number(row.get("indicative_buffer_pct")),
        "indicative_duration_headroom_h": _number(row.get("indicative_duration_headroom_h")),
        "indicative_clock_buffer_mins": _number(row.get("indicative_clock_buffer_mins")),
        "status": status,
        "status_eligible": bool(row.get("status_eligible")) and status in SCORED_SLA_STATUSES,
        "measurement_state": _text(row.get("measurement_state"), "UNKNOWN").upper(),
        "measurement_reason_code": _text(row.get("measurement_reason_code")),
        "measurement_reason_detail": _text(row.get("measurement_reason_detail")),
        "sla_measurement_basis": _text(row.get("sla_measurement_basis")),
        "sla_source": _text(row.get("sla_source"), "Not supplied"),
        "first_job_anchor": _text(row.get("first_job_anchor")),
        "last_job_anchor": _text(row.get("last_job_anchor")),
        "anchor_used": bool(row.get("anchor_used")),
        "actual_start_time": row.get("actual_start_time"),
        "actual_end_time": row.get("actual_end_time"),
        "total_runs": int(_number(row.get("total_runs")) or 0),
        "job_count": int(_number(row.get("job_count")) or 0),
    }


def _workflow_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [row for row in rows if row["status_eligible"]]
    breach_count = sum(row["status"] == "BREACH" for row in scored)
    unresolved = [
        row for row in rows
        if row["status"] in UNRESOLVED_SLA_STATUSES
        or row["measurement_state"] in {"ANCHOR_UNMATCHED", "NOT_OBSERVED"}
    ]
    observed = [row for row in rows if row["measurement_state"] != "NOT_OBSERVED"]
    no_sla = [row for row in rows if row["status"] in {"SLA_UNDECLARED", "NO_SLA"}]
    return {
        "inventory_count": len(rows),
        "observed_count": len(observed),
        "not_observed_count": len(rows) - len(observed),
        "scored_count": len(scored),
        "unscored_count": len(rows) - len(scored),
        "breach_count": breach_count,
        "at_risk_count": sum(row["status"] == "AT_RISK" for row in scored),
        "long_job_count": sum(row["status"] == "LONG_JOB" for row in scored),
        "ok_count": sum(row["status"] == "OK" for row in scored),
        "unresolved_count": len(unresolved),
        "no_sla_count": len(no_sla),
        "compliance_pct": round((len(scored) - breach_count) / len(scored) * 100, 1) if scored else None,
        "source": "sla_matrix.workflow_summary",
    }


def _normalise_sow_metric(value: Any) -> dict[str, Any]:
    """Freeze source fields such as ``sow``/``pct`` into report field names."""
    row = _as_dict(value)
    return {
        "name": _text(_first_present(row.get("name"), row.get("label"), row.get("key")), "Metric"),
        "commitment": _number(_first_present(row.get("commitment"), row.get("target"), row.get("sow_target"), row.get("sow"))),
        "actual": _number(_first_present(row.get("actual"), row.get("observed"), row.get("value"))),
        "pct_of_contract": _number(_first_present(row.get("pct_of_contract"), row.get("achievement_pct"), row.get("pct"))),
        "capacity_buffer": _number(_first_present(row.get("capacity_buffer"), row.get("headroom"))),
        "status": _text(_first_present(row.get("status"), row.get("Status")), "NOT_ASSESSED").upper(),
    }


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
                z = _number(_first_present(_as_dict(event).get("z_score"), _as_dict(event).get("z")))
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
            "cpu_pct": _number(_first_present(server.get("cpu_used"), server.get("cpu_avg"))),
            "memory_used_pct": _number(_first_present(server.get("mem_used"), server.get("memory_used"))),
            "memory_available_pct": _number(server.get("memory_available_pct")),
            "memory_pct": _number(_first_present(server.get("mem_used"), server.get("memory_used"))),
            "disk_pct": _number(_first_present(server.get("disk_used_max"), server.get("disk_used"), server.get("disk_pct"))),
            "max_anomaly_z": z_score,
            "exception_reason": reason,
        })
    return sorted(rows, key=lambda row: (-SEVERITY_ORDER.get(row["status"].lower(), 0), row["host"]))


def _detail_for_host(timeseries: dict[str, Any], host: str) -> dict[str, Any]:
    """Resolve Azure host keys case-insensitively without altering evidence."""
    for source_host, detail in timeseries.items():
        if str(source_host).lower() == host.lower():
            return _as_dict(detail)
    return {}


def _server_evidence(servers: list[dict[str, Any]], resource: dict[str, Any], correlations: list[Any]) -> list[dict[str, Any]]:
    """Create compact, source-backed host records for the report.

    Spike labels and correlation notes are copied from the deep-dive payload.
    A host with no series is retained and explicitly marked rather than being
    silently omitted from its DB/APP/SRE group.
    """
    deep = _as_dict(resource.get("deep_dive"))
    timeseries = _as_dict(deep.get("vms"))
    result: list[dict[str, Any]] = []
    for server in servers:
        host = _server_name(server)
        detail = _detail_for_host(timeseries, host)
        spike_entries: list[tuple[str, dict[str, Any]]] = []
        for metric, events in _as_dict(detail.get("spikes")).items():
            for event in _as_list(events):
                if isinstance(event, dict):
                    spike_entries.append((str(metric), event))
        spike_entries.sort(key=lambda item: abs(_number(_first_present(item[1].get("z_score"), item[1].get("z"))) or 0), reverse=True)
        spike_note = "No detected spike was supplied for this host."
        if spike_entries:
            metric, event = spike_entries[0]
            severity = _text(_first_present(event.get("severity"), event.get("status"), event.get("pattern")), "detected spike")
            peak = _first_present(event.get("peak"), event.get("value"), event.get("v"))
            direction = "low-water" if metric == "Available Memory Percentage" else "peak"
            suffix = f"; {direction} {peak}" if peak is not None else ""
            spike_note = f"{severity} — {metric}{suffix}."
        correlation_note = "No Ctrl-M time-overlap evidence was supplied for this host."
        for raw_event in correlations:
            event = _as_dict(raw_event)
            event_hosts = _as_list(event.get("hosts") or event.get("servers") or event.get("vms"))
            if not any(str(candidate).lower() == host.lower() for candidate in event_hosts):
                continue
            jobs = _as_list(event.get("correlated_jobs") or event.get("jobs"))
            job_names = [str(_as_dict(job).get("job") or _as_dict(job).get("name") or job) for job in jobs][:3]
            evidence = ", ".join(job_names) or _text(event.get("title") or event.get("insight"), "concurrent batch activity")
            correlation_note = f"Ctrl-M overlap: {evidence} — time overlap only, not proof of cause."
            break
        result.append({
            "host": host,
            "role": _text(server.get("type") or server.get("role"), "Other").upper(),
            "environment": _text(server.get("environment") or server.get("env"), "Unknown").upper(),
            "status": _text(server.get("status") or server.get("health"), "UNKNOWN").upper(),
            "cpu_pct": _number(_first_present(server.get("cpu_used"), server.get("cpu_avg"))),
            "memory_used_pct": _number(_first_present(server.get("mem_used"), server.get("memory_used"))),
            "memory_available_pct": _number(server.get("memory_available_pct")),
            "memory_pct": _number(_first_present(server.get("mem_used"), server.get("memory_used"))),
            "disk_pct": _number(_first_present(server.get("disk_used_max"), server.get("disk_used"), server.get("disk_pct"))),
            "has_timeseries": bool(_as_dict(detail).get("series")),
            "spike_note": spike_note,
            "correlation_note": correlation_note,
        })
    return sorted(result, key=lambda row: ({"DB": 0, "APP": 1, "SRE": 2}.get(row["role"], 9), row["host"]))


def _sow_interpretation(metrics: list[dict[str, Any]]) -> str:
    statuses = {_status(metric.get("status")) for metric in metrics}
    if any(status in {"over", "critical_over"} for status in statuses):
        return "commercial_review"
    if "low" in statuses:
        return "testing_coverage_risk"
    return "none"


def _priority_actions(batch: dict[str, Any], exceptions: list[dict[str, Any]], sow_metrics: list[dict[str, Any]], issues: list[dict[str, Any]], workflow: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    kpis = _as_dict(batch.get("kpis"))
    workflow_counts = _as_dict(workflow.get("summary"))
    has_workflow_inventory = bool(_as_list(workflow.get("rows")))
    breach_source = workflow_counts.get("breach_count") if has_workflow_inventory else _first_present(kpis.get("jobs_breach"), kpis.get("breach_count"))
    breach_count = int(_number(breach_source) or 0)
    if breach_count:
        actions.append({"id": "batch-breach", "priority": "P1", "source_type": "generated", "observation": f"{breach_count} eligible workflow SLA breach(es) were reported.", "likely_cause": "Batch schedule, dependency, or runtime regression.", "recommended_action": "Investigate the breached execution and its contributing jobs.", "status": "open"})
    unresolved_count = int(_number(workflow_counts.get("unresolved_count")) or 0) if has_workflow_inventory else 0
    if unresolved_count:
        actions.append({"id": "sla-measurement-unresolved", "priority": "P1", "source_type": "generated", "observation": f"{unresolved_count} contracted workflow measurement(s) are unresolved or not observed.", "likely_cause": "Configured sentinel alignment or Control-M coverage is incomplete.", "recommended_action": "Align sentinel names or approve a documented fallback policy before scoring these workflows.", "status": "open"})
    no_sla_count = int(_number(workflow_counts.get("no_sla_count")) or 0) if has_workflow_inventory else 0
    if no_sla_count:
        actions.append({"id": "sla-contract-coverage", "priority": "P2", "source_type": "generated", "observation": f"{no_sla_count} observed workflow(s) have no declared SLA.", "likely_cause": "The workbook explicitly states No SLA or omits a contractual ceiling.", "recommended_action": "Confirm whether each workflow is intentionally non-contractual and record the approved policy.", "status": "open"})
    for exception in exceptions[:2]:
        actions.append({"id": f"resource-{exception['host']}", "priority": "P1" if exception["status"] == "CRITICAL" else "P2", "source_type": "generated", "observation": f"{exception['host']} is {exception['status']} ({exception['exception_reason']}).", "likely_cause": "Validate capacity, workload, and concurrent batch activity.", "recommended_action": f"Review {exception['host']} time-series and owning workload.", "status": "open"})
    if _sow_interpretation(sow_metrics) != "none":
        actions.append({"id": "sow-capacity", "priority": "P3", "source_type": "generated", "observation": "SOW volume is outside the expected contract operating band.", "likely_cause": "Observed workload does not represent the contracted operating volume.", "recommended_action": "Confirm expected ramp and validate the performance conclusion at representative volume.", "status": "open"})
    for issue in issues:
        if not isinstance(issue, dict) or _status(issue.get("status")) in {"resolved", "closed"}:
            continue
        actions.append({"id": _text(issue.get("ID"), f"issue-{len(actions)+1}"), "priority": _text(issue.get("Severity"), "P3").upper(), "source_type": "manually_logged", "observation": _text(issue.get("Description"), "Open issue logged by the reviewer."), "likely_cause": "Manual issue register entry.", "recommended_action": _text(issue.get("Mitigation"), "Confirm owner and closure evidence."), "status": _text(issue.get("Status"), "open")})
    return actions[:12]


def _date_present(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _actionable_findings(body: dict[str, Any]) -> list[dict[str, Any]]:
    raw: list[Any] = []
    for key in ("findings", "red_flags"):
        value = body.get(key)
        if isinstance(value, list):
            raw.extend(value)
        elif isinstance(value, dict):
            raw.extend(_as_list(value.get("items") or value.get("findings") or value.get("red_flags")))
    result: list[dict[str, Any]] = []
    for item in raw:
        row = _as_dict(item)
        if not row or _status(row.get("status")) in CLOSED_ISSUE_STATUSES:
            continue
        severity = _status(_first_present(row.get("severity"), row.get("priority"), row.get("level")))
        if severity in {"critical", "high", "p0", "p1"}:
            result.append(row)
    return result


def _checklist_mismatches(approvals: dict[str, Any], *, batch: dict[str, Any], resource: dict[str, Any], sow: dict[str, Any], benchmark: dict[str, Any]) -> list[str]:
    checklist = _as_dict(approvals.get("checklist"))
    evidence = {
        "batch": bool(batch), "ctrlm": bool(batch),
        "ui": bool(benchmark), "perf": bool(benchmark),
        "res": bool(resource), "res15": bool(_as_dict(_as_dict(resource.get("deep_dive")).get("vms"))),
        "sow": bool(sow), "data": bool(sow),
        # An empty issue register can legitimately be reviewed and accepted.
        "issues": True,
    }
    return [key for key, claimed in checklist.items() if bool(claimed) and not evidence.get(key, False)]


def _sign_off_state(*, approvals: dict[str, Any], resource_summary: dict[str, Any] | None,
                    workflow: dict[str, Any], issues: list[dict[str, Any]],
                    critical_findings: list[dict[str, Any]], checklist_mismatches: list[str]) -> tuple[str, list[str]]:
    pe = _as_dict(approvals.get("pe"))
    customer = _as_dict(approvals.get("customer"))
    pe_requested, customer_requested = bool(pe.get("approved")), bool(customer.get("approved"))
    blockers: list[str] = []
    checklist = _as_dict(approvals.get("checklist"))
    if not checklist or not all(bool(value) for value in checklist.values()):
        blockers.append("The PE validation checklist is incomplete.")
    if checklist_mismatches:
        blockers.append("Checklist claims lack loaded evidence: " + ", ".join(sorted(checklist_mismatches)) + ".")
    if pe_requested and (not _text(pe.get("name")) or not _date_present(pe.get("date"))):
        blockers.append("PE reviewer name and valid sign-off date are required.")
    if customer_requested and (not _text(customer.get("name")) or not _date_present(customer.get("date"))):
        blockers.append("Customer approver name and valid sign-off date are required.")
    if resource_summary is None:
        blockers.append("The resource fleet summary is missing.")
    elif int(_number(resource_summary.get("critical")) or 0) > 0:
        blockers.append("Critical resource findings remain open.")
    counts = _as_dict(workflow.get("summary"))
    if int(_number(counts.get("breach_count")) or 0) > 0:
        blockers.append("Eligible workflow SLA breaches remain open.")
    if int(_number(counts.get("unresolved_count")) or 0) > 0:
        blockers.append("Contracted workflow measurements remain unresolved or not observed.")
    open_issues = [row for row in issues if _status(row.get("Status") or row.get("status")) not in CLOSED_ISSUE_STATUSES]
    if open_issues:
        blockers.append("The manual issue register contains open or excepted items.")
    if critical_findings:
        blockers.append("Critical or high PE findings remain open.")
    blockers = list(dict.fromkeys(blockers))
    if pe_requested and customer_requested:
        return ("approved_with_exceptions" if blockers else "clean_approved"), blockers
    if pe_requested:
        return "reviewed_with_exceptions" if blockers else "reviewed", blockers
    return "draft", blockers


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
    sla_matrix = _as_dict(body.get("sla_matrix"))
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
    workflow_rows = [_normalise_workflow_row(row) for row in _as_list(sla_matrix.get("workflow_summary")) if isinstance(row, dict)]
    workflow = {"rows": workflow_rows, "summary": _workflow_summary(workflow_rows), "source": "sla_matrix.workflow_summary"}
    sow_metrics = [_normalise_sow_metric(row) for row in _as_list(sow.get("metrics")) if isinstance(row, dict)]
    pe = _as_dict(approvals.get("pe"))
    customer_approval = _as_dict(approvals.get("customer"))
    requested_customer_approval = bool(customer_approval.get("approved"))
    requested_pe_review = bool(pe.get("approved"))
    pe_name = _text(pe.get("name") or approvals.get("pe_name") or body.get("pe_name"))
    cust_name = _text(customer_approval.get("name") or approvals.get("cust_name") or body.get("cust_name"))
    env = _text(approvals.get("env_type") or body.get("env_type") or body.get("env"))
    checklist_mismatches = _checklist_mismatches(approvals, batch=batch, resource=resource, sow=sow, benchmark=benchmark)
    critical_findings = _actionable_findings(body)
    sign_off, sign_off_blockers = _sign_off_state(
        approvals=approvals, resource_summary=resource_summary, workflow=workflow,
        issues=issues, critical_findings=critical_findings, checklist_mismatches=checklist_mismatches,
    )
    quality_flags.extend(sign_off_blockers)
    batch_kpis = _as_dict(batch.get("kpis"))
    final = _as_dict(body.get("final_judgment") or body.get("finalJudgment"))
    headline = _text(final.get("verdict_line") or final.get("summary") or final.get("narrative"))
    if not headline:
        headline = "Evidence has been frozen from the loaded dashboard panels; see the section-level findings and data-quality flags."
    deep_dive = _as_dict(resource.get("deep_dive"))
    correlation_events = _as_list(deep_dive.get("patterns"))
    server_evidence = _server_evidence(servers, resource, correlation_events)
    if correlation_events and not any(_number(_as_dict(event).get("confidence_pct")) is not None for event in correlation_events):
        quality_flags.append(
            "Correlation events are present without numeric confidence. They remain evidence of time overlap only and are not chart-ranked."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "customer": customer, "audit_id": resolved_id, "audit_window": _audit_window(batch), "generated_at": now,
            "engine_version": _text(body.get("engine_version"), "PE Dashboard export 2.0"),
            "sources": [_source_record("Ctrl-M batch", batch), _source_record("Workflow SLA Matrix", sla_matrix), _source_record("Azure Monitor", resource), _source_record("SOW volume", sow), _source_record("Benchmark", benchmark)],
            "data_coverage_pct": _number(_as_dict(batch.get("data_coverage")).get("confidence")),
            "missing_metrics": ["resource fleet summary"] if resource_summary is None else [],
            "sign_off_status": sign_off,
            "sign_off_blockers": sign_off_blockers,
            "checklist_mismatches": checklist_mismatches,
            "open_findings_count": len(critical_findings),
            "pe_name": pe_name,
            "cust_name": cust_name,
            "pe_approved": requested_pe_review,
            "cust_approved": requested_customer_approval,
            "env": env,
            # Preserve the source record count for the Review Registry.  It is
            # deliberately independent from generated priority actions: an
            # action can be closed or filtered without changing how many
            # issues the reviewer logged for this audit.
            "issues_logged_count": len(issues),
        },
        "approvals": approvals,
        "executive_verdict": {"headline": headline, "confidence_pct": _number(final.get("confidence_pct") or batch_kpis.get("confidence")), "data_quality_flags": quality_flags},
        "priority_actions": _priority_actions(batch, exceptions, sow_metrics, issues, workflow),
        "workflow_sla": workflow,
        "batch_sla": {
            "window_chart_series": _as_list(batch.get("window")), "breach_days": [row for row in _as_list(batch.get("window")) if _status(_as_dict(row).get("status")) in {"breach", "failed"}],
            "tight_days": [row for row in _as_list(batch.get("window")) if _status(_as_dict(row).get("status")) in {"at_risk", "long_job", "tight"}],
            "buffer_summary": workflow["summary"] if workflow_rows else batch_kpis,
            "workload_summary": batch_kpis,
            "workflow_summary": workflow_rows,
            "long_pole_trend_series": _as_dict(batch.get("longpole_matrix")),
            "top_jobs_table": [_normalise_job_row(row) for row in _as_list(batch.get("top_jobs") or batch.get("top_breaches")) if isinstance(row, dict)], "excluded_jobs": _as_list(_as_dict(batch.get("data_coverage")).get("excluded_jobs")),
        },
        "resource_review": {
            "fleet_summary": resource_summary, "exception_rule": "status != HEALTHY, with z-score >= 2.0 included as additional anomaly evidence", "exception_table": exceptions,
            "fleet_heatmap_series": _as_dict(deep_dive.get("heatmap")), "timeseries_by_host": _as_dict(deep_dive.get("vms")),
            "all_servers_table": servers, "server_evidence": server_evidence,
            "unit_semantics": {
                "cpu": "CPU utilisation % (higher = more pressure)",
                "memory_snapshot": "host memory used % (higher = more pressure)",
                "memory_timeseries": "available memory % (lower = more pressure)",
                "disk": "disk bandwidth consumed % (higher = more pressure)",
            },
        },
        "correlation_rca": correlation_events,
        "sow_capacity": {
            "metrics": sow_metrics,
            "interpretation": _sow_interpretation(sow_metrics),
            # This is a reported source status, not a status inferred from the
            # report.  Keep it for the legacy Review Registry without making
            # that registry calculate its own SOW verdict.
            "reported_status": _text(sow.get("overall_status") or sow.get("status")),
        },
        "benchmark": {
            # The report does not render a benchmark section yet, but Review
            # Registry still needs the exact upstream summary it historically
            # displayed.  Forward the precomputed values; do not aggregate
            # benchmark rows here.
            "loaded": bool(benchmark),
            "total_transactions": _number(benchmark.get("total_transactions")),
            "sla_breach_count": _number(_first_present(benchmark.get("sla_breaches"), benchmark.get("sla_breach_count"))),
            "degraded_count": _number(_first_present(benchmark.get("degraded"), benchmark.get("degraded_count"))),
            "batch_perf_regression_count": _number(_as_dict(benchmark.get("batch_perf_summary")).get("regressions")),
            "batch_perf_total_jobs": _number(_as_dict(benchmark.get("batch_perf_summary")).get("total_jobs")),
        },
        "methodology": {
            "job_sla_def": "Job SLA measures each job run against its resolved ceiling.",
            "window_sla_def": "Workflow SLA measures a workflow's eligible observed duration against its resolved contractual ceiling.",
            "buffer_formula": "buffer_pct = (SLA hours - runtime hours) / SLA hours * 100",
            "status_bands": {
                "OK": f">{pe_config.SLA_LONGJOB_PCT:g}% buffer",
                "LONG_JOB": f">{pe_config.SLA_ATRISK_PCT:g}% to <={pe_config.SLA_LONGJOB_PCT:g}% buffer",
                "AT_RISK": f">0% to <={pe_config.SLA_ATRISK_PCT:g}% buffer",
                "BREACH": "<=0% buffer",
            },
            "exclusion_rules": _as_list(_as_dict(batch.get("data_coverage")).get("excluded_jobs")),
            "source_badges": [source["name"] for source in [_source_record("Ctrl-M batch", batch), _source_record("Workflow SLA Matrix", sla_matrix), _source_record("Azure Monitor", resource), _source_record("SOW volume", sow)] if source["loaded"]],
        },
        "prior_audit_ref": None,
        "deltas": None,
        "validation": {"errors": [], "warnings": quality_flags},
    }
