"""Quick validation of Universal Intelligence Engine changes."""
import pandas as pd
from services.batch_calculator import build_batch_payload

# ── Test 1: ENDED NOT OK isolation ─────────────────────────────
# JOB_A has OK runs (2-5h) and one FAILED run with Run_Sec=0.
# Without the fix, End-Start fallback would inflate it to 19h.
data = {
    "Job_Name":        ["JOB_A","JOB_A","JOB_A","JOB_B","JOB_B"],
    "Sub_Application": ["BILLING"]*5,
    "Status":          ["OK","OK","FAILED","OK","OK"],
    "Run_Sec":         [7200, 18000, 0, 3600, 5400],
    "Start_Time":      pd.to_datetime([
        "2024-01-01 20:00","2024-01-02 20:00","2024-01-03 20:00",
        "2024-01-01 21:00","2024-01-02 21:00"
    ]),
    "End_Time":        pd.to_datetime([
        "2024-01-01 22:00","2024-01-02 01:00","2024-01-04 15:00",
        "2024-01-01 22:00","2024-01-02 22:30"
    ]),
}
df = pd.DataFrame(data)
df["run_time_hrs"] = df["Run_Sec"] / 3600.0
df["run_date"]     = df["Start_Time"].dt.date
df["month"]        = df["Start_Time"].dt.to_period("M").astype(str)
df["Hour_Bucket"]  = df["Start_Time"].dt.hour

payload = build_batch_payload(df)

print("=== Test 1: ENDED NOT OK Isolation ===")
for j in payload["top_jobs"]:
    peak = j["peak_hrs"]
    fc   = j.get("fail_count", 0)
    print(f"  {j['Job_Name']}: peak={peak}h  fail_count={fc}")
    if j["Job_Name"] == "JOB_A":
        assert peak <= 5.1, f"JOB_A peak should be ~5h (not 19h phantom). Got {peak}"
        assert fc == 1, f"JOB_A fail_count should be 1. Got {fc}"
print("  PASS\n")

# ── Test 2: Hourly counts in payload ───────────────────────────
print("=== Test 2: Hourly Counts for Temporal ===")
hc = payload.get("hourly_counts", {})
assert "hourly_jobs" in hc, "Missing hourly_jobs"
assert "hourly_fails" in hc, "Missing hourly_fails"
total_jobs = sum(hc["hourly_jobs"].values())
total_fails = sum(hc["hourly_fails"].values())
print(f"  Total jobs across hours: {total_jobs}")
print(f"  Total fails across hours: {total_fails}")
assert total_jobs == 5, f"Expected 5 total jobs, got {total_jobs}"
assert total_fails == 1, f"Expected 1 fail, got {total_fails}"
print("  PASS\n")

# ── Test 3: Dynamic SLA limits ─────────────────────────────────
print("=== Test 3: Dynamic SLA Limits ===")
daily_lim = payload["kpis"]["daily_limit_hrs"]
print(f"  daily_limit_hrs = {daily_lim}")
assert daily_lim > 0, "Daily limit should be positive"
print("  PASS\n")

# ── Test 4: SRE Classification ─────────────────────────────────
from services.resource_parser import _infer_server_type
print("=== Test 4: SRE Classification ===")
assert _infer_server_type("sre-app-01") == "SRE", "hostname SRE failed"
assert _infer_server_type("tsbb1530") == "SRE", "JDA SRE range failed"
assert _infer_server_type("server01", doc_section_hint="SRE App") == "SRE", "doc hint SRE failed"
assert _infer_server_type("oracle-db-01") == "DB", "DB detection failed"
assert _infer_server_type("webserver01") == "APP", "APP default failed"
print("  All SRE classifications correct")
print("  PASS\n")

# ── Test 5: Dynamic Thresholds ─────────────────────────────────
from services.resource_calculator import compute_dynamic_thresholds, normalize_server
print("=== Test 5: Dynamic Thresholds ===")
servers = [
    {"cpu_used": v, "mem_used": v+10, "disk_used_max": v-10, "mem_total_gb": 16, "disks": {"/": v-10}}
    for v in [40, 50, 55, 60, 45, 90]
]
dt = compute_dynamic_thresholds(servers)
print(f"  Source: {dt['source']}")
print(f"  CPU warn={dt['cpu']['warn']} crit={dt['cpu']['crit']}")
assert dt["source"] == "dynamic", "Should use dynamic thresholds for 6 servers"
assert dt["cpu"]["crit"] > dt["cpu"]["warn"], "Critical should be > warning"
print("  PASS\n")

# ── Test 6: Disk N/A for image-only ───────────────────────────
print("=== Test 6: Disk N/A for Image-Only ===")
img_server = {"host": "unknown01", "cpu_used": 0, "mem_used": 0, "disk_used_max": 0, "mem_total_gb": 0}
row = normalize_server(img_server)
print(f"  image_only={row['image_only']}, disk_pct={row['disk_pct']}")
assert row["image_only"] is True, "Should be image_only"
assert row["disk_pct"] is None, "Disk should be None for image-only"
real_server = {"host": "app01", "cpu_used": 50, "mem_used": 60, "disk_used_max": 40, "mem_total_gb": 16, "disks": {"/": 40}}
real_row = normalize_server(real_server)
assert real_row["disk_pct"] == 40.0, f"Real server disk should be 40.0, got {real_row['disk_pct']}"
print("  PASS\n")

# ── Test 7: Executive endpoint with real hourly data ───────────
from fastapi.testclient import TestClient
from main import app
print("=== Test 7: Executive Endpoint ===")
client = TestClient(app)
exec_payload = {
    "batch_kpis": {"compliance_pct": 92.5, "total_jobs": 25, "daily_limit_hrs": 6.0,
                   "jobs_breach": 3, "jobs_at_risk": 4},
    "top_jobs": [
        {"Sub_Application": "BILLING", "Job_Name": "JOB_X", "peak_hrs": 5.5, "buffer_pct": 8.3},
    ],
    "servers": [
        {"host": "app01", "cpu_used": 65, "mem_used": 72, "disk_used_max": 40},
        {"host": "db01", "cpu_used": 92, "mem_used": 88, "disk_used_max": 75},
    ],
    "sla_data": {"compliance_pct": 88.0, "breaching_runs": 5, "total_runs": 100},
    "window": [{"run_date": "2024-01-01", "total_hrs": 48, "job_count": 25}],
    "hourly_counts": {
        "hourly_jobs": {str(h): 15 if (h >= 20 or h <= 4) else 3 for h in range(24)},
        "hourly_fails": {str(h): 2 if (h >= 20 or h <= 4) else 0 for h in range(24)},
    },
}
res = client.post("/api/executive-dashboard", json=exec_payload)
assert res.status_code == 200, f"Expected 200, got {res.status_code}"
d = res.json()
temporal = d["temporal"]
hours_with_data = sum(1 for t in temporal if t["jobs"] > 0)
print(f"  OSHS: {d['oshs']['score']}/{d['oshs']['grade']}")
print(f"  Temporal hours with data: {hours_with_data}")
assert hours_with_data == 24, f"Should have data for all 24 hours, got {hours_with_data}"
# Verify real data used (not fallback heuristic)
h20 = next(t for t in temporal if t["hour"] == 20)
h12 = next(t for t in temporal if t["hour"] == 12)
assert h20["jobs"] > h12["jobs"], "Hour 20 should have more jobs than hour 12"
print(f"  h20 jobs={h20['jobs']}, h12 jobs={h12['jobs']} — correct temporal distribution")
print("  PASS\n")

print("=" * 50)
print("ALL 7 TESTS PASSED — Universal Intelligence Engine OK")
