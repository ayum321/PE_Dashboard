"""Comprehensive audit check — verify data accuracy across all layers."""
import requests
import json

BASE = "http://127.0.0.1:8000"

# 1. Config store SLA values
r = requests.get(f"{BASE}/api/config")
cfg = r.json()
print("=" * 60)
print("1. CONFIG STORE SLA VALUES")
print("=" * 60)
for k in ["daily_sla_hrs", "weekly_sla_hrs", "monthly_sla_hrs", "custom_sla_hrs"]:
    print(f"  {k}: {cfg.get(k)}")

# 2. Batch calculator output
r2 = requests.get(f"{BASE}/api/audit-context")
ac = r2.json()
lb = ac.get("last_batch", {})
kpis = lb.get("kpis", {})
print()
print("=" * 60)
print("2. BATCH CALCULATOR KPIs")
print("=" * 60)
print(f"  sla_ceiling:           {kpis.get('sla_ceiling')}")
print(f"  batch_type:            {kpis.get('batch_type')}")
print(f"  batch_window_comp:     {kpis.get('batch_window_compliance')}")
print(f"  window_breach_days:    {kpis.get('window_breach_days')}")
print(f"  window_total_days:     {kpis.get('window_total_days')}")
print(f"  compliance_pct:        {kpis.get('compliance_pct')}")
print(f"  total_jobs:            {kpis.get('total_jobs')}")
print(f"  total_runs:            {kpis.get('total_runs')}")
print(f"  fail_rate:             {kpis.get('fail_rate_pct')}")

# 3. SLA intelligence workflows
print()
print("=" * 60)
print("3. SLA INTELLIGENCE WORKFLOWS")
print("=" * 60)
try:
    from services import config_store
    intel = config_store.get("_sla_intelligence") or {}
    for w in (intel.get("workflows") or intel.get("contracts") or []):
        bn = w.get("batch_name", w.get("batch", "?"))
        sla_h = w.get("sla_hours")
        sched = w.get("schedule_type") or w.get("schedule")
        first = w.get("first_job")
        last = w.get("last_job")
        print(f"  {bn}: sla={sla_h}h, sched={sched}, first={first}, last={last}")
except Exception as e:
    print(f"  Error: {e}")

# 4. Full findings breakdown
print()
print("=" * 60)
print("4. FINDINGS QUALITY AUDIT")
print("=" * 60)
payload = {
    "batch_kpis": kpis,
    "top_jobs": lb.get("top_jobs", []),
    "top_breaches": lb.get("top_breaches", []),
    "window": lb.get("window", []),
    "anomalies": lb.get("anomalies", []),
    "sub_stats": lb.get("sub_stats", []),
    "customer_name": "Michelin",
}
r3 = requests.post(f"{BASE}/api/generate-findings", json=payload)
findings = r3.json()

crit = findings.get("summary", {}).get("critical", 0)
warn = findings.get("summary", {}).get("warning", 0)
info = findings.get("summary", {}).get("info", 0)
ok_n = findings.get("summary", {}).get("ok", 0)
total = findings.get("summary", {}).get("total", 0)
print(f"  Summary: {crit} critical, {warn} warning, {info} info, {ok_n} ok = {total} total")
print()

# Quality checks
issues = []
seen_root_causes = {}
for i, f in enumerate(findings.get("findings", []), 1):
    lvl = f.get("level", "?")
    txt = f.get("text", "")
    sub = f.get("sub", "")
    src = f.get("source", "")
    rc = f.get("root_cause", "")
    ec = f.get("evidence_class", "")

    # Check for duplicates
    key = (rc, lvl)
    if rc and key in seen_root_causes:
        issues.append(f"  DUPLICATE: #{i} '{txt[:60]}' shares root_cause='{rc}' with #{seen_root_causes[key]}")
    elif rc:
        seen_root_causes[key] = i

    # Check for misleading text
    if "daily" in txt.lower() and "weekly" in sub.lower():
        issues.append(f"  MISMATCH: #{i} says 'daily' in title but 'weekly' in sub")
    if "assumed" in sub.lower() and "From SLA" in sub:
        issues.append(f"  CONTRADICTION: #{i} says both 'assumed' and 'From SLA'")
    if ec == "defaulted" and "From SLA" in sub:
        issues.append(f"  EVIDENCE-CLASS: #{i} evidence_class=defaulted but says 'From SLA'")

    # Print finding
    status_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵", "ok": "🟢"}.get(lvl, "⚪")
    print(f"  {status_icon} #{i:2d} [{lvl:8s}] {txt}")
    if sub:
        print(f"           {sub[:140]}")

if issues:
    print()
    print("  ⚠️  QUALITY ISSUES DETECTED:")
    for iss in issues:
        print(f"    {iss}")
else:
    print()
    print("  ✅ No quality issues detected — all findings are clean.")
