"""Regression check for the baseline-locked standalone report.

Run from ``backend/PE_Dashboard_API/app`` with:
    python _test_legacy_report_export.py

This intentionally renders the original report template.  It proves that the
additive cover, one batch chart, and resource evidence use supplied dashboard
evidence without blanking the baseline Buffer/Status or SOW fields.
"""
from routers.export import ExportRequest, _locked_legacy_context, templates
from services.audit_report_payload import build_audit_report_payload


def _body() -> dict:
    return {
        "customer_name": "Example Customer",
        "approvals": {
            "customer_name": "Example Customer",
            "env_type": "TEST",
            "pe": {"approved": True, "name": "PE Reviewer", "date": "27 Aug 2026"},
            "customer": {"approved": True, "name": "Customer Reviewer", "date": "27 Aug 2026"},
        },
        "batch": {
            "kpis": {
                "compliance_pct": 96.6,
                "jobs_breach": 1,
                "jobs_ok": 28,
                "total_jobs": 29,
                "total_runs": 1804,
                "total_hrs": 146.3,
            },
            "data_coverage": {"date_range": ["2026-07-26", "2026-08-24"]},
            "top_jobs": [{
                "Job_Name": "JDA_PROCESSING_JOB_WKLY_2",
                "Sub_Application": "TEST_WEEKLY",
                "schedule_type": "WEEKLY",
                "peak_hrs": 8.13,
                "avg_hrs": 6.12,
                "sla_hrs": 13.0,
                "buffer_pct": 37.4,
                "buffer_status": "LONG_JOB",
                "sla_source": "batch_sla_xlsx",
            }],
        },
        "resource": {
            "kpis": {"total_servers": 2, "n_critical": 0, "n_warning": 1, "fleet_grade": "B", "fleet_score": 84.9},
            "servers": [
                {"host": "db-a", "type": "DB", "cpu_used": 92.0, "mem_used": 91.0, "disk_pct": 10.0, "mem_gb": 64.0, "status": "WARNING"},
                # Reproduces the Nfm defect: a red disk peak on a row the live
                # grader calls healthy. The row must explain itself, not contradict.
                {"host": "db-b", "type": "DB", "cpu_used": 5.5, "mem_used": 86.0, "disk_pct": 100.0, "mem_gb": 133.0, "status": "HEALTHY"},
            ],
            "deep_dive": {
                "vms": {"db-a": {
                    "series": {
                        "Percentage CPU": [{"t": "2026-08-24T00:00:00Z", "v": 16}, {"t": "2026-08-24T01:00:00Z", "v": 92}],
                        "Available Memory Percentage": [{"t": "2026-08-24T00:00:00Z", "v": 21}, {"t": "2026-08-24T01:00:00Z", "v": 9}],
                        "OS Disk Bandwidth Consumed Percentage": [{"t": "2026-08-24T00:00:00Z", "v": 1}, {"t": "2026-08-24T01:00:00Z", "v": 10}],
                    },
                    "spikes": {"Percentage CPU": [{
                        "severity": "critical", "peak": 92.0,
                        "start": "2026-08-24T00:30:00", "end": "2026-08-24T01:00:00",
                        "peak_time": "2026-08-24T01:00:00", "duration_min": 30,
                    }]},
                }},
                "spike_attribution": {"rows": [{
                    "vm": "db-a", "metric": "Percentage CPU", "severity": "critical", "peak": 92.0,
                    "start": "2026-08-24T00:30:00", "end": "2026-08-24T01:00:00",
                    "concurrent_jobs": 1, "heaviest": "JDA_PROCESSING_JOB_WKLY_2",
                    "jobs": [{"job": "JDA_PROCESSING_JOB_WKLY_2", "hrs": 0.5,
                              "start": "2026-08-24T00:20:00", "end": "2026-08-24T00:50:00"}],
                }]},
            },
        },
        "sow": {"metrics": [{"label": "Daily DFU", "sow": 9000000, "actual": 7968993, "pct": 88.5, "status": "ACCEPTABLE"}]},
    }


def main() -> None:
    source = _body()
    body = ExportRequest(**source)
    report = build_audit_report_payload(body.model_dump(exclude_none=True), audit_id="AUD-LEGACY-TEST")
    context, _ = _locked_legacy_context(body, report)
    html = templates.get_template("report_export.html").render(**context)

    # Locked baseline markers and baseline values remain complete.
    assert "Performance Engineering" in html and "Fleet Severity Distribution" in html
    assert "JDA_PROCESSING_JOB_WKLY_2" in html
    assert "37.4%" in html and "LONG JOB" in html
    assert "9,000,000" in html and "7,968,993" in html and "88.5%" in html

    # Only additive evidence is new; its lines and SVG come from existing payload data.
    assert "Audit Evidence Record" in html and "AUD-LEGACY-TEST" in html
    assert "Per-host trend &amp; Ctrl-M overlap evidence" in html
    assert "Ctrl-M overlap: JDA_PROCESSING_JOB_WKLY_2" in html
    assert "time overlap only, not proof of cause" in html
    assert html.count("Peak runtime vs SLA ceiling") == 1
    assert "<svg" in html
    assert "Methodology &amp; Lineage" in html

    # The cadence bar is scaled against the job's own ceiling, so its label must
    # carry the ceiling it was measured against rather than a bare peak.
    assert "8.13h of 13.00h" in html

    # A row whose live status grades calmer than its own worst cell explains itself.
    assert "Disk peaked above its critical ceiling" in html
    assert "live role-aware status is healthy" in html

    # The sign-off banner states what it does and does not clear.
    assert "It does not clear the findings below" in html

    # SOW utilisation is charted, not only tabulated.
    assert "Consumption as a share of the contracted ceiling" in html

    # The Ctrl-M overlap is drawn on the trend chart, not only named in prose.
    assert "Ctrl-M run (1)" in html, "Ctrl-M run lane missing from the chart legend"
    assert "Spike window" in html, "spike band missing from the chart legend"
    assert "JDA_PROCESSING_JOB_WKLY_2 (0.50h)" in html, "Ctrl-M run bar has no titled interval"
    assert "Ctrl-M bars are time coincidence, not proof of cause" in html
    print("legacy baseline report checks passed")


if __name__ == "__main__":
    main()
