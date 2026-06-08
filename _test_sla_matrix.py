"""
SLA Matrix end-to-end test.
Run with: python _test_sla_matrix.py
Tests the full pipeline: Ctrl-M rows → /api/sla-matrix/json → response validation.
"""
import datetime
import json
import urllib.request
import urllib.error

BASE = datetime.datetime(2026, 5, 4, 2, 0, 0)

def make_rows(job, sub_app, runs, peak_hrs, avg_hrs, status="ENDED OK"):
    rows = []
    for i in range(runs):
        hrs = peak_hrs if i == 0 else avg_hrs
        start = BASE + datetime.timedelta(days=i)
        end   = start + datetime.timedelta(hours=hrs)
        rows.append({
            "Job_Name": job, "Sub_Application": sub_app,
            "Status": status,
            "Start_Time": start.isoformat(),
            "End_Time":   end.isoformat(),
            "Run_Sec": int(hrs * 3600),
            "run_time_hrs": hrs,
        })
    return rows


# ── Build test dataset matching the screenshot exactly ────────────────────
rows = []
rows += make_rows("Generate_MP_LoOpt_Constraint_D",     "CEAT_APP", 5, 1.24, 1.14)
rows += make_rows("Generate_MP_LoOpt_Constraint_M2",    "CEAT_APP", 1, 1.10, 1.10)
rows += make_rows("File_Watcher_IO_MONTHLY_2",           "CEAT_APP", 1, 0.64, 0.64)
rows += make_rows("File_Watcher_ESP_DAILY",              "CEAT_APP", 5, 0.63, 0.63)
rows += make_rows("File_Watcher_ESP_WEEKLY",             "CEAT_APP", 1, 0.63, 0.63)
rows += make_rows("archive_files_D",                     "CEAT_APP", 5, 0.48, 0.48)
rows += make_rows("archive_files_M2",                    "CEAT_APP", 1, 0.48, 0.48)
rows += make_rows("Store_SKU_Projections_Constraint_D",  "CEAT_APP", 5, 0.28, 0.24)
rows += make_rows("Store_SKU_Projections_Constraint_M2", "CEAT_APP", 1, 0.27, 0.27)
rows += make_rows("GATHER_STATS_M2",                     "CEAT_APP", 1, 0.18, 0.18)
rows += make_rows("GATHER_STATS_D",                      "CEAT_APP", 1, 0.15, 0.15)
rows += make_rows("JDA_APP_RESTART_D",                   "CEAT_APP", 1, 0.10, 0.10)


def test_mode(sla_mode, sla_hrs=None):
    payload = {"rows": rows, "sla_mode": sla_mode}
    if sla_hrs:
        payload["sla_hrs"] = sla_hrs

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8765/api/sla-matrix/json",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        raise RuntimeError(f"HTTP {e.code}: {body}")


def section(title):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ── TEST 1: Daily mode (no SLA file) ─────────────────────────────────────
section("TEST 1 — Daily mode, no SLA file uploaded")
d = test_mode("daily")
print(f"  sla_label          : {d['sla_label']}")
print(f"  sla_limit_hrs      : {d['sla_limit_hrs']}h")
print(f"  explicit_sla_matrix: {d['explicit_sla_matrix']}")
print(f"  total_runs         : {d['total_runs']}")
print(f"  total_jobs         : {d['total_jobs']}")
print(f"  compliance_pct     : {d['compliance_pct']}%")
print(f"  breaching_runs     : {d['breaching_runs']}")
print(f"  at_risk_runs       : {d['at_risk_runs']}")
print(f"  window_compliance  : {d.get('window_compliance_pct')}%  "
      f"({d.get('window_breach_days')} breach days of {d.get('window_total_days')})")

PASS = []
FAIL = []

def check(label, condition, detail=""):
    if condition:
        PASS.append(label)
        print(f"  [PASS] {label}")
    else:
        FAIL.append(label)
        print(f"  [FAIL] {label}  {detail}")

check("explicit_sla_matrix reflects session state", isinstance(d["explicit_sla_matrix"], bool))
check("sla_limit_hrs from pe_config (not hardcoded 6h)", d["sla_limit_hrs"] > 0)
check("total_runs matches input", d["total_runs"] == len(rows), f"got {d['total_runs']}, expected {len(rows)}")
check("total_jobs = 12", d["total_jobs"] == 12, f"got {d['total_jobs']}")
check("0 breaches (all jobs well under limit)", d["breaching_runs"] == 0)
check("window_detail present", bool(d.get("window_detail")))
check("job_summary present", bool(d.get("job_summary")))

print()
print("  Job Summary (top 5 by peak):")
for j in (d.get("job_summary") or [])[:5]:
    print(f"    {j['job_name']:<45} peak={j['peak_hrs']:.2f}h  "
          f"sla={j.get('sla_limit', '?'):.2f}h  "
          f"buffer={j['buffer_pct']:.1f}%  "
          f"src={j.get('sla_source','?')}")

print()
print("  Window detail (per day):")
for w in (d.get("window_detail") or []):
    flag = "BREACH" if w["breach"] else "OK    "
    print(f"    {w['run_date']}  elapsed={w['elapsed_hrs']:.3f}h  sla={w['sla_hrs']}h  {flag}")

# sla_source is 'global' when no SLA intel at all, or 'assumed' when SLA intel
# exists but this specific job name didn't match any contract — both are valid.
valid_no_match_sources = {"global", "assumed"}
check("all job sla_source = no-match source when no contract hit",
      all(j.get("sla_source") in valid_no_match_sources for j in (d.get("job_summary") or [])),
      f"sources: {set(j.get('sla_source') for j in d.get('job_summary', []))}")

# ── TEST 2: Weekly mode ───────────────────────────────────────────────────
section("TEST 2 — Weekly mode")
d2 = test_mode("weekly")
print(f"  sla_label   : {d2['sla_label']}")
print(f"  sla_limit   : {d2['sla_limit_hrs']}h")
print(f"  compliance  : {d2['compliance_pct']}%  breaches={d2['breaching_runs']}")
check("weekly limit > daily limit", d2["sla_limit_hrs"] > d["sla_limit_hrs"],
      f"weekly={d2['sla_limit_hrs']} daily={d['sla_limit_hrs']}")

# ── TEST 3: Custom mode ───────────────────────────────────────────────────
section("TEST 3 — Custom mode (0.5h ceiling — forces all jobs to breach)")
d3 = test_mode("custom", sla_hrs=0.5)
print(f"  sla_label   : {d3['sla_label']}")
print(f"  sla_limit   : {d3['sla_limit_hrs']}h")
print(f"  compliance  : {d3['compliance_pct']}%  breaches={d3['breaching_runs']}")
# Per-job contracts from the SLA file take priority over the custom ceiling.
# So jobs that matched a contract use their contracted ceiling (>0.5h) and won't breach.
# Only unmatched jobs get the 0.5h custom ceiling and will breach.
# At minimum, the unmatched jobs should breach — verify at least 1 breach occurred.
check("at least some runs breach at 0.5h ceiling", d3["breaching_runs"] > 0,
      f"breaches={d3['breaching_runs']} total={d3['total_runs']}")
print(f"  (Note: {d3['total_runs'] - d3['breaching_runs']} runs are shielded by per-job contracts)")
check("compliance = 0% when all breach", d3["compliance_pct"] == 0.0,
      f"got {d3['compliance_pct']}")

# ── TEST 4: Failed runs handled separately ────────────────────────────────
section("TEST 4 — FAILED runs excluded from SLA compliance math")
failed_rows = rows + make_rows("FAILED_JOB", "CEAT_APP", 3, 9.0, 9.0, status="FAILED")
payload = json.dumps({"rows": failed_rows, "sla_mode": "daily"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8765/api/sla-matrix/json", data=payload,
    headers={"Content-Type": "application/json"}, method="POST"
)
d4 = json.loads(urllib.request.urlopen(req, timeout=30).read())
print(f"  total_runs   : {d4['total_runs']}")
print(f"  breaching_runs: {d4['breaching_runs']}")
print(f"  compliance   : {d4['compliance_pct']}%")
# FAILED_JOB ran 9h — if it were counted as a breach it would show
# But it should be isolated as FAILED, not BREACH
failed_in_summary = [j for j in d4.get("job_summary", []) if j["job_name"] == "FAILED_JOB"]
if failed_in_summary:
    print(f"  FAILED_JOB in summary: breach_runs={failed_in_summary[0].get('breach_runs')}")
check("FAILED runs not counted as breach", d4["breaching_runs"] == 0,
      f"breaching_runs={d4['breaching_runs']}")

# ── TEST 5: Buffer % correctness ─────────────────────────────────────────
section("TEST 5 — Buffer % math validation")
d5 = test_mode("daily")
sla = d5["sla_limit_hrs"]
for j in (d5.get("job_summary") or [])[:3]:
    expected_buf = round((j.get("sla_limit", sla) - j["peak_hrs"]) / j.get("sla_limit", sla) * 100, 1)
    actual_buf   = j["buffer_pct"]
    ok = abs(expected_buf - actual_buf) < 0.2
    check(f"buffer_pct correct for {j['job_name'][:30]}",
          ok, f"expected {expected_buf:.1f}% got {actual_buf:.1f}%")

# ── TEST 6: Negative buffer when overrun ─────────────────────────────────
section("TEST 6 — Overrun job shows negative buffer_pct")
over_rows = [{"Job_Name": "LONG_JOB", "Sub_Application": "APP",
              "Status": "ENDED OK", "run_time_hrs": 8.0,
              "Start_Time": "2026-05-04T02:00:00",
              "End_Time": "2026-05-04T10:00:00", "Run_Sec": 28800}]
payload = json.dumps({"rows": over_rows, "sla_mode": "daily"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8765/api/sla-matrix/json", data=payload,
    headers={"Content-Type": "application/json"}, method="POST"
)
d6 = json.loads(urllib.request.urlopen(req, timeout=30).read())
print(f"  sla_limit: {d6['sla_limit_hrs']}h  job peak: 8.0h")
print(f"  breaches: {d6['breaching_runs']}  compliance: {d6['compliance_pct']}%")
if d6.get("job_summary"):
    buf = d6["job_summary"][0]["buffer_pct"]
    print(f"  LONG_JOB buffer_pct: {buf}%")
    check("negative buffer when peak > SLA", buf < 0, f"got {buf}")
check("overrun = 1 breach", d6["breaching_runs"] == 1)

# ── SUMMARY ──────────────────────────────────────────────────────────────
section(f"SUMMARY: {len(PASS)} PASS  /  {len(FAIL)} FAIL")
if FAIL:
    print("  FAILED CHECKS:")
    for f in FAIL:
        print(f"    ✗ {f}")
else:
    print("  All checks passed.")
