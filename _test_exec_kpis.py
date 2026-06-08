"""Test exec dashboard KPIs reflect correct window compliance."""
import requests

r = requests.post("http://127.0.0.1:8000/api/executive-dashboard", json={
    "batch_kpis": {
        "compliance_pct": 15.2,
        "batch_window_compliance": 33.3,
        "window_breach_days": 10,
        "window_total_days": 15,
        "total_jobs": 249,
        "jobs_breach": 0,
        "jobs_at_risk": 0,
        "total_runs": 1814,
        "daily_limit_hrs": 4.0,
        "sla_ceiling": 4.0,
    },
    "top_jobs": [],
    "servers": [],
    "window": [],
    "sla_data": {},
})
d = r.json()
k = d.get("kpis", {})
print(f"batch_rate      = {k.get('batch_rate')}")
print(f"window_compliance = {k.get('window_compliance')}")
print(f"window_breach_days = {k.get('window_breach_days')}")
print(f"oshs_score       = {k.get('oshs_score')}")
print(f"oshs_grade       = {k.get('oshs_grade')}")

# Verify window compliance flows through
assert k.get("window_compliance") == 33.3, f"FAIL: window_compliance should be 33.3, got {k.get('window_compliance')}"
assert k.get("window_breach_days") == 10, f"FAIL: window_breach_days should be 10, got {k.get('window_breach_days')}"
print("\nPASS: Executive KPIs reflect correct window compliance")
