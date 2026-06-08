"""
PE Architect Audit v2 — Michelin Customer
Correct endpoints, deep analysis.
"""
import requests, json, os

BASE = "http://127.0.0.1:8000"

def upload(path, endpoint, field="file"):
    if not os.path.exists(path):
        return {"error": f"FILE NOT FOUND: {path}"}
    with open(path, "rb") as f:
        try:
            r = requests.post(BASE + endpoint, files={field: (os.path.basename(path), f)}, timeout=120)
            return {"status": r.status_code, "data": r.json()}
        except Exception as e:
            return {"error": str(e)}

def section(title):
    print(f"\n{'=' * 90}")
    print(f"  {title}")
    print(f"{'=' * 90}")

def main():
    # 0. Clear
    section("STEP 0: CLEAR SESSION")
    requests.post(f"{BASE}/api/clear-session")
    print("  Session cleared")

    # 1. BATCH XLSX
    section("STEP 1: BATCH XLSX -> /api/process-batch")
    res = upload(r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Michelin_Batch_performance.xlsx", "/api/process-batch")
    if "data" in res:
        d = res["data"]; kpis = d.get("kpis", {})
        print(f"  HTTP {res['status']}")
        print(f"  Customer:          {d.get('customer_name')}")
        print(f"  Total jobs:        {kpis.get('total_jobs')}")
        print(f"  Total runs:        {kpis.get('total_runs')}")
        print(f"  Days:              {kpis.get('days_count')}")
        print(f"  SLA source:        {kpis.get('sla_source')}")
        print(f"  Daily compliance:  {kpis.get('daily_compliance_pct')}%")
        print(f"  Window compliance: {kpis.get('window_compliance_pct')}%")
        print(f"  Failure rate:      {kpis.get('failure_rate_pct')}%")
        print(f"  Peak job:          {kpis.get('peak_job_name')} -- {kpis.get('peak_run_sec')}s")
        print(f"  Job summaries:     {len(d.get('job_summary', []))}")
        for k in d:
            if k not in ("kpis", "job_summary", "customer_name"):
                val = d[k]
                if isinstance(val, list):
                    print(f"  {k}: {len(val)} items")
                elif isinstance(val, dict):
                    print(f"  {k}: {list(val.keys())[:8]}")
    else:
        print(f"  ERROR: {res.get('error')}")

    # 2. BATCH CSV
    section("STEP 2: BATCH CSV -> /api/process-batch")
    res = upload(r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Last_15_Days_Report_of_CS_MICHELIN_SCPO_FF_2025_PROD.csv", "/api/process-batch")
    if "data" in res:
        d = res["data"]; kpis = d.get("kpis", {})
        print(f"  HTTP {res['status']}")
        print(f"  Customer:          {d.get('customer_name')}")
        print(f"  Total jobs:        {kpis.get('total_jobs')}")
        print(f"  Total runs:        {kpis.get('total_runs')}")
        print(f"  Days:              {kpis.get('days_count')}")
        print(f"  Daily compliance:  {kpis.get('daily_compliance_pct')}%")
        print(f"  Window compliance: {kpis.get('window_compliance_pct')}%")
        print(f"  Failure rate:      {kpis.get('failure_rate_pct')}%")
        print(f"  Job summaries:     {len(d.get('job_summary', []))}")
    else:
        print(f"  ERROR: {res.get('error')}")

    # 3. BATCH SLA INFO
    section("STEP 3: BatchSLA_info.xlsx -> /api/batch-sla/upload")
    res = upload(r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\BatchSLA_info.xlsx", "/api/batch-sla/upload")
    if "data" in res:
        d = res["data"]
        print(f"  HTTP {res['status']}")
        print(f"  Keys: {list(d.keys())}")
        print(f"  Response: {json.dumps(d, indent=2, default=str)[:600]}")
    else:
        print(f"  ERROR: {res.get('error')}")

    # 4. SOW PDF
    section("STEP 4: SOW PDF -> /api/sow/parse")
    res = upload(r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Manufacture Fran\u00e7aise Des Pneumatiques Michelin. Sched 1-A to SaaS PSA. 17Jun2022 (3).pdf", "/api/sow/parse")
    if "data" in res:
        d = res["data"]
        print(f"  HTTP {res['status']}")
        print(f"  Keys: {list(d.keys())}")
        print(f"  Response: {json.dumps(d, indent=2, default=str)[:800]}")
    else:
        print(f"  ERROR: {res.get('error')}")

    # 5. FINDINGS
    section("STEP 5: FINDINGS -> POST /api/generate-findings")
    try:
        r = requests.post(f"{BASE}/api/generate-findings", timeout=60)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        verdict = d.get("verdict", {})
        print(f"  VERDICT:     {verdict.get('verdict', 'NONE')}")
        print(f"  Confidence:  {verdict.get('confidence', '?')}")
        print(f"  Headline:    {verdict.get('headline', '?')}")
        findings = d.get("findings", [])
        print(f"  Total findings: {len(findings)}")
        sev = {}
        for f in findings:
            s = f.get("severity", "?")
            sev[s] = sev.get(s, 0) + 1
        print(f"  By severity: {sev}")
        for f in findings:
            marker = "XX" if f.get("severity") == "critical" else "WW" if f.get("severity") == "warning" else "II" if f.get("severity") == "info" else "  "
            print(f"    {marker} [{f.get('id','')}] {f.get('severity','').upper():8s} {f.get('title','')[:80]}")
        narr = d.get("narrative", {})
        if narr:
            print(f"\n  NARRATIVE:")
            for k in ("scope", "compliance", "rca", "impact", "evidence", "decision", "worst"):
                v = narr.get(k, "")
                if v:
                    print(f"    {k}: {v[:200]}")
        gaps = d.get("open_gaps", [])
        if gaps:
            print(f"\n  OPEN AUDIT GAPS ({len(gaps)}):")
            for g in gaps:
                print(f"    GAP: {g.get('missing','?')} -- {g.get('impact','?')}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # 6. EXECUTIVE
    section("STEP 6: EXECUTIVE -> POST /api/executive-dashboard")
    try:
        r = requests.post(f"{BASE}/api/executive-dashboard", timeout=60)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            print(f"  Keys: {list(d.keys())}")
            for k in ("formulas", "verdict", "risk_tier", "go_nogo"):
                if k in d:
                    print(f"  {k}: {json.dumps(d[k], default=str)[:200]}")
        else:
            print(f"  Detail: {d.get('detail', d)}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # 7. NARRATIVE
    section("STEP 7: PE NARRATIVE -> POST /api/pe-narrative")
    try:
        r = requests.post(f"{BASE}/api/pe-narrative", json={}, timeout=60)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        if r.status_code == 200:
            sections = d.get("sections", [])
            if isinstance(sections, list):
                for s in sections:
                    print(f"\n  [{s.get('title','?')}]")
                    print(f"  {s.get('body', s.get('content',''))[:300]}")
            else:
                print(f"  {json.dumps(d, default=str)[:500]}")
        else:
            print(f"  Detail: {d.get('detail', d)}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # 8. SOW COMPARE
    section("STEP 8: SOW COMPARE -> POST /api/sow/compare")
    try:
        r = requests.post(f"{BASE}/api/sow/compare", json={}, timeout=30)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        print(f"  {json.dumps(d, indent=2, default=str)[:500]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # 9. CORRELATE
    section("STEP 9: CROSS-CORRELATION -> POST /api/correlate")
    try:
        r = requests.post(f"{BASE}/api/correlate", json={}, timeout=30)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        print(f"  {json.dumps(d, indent=2, default=str)[:500]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    # 10. AUDIT CONTEXT
    section("STEP 10: AUDIT CONTEXT -> GET /api/audit-context")
    try:
        r = requests.get(f"{BASE}/api/audit-context", timeout=15)
        d = r.json()
        print(f"  HTTP {r.status_code}")
        print(f"  {json.dumps(d, indent=2, default=str)[:800]}")
    except Exception as e:
        print(f"  ERROR: {e}")

    print(f"\n{'=' * 90}")
    print("  AUDIT COMPLETE")
    print(f"{'=' * 90}")

if __name__ == "__main__":
    main()
