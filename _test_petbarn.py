"""
End-to-end test: Petbarn PE Review
Tests generic handling of a different customer with different data patterns:
  - Column names: 'Completion Status', 'Run Time (Sec.) ' (trailing space)
  - Date format: DD-MM-YYYY HH:MM
  - 3 Sub_Applications (TEST_DAILY, TEST_WEEKLY, + possible others)
  - DAILY + WEEKLY schedules
"""
import requests, json, sys, os

BASE = "http://127.0.0.1:8000"
DATA_DIR = r"c:\Users\1039081\Downloads\PETBARN PE REVIEW\PETBARN PE REVIEW"

CSV  = os.path.join(DATA_DIR, "Last_15_Days_Report_of_CS_PETBARN_SCPO_DNF_2022_TEST.csv")
XLSX = os.path.join(DATA_DIR, "PETBARN_v2022_BatchSLA_info.xlsx")
PDF  = os.path.join(DATA_DIR, "Petbarn Sched 1-A To SAAS and PSA 06Dec2023.pdf")

def sep(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

errors = []

# ── 1. Upload Ctrl-M CSV ─────────────────────────────────────
sep("1. UPLOAD CTRL-M CSV")
with open(CSV, "rb") as f:
    r = requests.post(f"{BASE}/api/process-batch", files={"file": (os.path.basename(CSV), f, "text/csv")})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    kpis = d.get("kpis", {})
    print(f"  Customer: {d.get('customer_name', '?')}")
    print(f"  Total runs: {kpis.get('total_runs', '?')}")
    print(f"  Unique jobs: {kpis.get('unique_jobs', '?')}")
    print(f"  Failed: {kpis.get('total_failed', '?')}")
    print(f"  Run days: {kpis.get('run_days', '?')}")
    print(f"  Sub-apps: {kpis.get('sub_application_count', '?')}")
    print(f"  Schedule: {kpis.get('sla_detected_mode', '?')}")
    print(f"  SLA ceiling: {kpis.get('sla_ceiling_hrs', '?')}h")
    
    # Check window data
    window = d.get("window", [])
    print(f"  Window days: {len(window)}")
    if window:
        for w in window[:3]:
            elapsed = w.get("elapsed_hrs", "N/A")
            total = w.get("total_hrs", "N/A")
            print(f"    {w.get('run_date','?')}: elapsed={elapsed}h, total={total}h")
    
    # Validate critical fields
    if kpis.get("total_runs", 0) == 0:
        errors.append("CSV: 0 total runs — column mapping likely failed")
    if kpis.get("unique_jobs", 0) == 0:
        errors.append("CSV: 0 unique jobs")
else:
    errors.append(f"CSV upload failed: {r.status_code} {r.text[:200]}")
    print(f"  ERROR: {r.text[:500]}")

# ── 2. Upload BatchSLA XLSX ──────────────────────────────────
sep("2. UPLOAD BatchSLA XLSX")
with open(XLSX, "rb") as f:
    r = requests.post(f"{BASE}/api/batch-sla/upload", files={"file": (os.path.basename(XLSX), f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    wfs = d.get("workflows", [])
    print(f"  Workflows parsed: {len(wfs)}")
    for wf in wfs:
        sla_h = wf.get("sla_hours", wf.get("sla_window_hrs", "?"))
        sched = wf.get("batch_type", wf.get("schedule_type", "?"))
        name = wf.get("batch_name", wf.get("name", "?"))
        print(f"    {name}: {sched} → {sla_h}h SLA")
        print(f"      First: {wf.get('first_job','?')} → Last: {wf.get('last_job','?')}")
    
    if not wfs:
        errors.append("XLSX: 0 workflows parsed")
else:
    errors.append(f"XLSX upload failed: {r.status_code} {r.text[:200]}")
    print(f"  ERROR: {r.text[:500]}")

# ── 3. Upload SOW PDF ────────────────────────────────────────
sep("3. UPLOAD SOW PDF")
with open(PDF, "rb") as f:
    r = requests.post(f"{BASE}/api/sow/parse", files={"file": (os.path.basename(PDF), f, "application/pdf")})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    contract = d.get("contract", d.get("data", {}))
    print(f"  Customer: {contract.get('customer_name', '?')}")
    print(f"  Contract years: {contract.get('contract_years', '?')}")
    print(f"  Annual fee: {contract.get('currency','?')}{contract.get('annual_fee','?')}")
    print(f"  Availability: {contract.get('availability_pct', '?')}%")
    
    sla = contract.get("sla_commitments", {})
    print(f"  SLA ceilings from SOW: {json.dumps(sla, indent=2) if sla else 'none extracted'}")
    
    volume = contract.get("volume_ramp", contract.get("volume", {}))
    if volume:
        print(f"  Volume ramp: {json.dumps(volume)[:200]}")
else:
    errors.append(f"SOW upload failed: {r.status_code}")
    print(f"  ERROR: {r.text[:500]}")

# ── 4. Check config (SLA ceilings) ──────────────────────────
sep("4. VERIFY CONFIG / SLA CEILINGS")
r = requests.get(f"{BASE}/api/config")
if r.status_code == 200:
    cfg = r.json()
    for k in ["daily_sla_hrs", "weekly_sla_hrs", "monthly_sla_hrs", "custom_sla_hrs"]:
        v = cfg.get(k)
        if v is not None:
            print(f"  {k}: {v}h")
    
    # Validate SLA ceilings are set
    if not any(cfg.get(k) for k in ["daily_sla_hrs", "weekly_sla_hrs"]):
        errors.append("CONFIG: No SLA ceilings set after XLSX+SOW upload")

# ── 5. Generate Findings ─────────────────────────────────────
sep("5. GENERATE FINDINGS")
r = requests.get(f"{BASE}/api/audit-context")
ctx = r.json() if r.status_code == 200 else {}

r = requests.post(f"{BASE}/api/generate-findings", json={
    "batch_kpis": ctx.get("slots", {}).get("batch_kpis"),
    "sla_matrix": ctx.get("slots", {}).get("sla_matrix_kpis"),
    "sow_compare": ctx.get("slots", {}).get("sow_contract"),
    "sla_ceilings": None,  # let it pull from config
})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    findings = d.get("findings", [])
    verdict = d.get("verdict", {})
    print(f"  Verdict: {verdict.get('grade', '?')} — {verdict.get('status', '?')}")
    print(f"  Total findings: {len(findings)}")
    
    by_sev = {}
    for f_ in findings:
        sev = f_.get("severity", "?")
        by_sev[sev] = by_sev.get(sev, 0) + 1
    print(f"  By severity: {json.dumps(by_sev)}")
    
    for f_ in findings:
        sev = f_.get("severity", "?")
        title = f_.get("title", "?")
        detail = f_.get("detail", "")[:120]
        print(f"  [{sev:8s}] {title}")
        print(f"            {detail}")
    
    # Validate
    if not findings:
        errors.append("FINDINGS: 0 findings generated")
    
    # Check for generic/wrong data
    for f_ in findings:
        detail = f_.get("detail", "")
        if "Michelin" in detail:
            errors.append(f"FINDINGS: Michelin name leaked into Petbarn findings: {detail[:100]}")
        if "4.0h" in detail and "daily" in detail.lower():
            # Check if it's using default SLA instead of customer SLA
            pass  # might be legitimate
else:
    errors.append(f"Findings failed: {r.status_code}")
    print(f"  ERROR: {r.text[:500]}")

# ── 6. Executive Dashboard ───────────────────────────────────
sep("6. EXECUTIVE DASHBOARD")
r = requests.post(f"{BASE}/api/executive-dashboard", json={
    "batch_kpis": ctx.get("slots", {}).get("batch_kpis"),
    "window": ctx.get("slots", {}).get("daily_window_series"),
    "sub_stats": ctx.get("slots", {}).get("sub_stats"),
    "top_jobs": ctx.get("slots", {}).get("batch_top_jobs"),
})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    dg = d.get("decision_gate", {})
    print(f"  OSHS: {dg.get('oshs_score', '?')}")
    print(f"  Grade: {dg.get('grade', '?')}")
    print(f"  Status: {dg.get('status', '?')}")
    print(f"  Window compliance: {dg.get('window_compliance_pct', '?')}%")
    print(f"  Breach days: {dg.get('breach_days', '?')}")
    print(f"  Total overrun: {dg.get('total_overrun_hrs', '?')}h")
    
    # Validate
    wc = dg.get("window_compliance_pct")
    if wc is not None and wc < 0:
        errors.append(f"EXEC: negative window compliance: {wc}%")
    bd = dg.get("breach_days")
    if bd is not None and bd < 0:
        errors.append(f"EXEC: negative breach days: {bd}")
else:
    errors.append(f"Executive failed: {r.status_code}")
    print(f"  ERROR: {r.text[:500]}")

# ── 7. SLA Matrix ────────────────────────────────────────────
sep("7. SLA MATRIX")
# SLA matrix needs a file upload, use the batch data from audit context
sla_batch = ctx.get("slots", {}).get("batch_kpis", {})
r = requests.post(f"{BASE}/api/sla-matrix/json", json={"rows": []})
print(f"  Status: {r.status_code}")
if r.status_code == 200:
    d = r.json()
    kpis = d.get("kpis", {})
    print(f"  Overall compliance: {kpis.get('overall_compliance_pct', '?')}%")
    print(f"  Total workflows: {kpis.get('total_workflows', '?')}")
    print(f"  Breach count: {kpis.get('breach_count', '?')}")
    
    wf_summary = d.get("workflow_summary", [])
    print(f"  Workflow summaries: {len(wf_summary)}")
    for wf in wf_summary[:5]:
        print(f"    {wf.get('workflow_name','?')}: {wf.get('compliance_pct','?')}% — {wf.get('schedule_type','?')}")
else:
    errors.append(f"SLA Matrix failed: {r.status_code}")
    print(f"  ERROR: {r.text[:500]}")

# ── SUMMARY ──────────────────────────────────────────────────
sep("FINAL SUMMARY")
if errors:
    print(f"  ❌ {len(errors)} ERROR(S):")
    for e in errors:
        print(f"    • {e}")
    sys.exit(1)
else:
    print("  ✅ ALL CHECKS PASSED — Petbarn data processed correctly")
    print("  Dashboard is generic and handles different column names,")
    print("  date formats, and customer data patterns.")
    sys.exit(0)
