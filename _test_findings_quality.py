"""Test that findings have root_cause, impact, recommendation populated."""
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

servers = [
    {"host": "App_1", "type": "APP", "cpu_pct": 81, "effective_cpu": 81,
     "mem_pct": 88, "dual_pressure": True, "state": "CRITICAL"},
    {"host": "DB_1",  "type": "DB",  "cpu_pct": 77, "effective_cpu": 77,
     "mem_pct": 55, "dual_pressure": False, "state": "WARNING"},
    {"host": "SRE_1", "type": "SRE", "cpu_pct": 65, "effective_cpu": 65,
     "mem_pct": 82, "dual_pressure": False, "state": "WARNING"},
]
resource_kpis = {
    "fleet_grade": "D", "fleet_score": 22,
    "n_critical": 1, "n_warning": 2, "n_healthy": 0,
    "avg_cpu": 74, "avg_mem": 75, "total_servers": 3,
}
sla_matrix = {
    "compliance_pct": 33.3,
    "window_compliance_pct": 33.3,
    "window_total_days": 3,
    "window_breach_days": 2,
    "breaching_runs": 3,
    "at_risk_runs": 0,
    "ok_runs": 2,
    "total_runs": 5,
    "total_jobs": 2,
    "sla_label": "Per-Job SLA (customer matrix)",
    "sla_limit_hrs": 1.5,
    "worst_job": "WF1_JOB_A",
    "worst_hrs": 10.2,
    "worst_margin_hrs": 8.7,
    "breaches": [{"job_name": "WF1_JOB_A", "sub_application": "WF1",
                  "run_date": "2025-05-04", "start_time": "02:00", "end_time": "12:12",
                  "run_hrs": 10.2, "sla_limit_hrs": 1.5, "breach_margin_hrs": 8.7, "status": "BREACH"}],
    "job_summary": [],
}

r = client.post("/api/generate-findings", json={
    "resource_kpis": resource_kpis,
    "servers": servers,
    "sla_matrix": sla_matrix,
})
findings = r.json()["findings"]

print(f"HTTP {r.status_code}  |  Total findings: {len(findings)}")
print()

missing_fields = []
for f in findings:
    if f["level"] not in ("critical", "warning"):
        continue
    has_rc = bool(f.get("root_cause"))
    has_im = bool(f.get("impact"))
    has_ac = bool(f.get("recommendation"))
    status = "OK " if (has_rc and has_im and has_ac) else "MISSING"
    if not (has_rc and has_im and has_ac):
        missing_fields.append(f["text"][:60])
    print(f"[{f['level'].upper():<8}] [{status}] {f['text'][:65]}")
    print(f"  root_cause:  {(f.get('root_cause') or '—')[:70]}")
    print(f"  impact:      {(f.get('impact') or '—')[:80]}")
    print(f"  action:      {(f.get('recommendation') or '—')[:80]}")
    print()

print(f"--- {len(missing_fields)} critical/warning findings missing fields ---")
for m in missing_fields:
    print(f"  {m}")
