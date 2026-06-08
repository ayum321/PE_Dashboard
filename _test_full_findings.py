import requests, json
BASE = "http://127.0.0.1:8000"
ac = requests.get(f"{BASE}/api/audit-context").json()["slots"]
payload = {
    "batch_kpis": ac.get("batch_kpis", {}),
    "top_jobs": ac.get("batch_top_jobs", []),
    "top_breaches": [],
    "window": ac.get("daily_window_series", []),
    "anomalies": ac.get("regression_df", []),
    "sub_stats": [],
    "resource_kpis": {},
    "servers": [],
    "sla_ceilings": {},
    "issues": [],
    "sla_matrix": ac.get("sla_matrix_kpis", {}),
    "benchmark": {},
    "sow_compare": {},
    "customer_name": ac.get("customer_name", ""),
    "sla_triage": {},
    "deep_dive": {}
}
r = requests.post(f"{BASE}/api/generate-findings", json=payload, timeout=60)
d = r.json()
findings = d.get("findings", [])
print(f"TOTAL FINDINGS: {len(findings)}")
levels = {}
for f in findings:
    l = f.get("level", "?")
    levels[l] = levels.get(l, 0) + 1
print(f"By level: {levels}")
print()
for i, f in enumerate(findings):
    lv = f.get("level", "?").upper()
    txt = f.get("text", "")[:100]
    src = f.get("source", "")
    print(f"  {i+1:2d}. [{lv:8s}] [{src:8s}] {txt}")
print()
verdict = d.get("verdict", {})
print("VERDICT:", json.dumps(verdict, indent=2, default=str))
print()
gaps = d.get("open_gaps", d.get("gaps", []))
print(f"OPEN GAPS: {len(gaps)}")
for g in gaps:
    print(f"  - {json.dumps(g, default=str)[:200]}")
narr = d.get("narrative", {})
if narr:
    print()
    print("NARRATIVE:")
    for k, v in narr.items():
        if v:
            print(f"  {k}: {str(v)[:200]}")
cov = d.get("coverage", d.get("coverage_strip", []))
if cov:
    print()
    print("COVERAGE STRIP:")
    for c in cov:
        print(f"  {json.dumps(c, default=str)[:200]}")
# Full response keys
print()
print("ALL RESPONSE KEYS:", list(d.keys()))
