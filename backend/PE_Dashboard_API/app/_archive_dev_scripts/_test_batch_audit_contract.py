"""Regression checks for audit-visible batch duration and Findings contracts.

Run: py -3.14 _test_batch_audit_contract.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pandas as pd

from routers.findings import FindingsRequest, _generate_findings_impl, _uat_evidence
from services.batch_calculator import _annotate_window_spikes, build_batch_payload


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_idle_gap_is_visible_but_does_not_replace_sla_basis() -> None:
    """A separated run exposes its clock span while keeping block-based SLA duration."""
    rows = []
    start = datetime(2026, 1, 1, 0, 0)
    for day in range(6):
        d = start + timedelta(days=day)
        # Two one-hour jobs with a four-hour idle gap: elapsed=6h, busy=2h,
        # longest contiguous block=1h.  The response must show all three.
        rows.extend([
            {"Job_Name": "BATCH_START", "Sub_Application": "DAILY", "Status": "ENDED OK",
             "Start_Time": d, "End_Time": d + timedelta(hours=1), "Run_Sec": 3600,
             "run_time_hrs": 1.0, "run_date": d.date(), "month": d.strftime("%Y-%m")},
            {"Job_Name": "BATCH_END", "Sub_Application": "DAILY", "Status": "ENDED OK",
             "Start_Time": d + timedelta(hours=5), "End_Time": d + timedelta(hours=6), "Run_Sec": 3600,
             "run_time_hrs": 1.0, "run_date": d.date(), "month": d.strftime("%Y-%m")},
        ])
    payload = build_batch_payload(pd.DataFrame(rows))
    point = payload["window"][0]
    _assert(point["actual_start"] and point["actual_end"], "actual window bounds missing")
    _assert(point["elapsed_hrs"] == 6.0 and point["active_busy_hrs"] == 2.0,
            f"idle-gap evidence wrong: {point}")
    _assert(point["sla_measurement_basis"] == "largest_contiguous_block", point)
    _assert(point["sla_duration_hrs"] == 1.0, point)
    _assert(point["clock_headroom_hrs"] != point["duration_headroom_hrs"], point)
    print("  [OK] idle gap exposes start/end, elapsed, busy, block SLA basis, and separate clock headroom")


def test_spike_metadata_is_server_owned() -> None:
    records = [{"run_date": f"2026-02-{d:02d}", "effective_hrs": 1.0} for d in range(1, 6)]
    records.append({"run_date": "2026-02-06", "effective_hrs": 10.0})
    _annotate_window_spikes(records)
    spike = records[-1]["spike"]
    _assert(spike and spike["is_spike"], records)
    _assert(spike["trigger_reason"] == "runtime_above_statistical_baseline", spike)
    _assert(spike["baseline_mean_hrs"] > 0 and spike["z_score"] >= spike["threshold_z"], spike)
    _assert(all(r["spike"] is None for r in records[:-1]), records)
    print("  [OK] daily-window spike metadata includes server baseline, z-score, and typed reason")


def test_uat_is_absent_without_ui_evidence_and_present_with_it() -> None:
    _assert(_uat_evidence(None) is None, "UAT must be absent with no benchmark")
    _assert(_uat_evidence({"kind": "batch", "batch_perf_summary": {}}) is None,
            "empty batch-performance metadata must not create a UAT section")
    batch_uat = _uat_evidence({"kind": "batch", "batch_perf_summary": {
        "total_jobs": 3, "comparable": 3, "regressions": 1,
    }})
    _assert(batch_uat and batch_uat["evidence_type"] == "batch_performance_comparison"
            and batch_uat["severity"] == "critical", batch_uat)
    uat = _uat_evidence({"kind": "ui", "rows": [{"transaction": "Plan", "status": "BREACH"}],
                         "summary": {"total": 1, "degraded": 1}, "sla_breaches": 1})
    _assert(uat and uat["severity"] == "critical" and uat["transactions"] == 1, uat)
    response = asyncio.run(_generate_findings_impl(FindingsRequest(
        batch_kpis={"total_runs": 1, "total_jobs": 1},
        top_jobs=[{"Job_Name": "J", "peak_hrs": 9, "sla_hrs": 6, "buffer_status": "BREACH"}],
        benchmark={"kind": "ui", "rows": [{"transaction": "Plan", "status": "BREACH"}],
                   "summary": {"total": 1, "degraded": 1}, "sla_breaches": 1},
    )))
    _assert(response.uat and response.top_action and response.top_action["rank"] == 1,
            response.model_dump())
    print("  [OK] UAT is gated by supplied UI/batch-performance evidence and Findings returns server-ranked top action")


def main() -> None:
    print("Batch audit contract regression suite")
    test_idle_gap_is_visible_but_does_not_replace_sla_basis()
    test_spike_metadata_is_server_owned()
    test_uat_is_absent_without_ui_evidence_and_present_with_it()
    print("ALL BATCH AUDIT CONTRACT CHECKS PASSED")


if __name__ == "__main__":
    main()
