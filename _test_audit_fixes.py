"""Comprehensive test: verify all audit fixes end-to-end."""
import json, requests

BASE = "http://127.0.0.1:8000"

# ═══════════════════════════════════════════════════════════════
# TEST 1: Upload batch + SLA, verify pattern intelligence
# ═══════════════════════════════════════════════════════════════
print("=" * 60)
print("TEST 1: Pattern Intelligence + Breach Classification")
print("=" * 60)

with open(r"c:\Users\1039081\Downloads\PETBARN PE REVIEW\PETBARN PE REVIEW\Last_15_Days_Report_of_CS_PETBARN_SCPO_DNF_2022_TEST.csv", "rb") as f:
    r = requests.post(f"{BASE}/api/process-batch", files={"file": f})
batch = r.json()

with open(r"c:\Users\1039081\Downloads\PETBARN PE REVIEW\PETBARN PE REVIEW\PETBARN_v2022_BatchSLA_info.xlsx", "rb") as f:
    r2 = requests.post(f"{BASE}/api/batch-sla/upload", files={"file": f})
print(f"SLA upload: {r2.status_code}")

tj = batch.get("top_jobs", [])
print(f"Top jobs: {len(tj)}")

for j in tj[:5]:
    name = j.get("Job_Name", "?")[:35]
    bd = j.get("breach_days", "MISSING")
    ad = j.get("active_days", "MISSING")
    bp = j.get("breach_pattern", "MISSING")
    buf = j.get("buffer_pct", "MISSING")
    bs = j.get("buffer_status", "MISSING")
    print(f"  {name:<35} buf={buf}% status={bs} breach={bd}/{ad} pattern={bp}")

# ═══════════════════════════════════════════════════════════════
# TEST 2: Division-by-zero guard
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 2: Division-by-Zero Guard")
print("=" * 60)

nan_jobs = [j for j in tj if str(j.get("buffer_pct", 0)) in ("nan", "NaN", "None")]
inf_jobs = [j for j in tj if abs(float(j.get("buffer_pct", 0))) > 9999]
print(f"Jobs with NaN buffer_pct: {len(nan_jobs)} {'FAIL' if nan_jobs else 'PASS'}")
print(f"Jobs with inf buffer_pct: {len(inf_jobs)} {'FAIL' if inf_jobs else 'PASS'}")

status_counts = {}
for j in tj:
    s = j.get("buffer_status", "?")
    status_counts[s] = status_counts.get(s, 0) + 1
print(f"Buffer status distribution: {status_counts}")

# ═══════════════════════════════════════════════════════════════
# TEST 3: Findings - no duplicate window compliance findings
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 3: No Duplicate Window Findings")
print("=" * 60)

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

window_findings = [f for f in findings
                   if "window" in (f.get("text", "") + f.get("root_cause", "")).lower()
                   and f.get("level") in ("critical", "warning")]
print(f"Window-related findings: {len(window_findings)}")
for wf in window_findings:
    print(f"  [{wf['level']}] root_cause={wf.get('root_cause','')} | {wf['text'][:80]}")

texts = [f["text"] for f in findings]
dupes = set(t for t in texts if texts.count(t) > 1)
print(f"{'FAIL: Duplicates: ' + str(dupes) if dupes else 'PASS: No duplicate finding texts'}")

# ═══════════════════════════════════════════════════════════════
# TEST 4: R2 has root_cause and evidence
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 4: R2 Root Cause + Evidence Citation")
print("=" * 60)

job_sla_findings = [f for f in findings if "SLA ceiling" in f.get("text", "")]
for jsf in job_sla_findings:
    rc = jsf.get("root_cause", "")
    ev = jsf.get("evidence", "")
    print(f"  [{jsf['level']}] root_cause={'SET' if rc else 'MISSING'} evidence={'SET' if ev else 'MISSING'}")
    print(f"    root_cause={rc}")
    print(f"    evidence={ev}")

# ═══════════════════════════════════════════════════════════════
# TEST 5: Narrative scope line (no "0-day window")
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("TEST 5: Narrative Scope Line")
print("=" * 60)

narrative_finding = next((f for f in findings if f.get("text") == "Audit Narrative"), None)
if narrative_finding:
    narrative = narrative_finding.get("sub", "")
    lines = narrative.split("\n")
    scope_line = lines[0] if lines else ""
    print(f"Scope: {scope_line}")
    print(f"{'FAIL' if '0-day window' in scope_line else 'PASS'}: scope line check")

# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
total = len(findings)
crits = sum(1 for f in findings if f.get("level") == "critical")
warns = sum(1 for f in findings if f.get("level") == "warning")
oks = sum(1 for f in findings if f.get("level") == "ok")
infos = sum(1 for f in findings if f.get("level") == "info")
print(f"Total: {total} | Critical: {crits} | Warning: {warns} | OK: {oks} | Info: {infos}")
print(f"Root causes present: {sum(1 for f in findings if f.get('root_cause'))}/{total}")
print(f"Evidence present: {sum(1 for f in findings if f.get('evidence'))}/{total}")
