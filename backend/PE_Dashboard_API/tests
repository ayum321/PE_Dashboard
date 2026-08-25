"""Direct regression runner for generic BatchSLA header interpretation."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sla_merger import _map_columns, parse_batch_sla_xlsx


def _xlsx(rows: list[dict]) -> bytes:
    stream = io.BytesIO()
    with pd.ExcelWriter(stream, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name="SLA")
    return stream.getvalue()


def _parse(header: str, value: object, start: str = "8/3/2026 11:43") -> dict:
    raw = _xlsx([{
        "Batch_Name": "Collab Monthly",
        "Schedule": "1st of every month",
        "Start Time": start,
        header: value,
        "Current end time": "8/3/2026 11:50",
    }])
    return parse_batch_sla_xlsx(raw, "BatchSLA_info.xlsx")["workflows"][0]


def main() -> None:
    duration_headers = {
        "SLA(in Hrs)": ("8Hrs", 8.0),
        "SLA (in Hours)": ("6 hours", 6.0),
        "SLA (Minutes)": ("90 min", 1.5),
        "SLA_Hours": ("4.5", 4.5),
        "Expected SLA (hrs)": ("3Hrs", 3.0),
    }
    for header, (raw_value, expected) in duration_headers.items():
        mapping = _map_columns(["Batch_Name", "Start Time", header, "Current end time"])
        assert mapping.get("Expected_SLA") == header, (header, mapping)
        assert mapping.get("Expected_End_Time") != header, (header, mapping)
        row = _parse(header, raw_value)
        assert row["sla_hours"] == expected, (header, row)
        assert row["sla_source"] == "BATCH_SLA_XLSX", (header, row)
        assert row["sla_confidence"] == "VERIFIED", (header, row)

    # Reproduce the reported nine-workflow file shape. Every row must remain
    # explicit, and monthly 8-hour values must not collapse to the 6-hour
    # DATE_SPECIFIC_MONTHLY fallback.
    screenshot_rows = [
        ("Maintenance", "Daily", "6Hrs"),
        ("DB backup", "Daily", "6Hrs"),
        ("Demand Daily", "Mon-Fri", "6Hrs"),
        ("FF daily", "Mon-Fri", "6Hrs"),
        ("Collab Weekly", "Mon and Thur", "8Hrs"),
        ("Collab Monthly", "1st of every month", "8Hrs"),
        ("Demand Sunday", "Sunday", "8Hrs"),
        ("Demand Monthly", "1st of every month", "8Hrs"),
        ("FF Nightly", "Mon-Fri", "6Hrs"),
    ]
    raw = _xlsx([
        {
            "Batch_Name": name,
            "Schedule": schedule,
            "Start Time": "8/3/2026 06:00",
            "SLA(in Hrs)": sla,
            "Current end time": "8/3/2026 06:30",
        }
        for name, schedule, sla in screenshot_rows
    ])
    parsed = parse_batch_sla_xlsx(raw, "BatchSLA_info.xlsx")
    assert parsed["row_count"] == 9, parsed
    assert all(w["sla_source"] == "BATCH_SLA_XLSX" for w in parsed["workflows"]), parsed
    assert not any("has no SLA column" in w for w in parsed["warnings"]), parsed
    by_name = {w["workflow"]: w for w in parsed["workflows"]}
    assert by_name["Collab Monthly"]["sla_hours"] == 8.0, by_name["Collab Monthly"]
    assert by_name["Demand Monthly"]["sla_hours"] == 8.0, by_name["Demand Monthly"]

    # A bare SLA header remains a clock-time deadline and must not be forced
    # through duration parsing.
    deadline_mapping = _map_columns([
        "Batch_Name", "Start Time", "SLA", "Current end time",
    ])
    assert deadline_mapping.get("Expected_End_Time") == "SLA", deadline_mapping
    assert "Expected_SLA" not in deadline_mapping, deadline_mapping
    deadline = _parse("SLA", "7:00 PM", start="8/3/2026 5:00 PM")
    assert deadline["sla_hours"] == 2.0, deadline
    assert deadline["sla_source"] == "BATCH_SLA_XLSX", deadline

    # A bare SLA header is disambiguated by explicit units in its values.
    bare_duration = _parse("SLA", "8Hrs")
    assert bare_duration["sla_hours"] == 8.0, bare_duration
    assert bare_duration["sla_source"] == "BATCH_SLA_XLSX", bare_duration
    bare_numeric_duration = _parse("SLA", 8)
    assert bare_numeric_duration["sla_hours"] == 8.0, bare_numeric_duration
    bare_numeric_text_duration = _parse("SLA", "8.0")
    assert bare_numeric_text_duration["sla_hours"] == 8.0, bare_numeric_text_duration

    print("PASS: SLA duration/deadline header variants are interpreted correctly")


if __name__ == "__main__":
    main()
