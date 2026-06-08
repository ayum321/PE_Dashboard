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
print(f"HTTP {r.status_code}")
verdict = d.get("verdict", {})
print(f"VERDICT:    {verdict.get('verdict', 'NONE')}")
print(f"Confidence: {verdict.get('confidence', '?')}")
print(f"Headline:   {verdict.get('headline', '?')}")

findings = d.get("findings", [])
print(f"Total findings: {len(findings)}")
sev = {}
for f in findings:
    s = f.get("severity", "?")
    sev[s] = sev.get(s, 0) + 1
print(f"By severity: {sev}")
print()

for f in findings:
    smark = {"critical":"[CRIT]","warning":"[WARN]","info":"[INFO]"}.get(f.get("severity",""),"[    ]")
    print(f"{smark} {f.get('id',''):12s} {f.get('title','')[:75]}")

narr = d.get("narrative", {})
if narr:
    print()
    print("=== NARRATIVE ===")
    for k in ("scope","compliance","rca","impact","evidence","decision","worst"):
        v = narr.get(k, "")
        if v:
            print(f"  {k}: {v[:250]}")

gaps = d.get("open_gaps", [])
if gaps:
    print()
    print(f"=== OPEN AUDIT GAPS ({len(gaps)}) ===")
    for g in gaps:
        print(f"  MISSING: {g.get('missing','?')} -- IMPACT: {g.get('impact','?')}")
