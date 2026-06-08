"""Quick end-to-end test for PE Narrative with flat SLA matrix + DFU/SKU."""
import urllib.request, json

payload = {
    "customer_name": "Petbarn",
    "batch": {
        "filename": "CtrlM.csv",
        "kpis": {"total_jobs": 249, "total_runs": 1814, "compliance_pct": 15.0,
                 "jobs_breach": 5, "failed_runs": 3, "daily_limit_hrs": 6.0},
        "top_jobs": [{"Job_Name": "JOB_001", "peak_hrs": 5.2, "avg_hrs": 4.8,
                      "total_hrs": 24.0, "buffer_pct": 13.3, "sla_used_pct": 86.7,
                      "buffer_status": "AT_RISK"}],
    },
    # FLAT sla_matrix — exactly what SlaMatrixResponse.model_dump() returns
    "sla_matrix": {
        "compliance_pct": 15.0, "total_runs": 1814, "total_jobs": 249,
        "breaching_runs": 1547, "at_risk_runs": 200, "failed_runs": 67,
        "ok_runs": 267, "sla_limit_hrs": 6.0,
        "window_breach_days": 12, "window_total_days": 30,
        "job_summary": [
            {"job_name": "JOB_001", "peak_hrs": 5.2, "sla_limit": 6.0, "buffer_pct": 13.3},
            {"job_name": "JOB_002", "peak_hrs": 4.8, "sla_limit": 6.0, "buffer_pct": 20.0},
        ],
        "breaches": [],
    },
    "resource": {
        "kpis": {"fleet_grade": "F", "avg_cpu": 82.0, "avg_mem": 77.0,
                 "n_critical": 3, "n_warning": 2, "total_servers": 6},
        "servers": [
            {"host": "SRE-01", "type": "SRE", "cpu_pct": 88.0, "mem_pct": 82.0, "status": "Critical"},
            {"host": "APP-01", "type": "APP", "cpu_pct": 75.0, "mem_pct": 70.0, "status": "Warning"},
        ]
    },
    # Manual DFU/SKU built by triggerPeNarrative() in the frontend
    "sow_compare": {
        "Daily DFU": {"sow": 500000, "actual": 450000},
        "Daily SKU": {"sow": 80000, "actual": 72000},
    }
}

req = urllib.request.Request(
    "http://127.0.0.1:8765/api/pe-narrative",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}
)
resp = urllib.request.urlopen(req)
data = json.loads(resp.read())

print("VERDICT:", data["verdict"])
print("SUMMARY:", data["summary"][:130])
print()
for s in data["sections"]:
    print("=== " + s["title"] + " ===")
    print("  prose:", s["prose"][:130])
    print("  rows:", s["table"]["rows"][:3])
    print()
