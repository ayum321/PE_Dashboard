"""Quick test: verify breach_pattern fields flow through findings."""
import json, requests

BASE = "http://127.0.0.1:8000"

# Upload batch
with open(r"c:\Users\1039081\Downloads\PETBARN PE REVIEW\PETBARN PE REVIEW\Last_15_Days_Report_of_CS_PETBARN_SCPO_DNF_2022_TEST.csv", "rb") as f:
    r = requests.post(f"{BASE}/api/process-batch", files={"file": f})
batch = r.json()

# Upload SLA
with open(r"c:\Users\1039081\Downloads\PETBARN PE REVIEW\PETBARN PE REVIEW\PETBARN_v2022_BatchSLA_info.xlsx", "rb") as f:
    r2 = requests.post(f"{BASE}/api/batch-sla/upload", files={"file": f})
print("SLA upload:", r2.status_code)

tj = batch.get("top_jobs", [])
print(f"top_jobs count: {len(tj)}")

# Show breach pattern for first 10 jobs
print("\n--- Breach Pattern Intelligence ---")
for j in tj[:10]:
    name = j.get("Job_Name", "?")[:35]
    bd = j.get("breach_days", 0)
    ad = j.get("active_days", 0)
    bp = j.get("breach_pattern", "?")
    peak = j.get("peak_hrs", 0)
    sla = j.get("sla_hrs", 0)
    buf = j.get("buffer_pct", 0)
    print(f"  {name:<35} peak={peak:.2f}h sla={sla:.1f}h buf={buf:.0f}% breach_days={bd}/{ad} pattern={bp}")

# Generate findings with this data
payload = {
    "batch_kpis": batch.get("kpis"),
    "top_jobs": tj,
    "top_breaches": batch.get("top_breaches"),
    "window": batch.get("window"),
    "anomalies": batch.get("anomalies"),
    "sub_stats": batch.get("sub_stats"),
}
r3 = requests.post(f"{BASE}/api/generate-findings", json=payload)
data = r3.json()
findings = data.get("findings", [])
print(f"\nTotal findings: {len(findings)}")

# Show critical findings with pattern info
crits = [f for f in findings if f.get("level") == "critical"]
warns = [f for f in findings if f.get("level") == "warning"]
print(f"Critical: {len(crits)}, Warning: {len(warns)}")

for f in crits:
    print(f"\n  [CRITICAL] {f['text']}")
    sub = (f.get("sub") or "")[:250]
    if sub:
        print(f"    {sub}")
    if f.get("impact"):
        print(f"    Impact: {f['impact']}")
    if f.get("recommendation"):
        print(f"    Rec: {f['recommendation']}")
