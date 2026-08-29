"""Regression guard for the React-consumed SLA workflow evidence contract.

Run: py -3.14 _test_sla_workflow_contract.py
"""
from __future__ import annotations

import pandas as pd

from routers.sla_matrix import _compute_sla_matrix


def main() -> None:
    df = pd.DataFrame([
        {"Job_Name": "BATCH_START", "Sub_Application": "TEST_DAILY", "Status": "ENDED OK",
         "Start_Time": "2026-03-01 00:00", "End_Time": "2026-03-01 01:00", "Run_Sec": 3600,
         "run_time_hrs": 1.0, "run_date": "2026-03-01"},
        {"Job_Name": "BATCH_END", "Sub_Application": "TEST_DAILY", "Status": "ENDED OK",
         "Start_Time": "2026-03-01 03:00", "End_Time": "2026-03-01 04:00", "Run_Sec": 3600,
         "run_time_hrs": 1.0, "run_date": "2026-03-01"},
    ])
    result = _compute_sla_matrix(df, "daily", 6.0).model_dump()
    workflows = result.get("workflow_summary") or []
    assert workflows, result
    row = workflows[0]
    required = {
        "actual_start_time", "actual_end_time", "elapsed_duration_h",
        "sla_measurement_basis", "duration_headroom_h", "duration_overrun_h",
        "runtime_h", "status",
    }
    missing = required - set(row)
    assert not missing, f"workflow contract missing {sorted(missing)}: {row}"
    assert row["actual_start_time"] == row["workflow_start"], row
    assert row["actual_end_time"] == row["workflow_end"], row
    assert row["elapsed_duration_h"] == row["runtime_h"], row
    assert row["sla_measurement_basis"] == "per_run_elapsed_span", row
    assert row["duration_headroom_h"] == round(row["sla_h"] - row["runtime_h"], 4), row
    assert row["duration_overrun_h"] == 0.0, row
    print("[OK] SLA workflow response exposes stable actual start/end, elapsed span, basis, headroom, and overrun")


if __name__ == "__main__":
    main()
