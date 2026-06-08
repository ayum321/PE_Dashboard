"""Upload Michelin PE files to the dashboard and capture all responses."""
import json
import requests

BASE = "http://127.0.0.1:8000"

# 1. Clear session
print("=" * 60)
print("STEP 1: CLEAR SESSION")
print("=" * 60)
r = requests.post(f"{BASE}/api/clear-session")
print(f"  Status: {r.status_code}")

# 2. Upload Batch CSV
print("\n" + "=" * 60)
print("STEP 2: UPLOAD BATCH CSV")
print("=" * 60)
csv_path = r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Last_15_Days_Report_of_CS_MICHELIN_SCPO_FF_2025_PROD.csv"
with open(csv_path, "rb") as f:
    r = requests.post(f"{BASE}/api/process-batch", files={"file": ("batch.csv", f, "text/csv")})
print(f"  Status: {r.status_code}")
batch = r.json()
print(f"  KPIs: compliance={batch.get('kpis',{}).get('compliance_pct')}, "
      f"total_jobs={batch.get('kpis',{}).get('total_jobs')}, "
      f"total_runs={batch.get('kpis',{}).get('total_runs')}, "
      f"breach={batch.get('kpis',{}).get('jobs_breach')}, "
      f"at_risk={batch.get('kpis',{}).get('jobs_at_risk')}, "
      f"fail_rate={batch.get('kpis',{}).get('fail_rate_pct')}")
print(f"  Data coverage: {batch.get('data_coverage', {})}")
print(f"  Window: {batch.get('kpis',{}).get('batch_window_compliance')}")
if batch.get("anomalies"):
    print(f"  Anomalies: {len(batch.get('anomalies',[]))} detected")
    for a in batch.get("anomalies", [])[:3]:
        print(f"    {a.get('Job_Name','?')}: z={a.get('zscore','?')}")

# 3. Upload SLA XLSX
print("\n" + "=" * 60)
print("STEP 3: UPLOAD SLA XLSX")
print("=" * 60)
xlsx_path = r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\BatchSLA_info.xlsx"
with open(xlsx_path, "rb") as f:
    r = requests.post(f"{BASE}/api/batch-sla/upload", files={"file": ("sla.xlsx", f)})
print(f"  Status: {r.status_code}")
sla = r.json()
print(f"  Response keys: {list(sla.keys())}")
if "kpis" in sla:
    print(f"  SLA KPIs: {sla['kpis']}")
if "sla_ceilings" in sla:
    print(f"  SLA Ceilings: {sla['sla_ceilings']}")
if "contracts" in sla:
    print(f"  Contracts: {len(sla.get('contracts',[]))} entries")
    for c in sla.get("contracts", [])[:3]:
        print(f"    {c.get('batch_name','?')}: sla={c.get('sla_hours','?')}h, schedule={c.get('schedule_type','?')}")

# 4. Upload SOW PDF
print("\n" + "=" * 60)
print("STEP 4: UPLOAD SOW PDF")
print("=" * 60)
sow_path = r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Manufacture Française Des Pneumatiques Michelin. Sched 1-A to SaaS PSA. 17Jun2022 (3).pdf"
with open(sow_path, "rb") as f:
    r = requests.post(f"{BASE}/api/sow/parse", files={"file": ("sow.pdf", f)})
print(f"  Status: {r.status_code}")
sow = r.json()
print(f"  Response keys: {list(sow.keys())}")
for k, v in sow.items():
    if isinstance(v, (str, int, float, bool)):
        print(f"    {k}: {v}")
    elif isinstance(v, dict):
        print(f"    {k}: {json.dumps(v)[:200]}")
    elif isinstance(v, list):
        print(f"    {k}: [{len(v)} items]")

# 5. Generate Findings
print("\n" + "=" * 60)
print("STEP 5: GENERATE FINDINGS")
print("=" * 60)
payload = {
    "batch_kpis": batch.get("kpis"),
    "top_jobs": batch.get("top_jobs"),
    "top_breaches": batch.get("top_breaches"),
    "window": batch.get("window"),
    "anomalies": batch.get("anomalies"),
    "sub_stats": batch.get("sub_stats"),
    "customer_name": "Michelin",
}
r = requests.post(f"{BASE}/api/generate-findings", json=payload)
print(f"  Status: {r.status_code}")
findings = r.json()
print(f"  Summary: {findings.get('summary')}")
print(f"  Total findings: {len(findings.get('findings', []))}")
print()
print("  ALL FINDINGS:")
for i, f in enumerate(findings.get("findings", []), 1):
    lvl = f.get("level", "?")
    txt = f.get("text", "")
    sub = f.get("sub", "")[:150]
    src = f.get("source", "")
    rc = f.get("root_cause", "")
    print(f"  {i:2d}. [{lvl:8s}] [{src:10s}] {txt}")
    if sub:
        print(f"      └─ {sub}")
