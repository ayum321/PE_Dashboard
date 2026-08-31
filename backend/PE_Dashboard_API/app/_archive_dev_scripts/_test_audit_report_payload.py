"""Focused contract checks for the immutable report payload.

Run from ``backend/PE_Dashboard_API/app`` with:
    python _test_audit_report_payload.py
"""
from services.audit_report_payload import attach_prior_audit, build_audit_report_payload
from services.report_svg import render_report_charts


def _body():
    return {
        "customer_name": "Example Customer",
        "approvals": {
            "pe": {"approved": True},
            "customer": {"approved": True},
        },
        "batch": {
            "kpis": {"window_compliance_pct": 96.6, "breach_count": 1},
            "window": [{"date": "2026-08-26", "effective_hrs": 7.0, "sla_hrs": 6.0, "status": "BREACH"}],
            "top_jobs": [{"Job_Name": "JOB_A", "Sub_Application": "TEST_DAILY", "schedule_type": "DAILY", "peak_hrs": 6.5, "avg_hrs": 5.1, "sla_hrs": 8, "buffer_pct": 18.75, "buffer_status": "LONG_JOB"}],
            "longpole_matrix": {"rows": [{"job": "JOB_A", "avg_min": 240, "max_min": 390, "is_longpole": True}]},
        },
        "resource": {
            "kpis": {"total_servers": 2, "n_critical": 0, "n_warning": 1, "n_healthy": 1, "fleet_grade": "B", "fleet_score": 84.5},
            "servers": [
                {"host": "healthy-host", "status": "Healthy", "cpu_used": 20},
                {"host": "warn-host", "status": "Warning", "cpu_used": 89},
            ],
            "deep_dive": {
                "heatmap": {"timestamps": ["2026-08-26T00:00:00Z"], "grids": {"cpu": [{"name": "warn-host", "values": [89]}]}},
                "vms": {"WARN-HOST": {"series": {"Percentage CPU": [{"t": "2026-08-26T00:00:00Z", "v": 89}]}}},
                "patterns": [{"type": "cross_vm_correlation", "title": "Two hosts at 00:00 UTC", "confidence_pct": 62}],
            },
        },
        "sow": {"metrics": [{"label": "Daily DFU", "sow": 9000000, "actual": 7968993, "pct": 88.5, "status": "ACCEPTABLE"}]},
    }


def main():
    payload = build_audit_report_payload(_body(), audit_id="AUD-CONTRACT")
    fleet = payload["resource_review"]["fleet_summary"]
    assert fleet["ok"] == 1 and fleet["warning"] == 1
    assert payload["meta"]["sign_off_status"] == "customer_approved"
    assert [row["host"] for row in payload["resource_review"]["exception_table"]] == ["warn-host"]
    job = payload["batch_sla"]["top_jobs_table"][0]
    assert job["sub_app"] == "TEST_DAILY" and job["buffer_pct"] == 18.75 and job["status"] == "LONG_JOB"
    sow = payload["sow_capacity"]["metrics"][0]
    assert sow["commitment"] == 9000000 and sow["pct_of_contract"] == 88.5
    charts, errors, _warnings = render_report_charts(payload)
    assert not errors and {"batch_cadence", "fleet_heatmap", "exception_timeseries", "correlation", "resource_snippets"}.issubset(charts)

    missing_kpis = _body()
    missing_kpis["resource"].pop("kpis")
    blocked = build_audit_report_payload(missing_kpis, audit_id="AUD-BLOCKED")
    assert blocked["resource_review"]["fleet_summary"] is None
    assert blocked["meta"]["sign_off_status"] != "customer_approved"

    attach_prior_audit(payload, None)
    assert payload["deltas"]["state"] == "first_audit"

    # No Azure deep-dive evidence is a visible report caveat, not a false
    # "blank chart" failure. A supplied-but-empty series is still rejected by
    # the renderer's zero-point gate.
    source_absent = _body()
    source_absent["resource"].pop("deep_dive")
    absent_payload = build_audit_report_payload(source_absent, audit_id="AUD-ABSENT")
    absent_charts, absent_errors, absent_warnings = render_report_charts(absent_payload)
    assert not absent_errors and "batch_cadence" in absent_charts
    assert any("Fleet heatmap source evidence is absent" in warning for warning in absent_warnings)
    print("audit report payload checks passed")


if __name__ == "__main__":
    main()
