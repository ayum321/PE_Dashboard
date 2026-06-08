"""Test SLA Matrix per-job resolution."""
from fastapi.testclient import TestClient
from main import app
from services import config_store

# Simulate uploaded SLA intelligence with per-workflow contracts
config_store.set("_sla_intelligence", {
    "valid_rows": 2,
    "ceilings": {"DAILY": 1.5},
    "contracts": [
        {
            "batch_name": "WF1",
            "schedule_type": "DAILY",
            "sla_model": "WINDOW",
            "sla_window_hrs": 1.5,
            "sla_duration_hrs": None,
            "completeness": "complete",
            "source_row": 2,
            "schedule_raw": "Daily",
            "sla_start": None,
            "sla_end": None,
            "buffer_minutes": None,
            "first_job": None,
            "last_job": None,
            "timezone": None,
            "comments": "",
            "interpretation_notes": "",
            "source_sheet": "Sheet1",
            "business_ack": False,
            "is_cyclic": False,
        },
        {
            "batch_name": "WF2",
            "schedule_type": "DAILY",
            "sla_model": "WINDOW",
            "sla_window_hrs": 0.75,
            "sla_duration_hrs": None,
            "completeness": "complete",
            "source_row": 3,
            "schedule_raw": "Daily",
            "sla_start": None,
            "sla_end": None,
            "buffer_minutes": None,
            "first_job": None,
            "last_job": None,
            "timezone": None,
            "comments": "",
            "interpretation_notes": "",
            "source_sheet": "Sheet1",
            "business_ack": False,
            "is_cyclic": False,
        },
    ],
})

client = TestClient(app)

# Simulate production CSV rows — WF1 breaches at 6h, WF2 breaches at 3h
rows = [
    {"Job_Name": "WF1_JOB_A", "Sub_Application": "WF1", "run_time_hrs": 6.0, "Status": "ENDED OK", "Start_Time": "2025-04-27 02:00:00", "End_Time": "2025-04-27 08:00:00"},
    {"Job_Name": "WF1_JOB_B", "Sub_Application": "WF1", "run_time_hrs": 1.2, "Status": "ENDED OK", "Start_Time": "2025-04-21 02:00:00", "End_Time": "2025-04-21 03:12:00"},
    {"Job_Name": "WF2_JOB_A", "Sub_Application": "WF2", "run_time_hrs": 3.0, "Status": "ENDED OK", "Start_Time": "2025-04-27 01:00:00", "End_Time": "2025-04-27 04:00:00"},
    {"Job_Name": "WF2_JOB_B", "Sub_Application": "WF2", "run_time_hrs": 0.3, "Status": "ENDED OK", "Start_Time": "2025-04-21 01:00:00", "End_Time": "2025-04-21 01:18:00"},
    {"Job_Name": "WF1_JOB_A", "Sub_Application": "WF1", "run_time_hrs": 10.2, "Status": "ENDED OK", "Start_Time": "2025-05-04 02:00:00", "End_Time": "2025-05-04 12:12:00"},
]

r = client.post("/api/sla-matrix/json", json={"rows": rows, "sla_mode": "daily"})
data = r.json()

print(f"HTTP {r.status_code}")
print(f"SLA Label: {data.get('sla_label')}")
print(f"Compliance (headline): {data.get('compliance_pct')}%")
print(f"Window Compliance: {data.get('window_compliance_pct')}%")
print(f"Window Total Days: {data.get('window_total_days')}")
print(f"Window Breach Days: {data.get('window_breach_days')}")
print(f"Breaching Runs: {data.get('breaching_runs')}")
print(f"Total Runs: {data.get('total_runs')}")
print(f"Worst Job: {data.get('worst_job')} ({data.get('worst_hrs')}h)")
print()
print("Per-run detail:")
for b in data.get("breaches", []):
    print(f"  {b['job_name']} | {b['run_date']} | {b['run_hrs']}h vs SLA {b['sla_limit_hrs']}h | {b['status']}")
print()
print("Window detail:")
for w in data.get("window_detail") or []:
    print(f"  {w['run_date']} | elapsed {w['elapsed_hrs']}h | SLA {w['sla_hrs']}h | {'BREACH' if w['breach'] else 'OK'}")
