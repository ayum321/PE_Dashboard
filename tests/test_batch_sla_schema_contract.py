"""Strict v1 BatchSLA schema regression runner.

Run from ``app/``:
    py -3.14 ..\tests\test_batch_sla_schema_contract.py
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi import HTTPException
from starlette.datastructures import UploadFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from routers.upload import upload_batch_sla
from services import config_store
from services.sla_merger import build_workbook_sla_snapshot, parse_batch_sla_xlsx


def _xlsx(columns: list[str], values: list[object]) -> bytes:
    stream = io.BytesIO()
    frame = pd.DataFrame([values], columns=columns)
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="SLA")
    return stream.getvalue()


def _workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, index=False, sheet_name=name)
    return stream.getvalue()


def _valid_columns() -> list[str]:
    return ["Batch_Name", "Start Time", "Expected End Time/SLA", "Comments"]


def _valid_values() -> list[object]:
    return ["DEMAND_DAILY", "23:00 EST", "07:00 EST", "verified fixture"]


def main() -> None:
    raw = _xlsx(_valid_columns(), _valid_values())
    parsed = parse_batch_sla_xlsx(raw)
    assert parsed["ingestion_status"] == "accepted", parsed
    assert parsed["workflows"][0]["sla_hours"] == 8.0, parsed

    # Case / whitespace / underscore variants are intentionally equivalent.
    equivalent = _xlsx(
        [" batch name ", "START_TIME", "expected end time/sla", "comments"],
        _valid_values(),
    )
    equivalent_parsed = parse_batch_sla_xlsx(equivalent)
    assert equivalent_parsed["ingestion_status"] == "accepted", equivalent_parsed
    assert equivalent_parsed["workflows"][0]["sla_hours"] == parsed["workflows"][0]["sla_hours"]

    # Batch_SLA.xlsx is a declared schedule contract. "End Time" and
    # "Duration" must map transparently, while the absence of an explicitly
    # named Current End Time means it is NOT an observed execution.
    contract_only = _xlsx(
        ["Module", "Batch name\u202f", "Frequency", "Start time", "End Time", "Duration", "Comment"],
        ["CORE", "CCBA_IO_SUNDAY", "Weekly", "08:30 AM SAST", "02:30 PM SAST", "06:00:00 hrs.", "contract only"],
    )
    contract_parsed = parse_batch_sla_xlsx(contract_only, "Batch_SLA.xlsx")
    assert contract_parsed["ingestion_status"] == "accepted", contract_parsed
    contract_row = contract_parsed["workflows"][0]
    assert contract_row["sla_hours"] == 6.0, contract_row
    assert contract_row["last_run_hours_xlsx"] is None, contract_row
    assert contract_row["workbook_timing_source"] == "WORKBOOK_COMPLETION_NOT_REPORTED", contract_row
    contract_snapshot = build_workbook_sla_snapshot(contract_parsed)
    assert contract_snapshot["observed_workflow_count"] == 0, contract_snapshot
    assert contract_snapshot["workflow_summary"][0]["status"] == "NOT_OBSERVED", contract_snapshot

    # A clock-window and declared Duration that agree are valid contract
    # evidence. The duration remains a contract value, never an execution.
    assert contract_row["workbook_clock_window_hours"] == 6.0, contract_row
    assert contract_row["workbook_contract_duration_hours"] == 6.0, contract_row
    assert contract_row["contract_conflict"] is False, contract_row

    # When those two contract statements disagree, do not select either value
    # or fall back to a generic ceiling. The conflict must remain visible in
    # the workbook snapshot so it cannot turn into a fabricated verdict.
    conflicting = _xlsx(
        ["Batch name", "Start time", "End Time", "Duration"],
        ["GENERIC_MONTHLY", "01:00", "02:00", "02:00:00 hrs"],
    )
    conflict_parsed = parse_batch_sla_xlsx(conflicting, "any_customer.xlsx")
    conflict_row = conflict_parsed["workflows"][0]
    assert conflict_row["sla_source"] == "CONTRACT_CONFLICT", conflict_row
    assert conflict_row["sla_hours"] is None, conflict_row
    assert conflict_row["sla_schema"] == "CLOCK_DURATION_CONFLICT", conflict_row
    assert conflict_row["workbook_clock_window_hours"] == 1.0, conflict_row
    assert conflict_row["workbook_contract_duration_hours"] == 2.0, conflict_row
    conflict_snapshot = build_workbook_sla_snapshot(conflict_parsed)
    assert conflict_snapshot["workflow_summary"][0]["status"] == "SLA_CONTRACT_CONFLICT", conflict_snapshot
    assert conflict_snapshot["workflow_summary"][0]["sla_h"] is None, conflict_snapshot
    assert conflict_snapshot["workflow_summary"][0]["measurement_reason_code"] == "CLOCK_DURATION_CONFLICT", conflict_snapshot

    # A workbook can include lookup/readme tabs. They must be transparently
    # ignored, not represented as blocked SLA fields in the mapping report.
    with_auxiliary_tab = _workbook({
        "Contract": pd.DataFrame([_valid_values()], columns=_valid_columns()),
        "Lookup": pd.DataFrame({"Daily/Weekly/Monthly": ["Daily"]}),
    })
    multi_sheet = parse_batch_sla_xlsx(with_auxiliary_tab, "multi.xlsx")
    reports = {sheet["sheet_name"]: sheet for sheet in multi_sheet["mapping_report"]["sheets"]}
    assert reports["Contract"]["included_in_ingestion"] is True, reports
    assert reports["Lookup"]["included_in_ingestion"] is False, reports
    assert multi_sheet["row_count"] == 1, multi_sheet

    # An explicitly supplied Current End Time remains workbook evidence even
    # when it equals the contractual expected end. The equality is a caveat,
    # not a reason to hide the calculated zero-buffer result.
    placeholder = _xlsx(
        _valid_columns()[:-1] + ["Current end time", "Comments"],
        ["USF_DAILY", "21:05 CST", "06:00 CST", "06:00 CST", "UAT placeholder"],
    )
    placeholder_parsed = parse_batch_sla_xlsx(placeholder, "USF_2025_BatchSLA_info.xlsx")
    placeholder_row = placeholder_parsed["workflows"][0]
    assert placeholder_row["runtime_is_placeholder"] is False, placeholder_row
    assert placeholder_row["runtime_source_caveat"] == "REPORTED_END_EQUALS_TARGET", placeholder_row
    assert placeholder_row["last_run_hours_xlsx"] == 8.917, placeholder_row
    assert placeholder_row["compliance"] == "NO_BUFFER", placeholder_row
    placeholder_snapshot = build_workbook_sla_snapshot(placeholder_parsed)["workflow_summary"][0]
    assert placeholder_snapshot["status"] == "NO_BUFFER", placeholder_snapshot
    assert placeholder_snapshot["duration_headroom_mins"] == 0, placeholder_snapshot
    assert placeholder_snapshot["buffer_pct"] == 0.0, placeholder_snapshot

    # A dated Batch/Start/End/Total batch time workbook is execution history,
    # not a contract. It must be handed to Batch Review without inventing SLA.
    wella_history = _xlsx(
        ["Batch", "Start Time", "End Time", "Total batch time"],
        ["Daily", "2026-04-09 00:29", "2026-04-09 05:50", "05:21:00"],
    )
    history_detected = parse_batch_sla_xlsx(wella_history, "Batch Run Times.xlsx")
    assert history_detected["ingestion_status"] == "reroute", history_detected
    assert history_detected["file_role"] == "batch_execution_history", history_detected
    assert history_detected["execution_history_sheets"][0]["runtime_field"] == "Total batch time", history_detected

    # Missing required fields reject the candidate before any configuration is written.
    missing = _xlsx(["Batch_Name", "Expected End Time/SLA"], ["DEMAND_DAILY", "07:00 EST"])
    rejected = parse_batch_sla_xlsx(missing)
    assert rejected["ingestion_status"] == "blocked", rejected
    assert "start_time" in rejected["mapping_report"]["sheets"][0]["missing_required"], rejected
    with patch.object(config_store, "set") as set_config:
        try:
            asyncio.run(upload_batch_sla(UploadFile(io.BytesIO(missing), filename="missing.xlsx")))
            raise AssertionError("missing required field was accepted")
        except HTTPException as exc:
            assert exc.status_code == 422, exc
            assert "start_time" in exc.detail["message"], exc.detail
        set_config.assert_not_called()

    # Optional source absence is explicit, never a blank/dash masquerading as data.
    states = {state["canonical_field"]: state for state in parsed["mapping_report"]["sheets"][0]["field_states"]}
    assert states["module"]["state"] == "field_absent_in_source", states
    assert states["comments"]["state"] == "mapped_populated", states

    # Two raw headers resolving to one canonical field are an upload blocker.
    duplicate = _xlsx(
        ["Batch_Name", "Batch Name", "Start Time", "Expected End Time/SLA"],
        ["DEMAND_DAILY", "DEMAND_DAILY", "23:00 EST", "07:00 EST"],
    )
    duplicate_parsed = parse_batch_sla_xlsx(duplicate)
    assert duplicate_parsed["ingestion_status"] == "blocked", duplicate_parsed
    duplicate_fields = [item["canonical_field"] for item in duplicate_parsed["mapping_report"]["sheets"][0]["duplicates"]]
    assert "batch_name" in duplicate_fields, duplicate_parsed
    print("PASS: strict BatchSLA schema maps verified variants, blocks ambiguity, and preserves prior config on rejection")


if __name__ == "__main__":
    main()
