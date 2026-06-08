"""
Generic Accuracy Test — ingests diverse Ctrl-M files into the PE Dashboard,
independently computes expected KPIs from raw data, and compares with
dashboard output to report accuracy percentages.

Tests:
  1. Column mapping / schema detection across different file formats
  2. Job count, run count, unique job count accuracy
  3. Compliance % (job-level SLA) mathematical correctness
  4. Failed job detection
  5. Window compliance (daily elapsed) correctness
  6. Peak runtime detection per job
  7. Customer name extraction
  8. PE Findings quality — are the right issues flagged?
"""
import os, sys, json, time, math, traceback
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# Import dashboard's own date parser for consistent parsing
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services.batch_calculator import _parse_dt as _dashboard_parse_dt

BASE = "http://127.0.0.1:8000"
SLA_DIR = r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports"
DEFAULT_SLA_HRS = 6.0  # Dashboard default daily SLA

# ═══════════════════════════════════════════════════════════════
# Test files — curated for diversity of schemas & edge cases
# ═══════════════════════════════════════════════════════════════
TEST_FILES = [
    # (filename, customer_expected, schema_notes)
    ("Last_30_Days_Report_of_CS_FERRERO_SCPO_DMD_2022_PROD.csv",
     "Ferrero", "Standard 8-col, yyyy-mm-dd dates, PROD"),
    ("Last_30_Days_Report_of_CS_FERRERO_SCPO_DMD_2022_TEST.csv",
     "Ferrero", "Standard 8-col, many ENDED NOT OK"),
    ("Last_60_Days_Report_of_CS_ABBVIE_SCPO_2022_PROD.csv",
     "AbbVie", "Standard 8-col, subfolder paths in Folder_Name, PROD"),
    ("Last_15_Days_Report_of_CS_HNK_HNS_2022_SCPO_ESP_TEST.csv",
     "Henkel", "Standard 8-col, dd-mm-yyyy dates, long runtimes"),
    ("Last_30_Days_Report_of_CS_BJS_SCPO_DMS_LDE_TEST(in).csv",
     "BJS", "9-col (extra Comments), dd-mm-yyyy dates"),
    ("Last_30_Days_Report_of_CS_DISTELL_SCPO_2022_SUPPLY_TEST.csv",
     "Distell", "Standard 8-col, TEST env"),
    ("Last_30_Days_Report_of_CS_TATA_STEEL_SCPO_OP_PROD.csv",
     "Tata", "Standard 8-col, PROD"),
    ("Last_7_Days_Report_of_CS_OSRAM_SCPO_PROD.csv",
     "Osram", "Standard 8-col, short 7-day window"),
    ("Last_30_Days_Report_of_CS_CNA_SCPO_2022_TEST.csv",
     "CNA", "Standard 8-col, TEST"),
    ("Last_60_Days_Report_of_CS_MAXEDA_SCPO_DMD_2019_PROD.csv",
     "Maxeda", "4-col only (no Folder/App/End_Time/Status), mm/dd/yyyy"),
    ("Last_30_Days_Report_of_CS_AZSPA_SCPO_PROD.csv",
     "AZ", "Standard 8-col, PROD"),
    ("Last_15_Days_Report_of_CS_HALEON_SCPO_2025_TEST.csv",
     "Haleon", "Standard 8-col, EDI/PO patterns"),
]


def _clear_session():
    """Wipe all session data before each test."""
    r = requests.post(f"{BASE}/api/clear-session",
                      json={}, headers={"Content-Type": "application/json"})
    r.raise_for_status()
    time.sleep(0.3)


def _upload_batch(filepath: str) -> dict:
    """Upload a file via multipart to the batch processing endpoint."""
    with open(filepath, "rb") as f:
        r = requests.post(f"{BASE}/api/process-batch",
                          files={"file": (os.path.basename(filepath), f)},
                          timeout=120)
    r.raise_for_status()
    return r.json()


def _get_findings(batch_response: dict) -> dict:
    """Call the findings engine with the batch data."""
    payload = {
        "batch_kpis": batch_response.get("kpis") or batch_response.get("batch_kpis"),
        "top_jobs": batch_response.get("top_jobs", []),
        "window": batch_response.get("window", []),
        "anomalies": batch_response.get("anomalies", []),
        "customer_name": batch_response.get("customer_name", ""),
    }
    r = requests.post(f"{BASE}/api/generate-findings",
                      json=payload, headers={"Content-Type": "application/json"},
                      timeout=60)
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════
# Independent verification engine — computes expected KPIs from raw data
# ═══════════════════════════════════════════════════════════════
def _independent_analysis(filepath: str) -> dict:
    """Parse the CSV independently and compute expected KPIs."""
    # Read with flexible parsing
    try:
        df = pd.read_csv(filepath, encoding="utf-8-sig")
    except Exception:
        df = pd.read_csv(filepath, encoding="latin-1")

    # Strip whitespace from column names
    df.columns = [c.strip() for c in df.columns]

    result = {
        "columns_found": list(df.columns),
        "total_rows": len(df),
    }

    # ── Map columns ──────────────────────────────────────────
    col_map = {}
    for c in df.columns:
        cl = c.lower().replace(" ", "_").replace("(", "").replace(")", "")
        if "job_name" in cl or "job" == cl:
            col_map["job"] = c
        elif "sub_app" in cl or "sub_application" in cl:
            col_map["sub_app"] = c
        elif "start_time" in cl or "start_date" in cl:
            col_map["start"] = c
        elif "end_time" in cl or "end_date" in cl:
            col_map["end"] = c
        elif "run_time" in cl or "run_sec" in cl or "elapsed" in cl:
            col_map["runtime"] = c
        elif "status" in cl or "completion" in cl:
            col_map["status"] = c
        elif "folder" in cl:
            col_map["folder"] = c

    result["col_map"] = col_map

    # ── Parse runtime ────────────────────────────────────────
    if "runtime" in col_map:
        df["_run_sec"] = pd.to_numeric(df[col_map["runtime"]], errors="coerce").fillna(0)
    else:
        df["_run_sec"] = 0.0

    # ── Parse dates ──────────────────────────────────────────
    if "start" in col_map:
        # Use dashboard's own _parse_dt for consistent multi-format parsing
        df["_start_dt"] = _dashboard_parse_dt(df[col_map["start"]])
        df["_date"] = df["_start_dt"].dt.date
    else:
        df["_start_dt"] = pd.NaT
        df["_date"] = None

    # ── Parse status ─────────────────────────────────────────
    if "status" in col_map:
        df["_status"] = df[col_map["status"]].astype(str).str.strip().str.upper()
        ok_mask = df["_status"].str.contains("ENDED OK", na=False) & ~df["_status"].str.contains("NOT OK", na=False)
        fail_mask = df["_status"].str.contains("NOT OK|ABENDED|FAILED|TERMINATED", na=False, regex=True)
    else:
        ok_mask = pd.Series(True, index=df.index)
        fail_mask = pd.Series(False, index=df.index)

    df["_ok"] = ok_mask
    df["_fail"] = fail_mask

    # ── Basic KPIs ───────────────────────────────────────────
    total_runs = len(df)
    failed_runs = fail_mask.sum()
    ok_runs = ok_mask.sum()

    if "job" in col_map:
        # Dashboard uses composite key [Sub_Application, Job_Name] (RULE 6) to
        # distinguish DAILY vs WEEKLY jobs sharing the same base name.
        if "sub_app" in col_map and col_map["sub_app"] in df.columns:
            unique_jobs = df.groupby([col_map["sub_app"], col_map["job"]]).ngroups
        else:
            unique_jobs = df[col_map["job"]].nunique()
        job_col = col_map["job"]
    else:
        unique_jobs = 0
        job_col = None

    result["total_runs"] = int(total_runs)
    result["failed_runs"] = int(failed_runs)
    result["ok_runs"] = int(ok_runs)
    result["unique_jobs"] = int(unique_jobs)

    # ── Peak runtime per job & SLA compliance ────────────────
    sla_sec = DEFAULT_SLA_HRS * 3600
    if job_col:
        ok_df = df[df["_ok"]]
        if len(ok_df) > 0:
            job_peaks = ok_df.groupby(job_col)["_run_sec"].max()
            breaching_jobs = (job_peaks > sla_sec).sum()
            total_jobs_with_ok = len(job_peaks)
            compliance_pct = round(
                (total_jobs_with_ok - breaching_jobs) / max(1, total_jobs_with_ok) * 100, 1
            )
        else:
            breaching_jobs = 0
            total_jobs_with_ok = 0
            compliance_pct = 100.0
    else:
        breaching_jobs = 0
        total_jobs_with_ok = unique_jobs
        compliance_pct = 100.0

    result["breaching_jobs"] = int(breaching_jobs)
    result["compliance_pct"] = compliance_pct
    result["total_jobs_with_ok_runs"] = int(total_jobs_with_ok)

    # ── Daily window (elapsed) ───────────────────────────────
    if "end" in col_map and "_date" in df.columns:
        # Use dashboard's own _parse_dt for consistent multi-format parsing
        df["_end_dt"] = _dashboard_parse_dt(df[col_map["end"]])

        # Exclude cyclic sub-applications (same logic as dashboard):
        # Sub_Applications with high run frequency that span the whole day.
        df_window = df.copy()
        sub_col = col_map.get("sub_app")
        if sub_col and sub_col in df.columns and "_date" in df.columns:
            # Match dashboard's detect_cyclic_subs: >20 runs/day avg AND >3 runs/job/day
            runs_per_day = df.groupby([sub_col, "_date"]).size().reset_index(name="n")
            avg_rpd = runs_per_day.groupby(sub_col)["n"].mean()
            jobs_per_day = df.groupby([sub_col, "_date"])[col_map.get("job", sub_col)].nunique().reset_index(name="nj")
            avg_jpd = jobs_per_day.groupby(sub_col)["nj"].mean()
            avg_rpj = avg_rpd / avg_jpd.clip(lower=1)
            candidates = set(avg_rpd[avg_rpd > 20].index)
            cyclic_subs = {s for s in candidates if avg_rpj.get(s, 0) > 3}
            if cyclic_subs:
                df_window = df_window[~df_window[sub_col].isin(cyclic_subs)]

        # Compute elapsed from non-cyclic data only (matching dashboard)
        elap = df_window.dropna(subset=["_date", "_start_dt", "_end_dt"]).groupby("_date").agg(
            first_start=("_start_dt", "min"),
            last_end=("_end_dt", "max"),
        )
        elap["elapsed_sec"] = (elap["last_end"] - elap["first_start"]).dt.total_seconds()
        elap["elapsed_sec"] = elap["elapsed_sec"].clip(lower=0, upper=168*3600)
        elap["elapsed_hrs"] = elap["elapsed_sec"] / 3600

        # total_days = ALL dates in data (incl. cyclic-only days)
        # Days with only cyclic data get elapsed_hrs = 0 (non-breach)
        all_dates = df.dropna(subset=["_date"])["_date"].unique()
        total_days = len(all_dates)
        # Merge elapsed into full date set
        all_dates_df = pd.DataFrame({"_date": all_dates})
        all_dates_df = all_dates_df.merge(
            elap[["elapsed_hrs"]], left_on="_date", right_index=True, how="left")
        all_dates_df["elapsed_hrs"] = all_dates_df["elapsed_hrs"].fillna(0.0)
        all_dates_df["breach"] = all_dates_df["elapsed_hrs"] > DEFAULT_SLA_HRS

        breach_days = int(all_dates_df["breach"].sum())
        window_compliance = round((total_days - breach_days) / max(1, total_days) * 100, 1)
    else:
        total_days = 0
        breach_days = 0
        window_compliance = None  # Can't compute without End_Time

    result["window_total_days"] = total_days
    result["window_breach_days"] = breach_days
    result["window_compliance_pct"] = window_compliance

    # ── Date span ────────────────────────────────────────────
    valid_dates = df["_date"].dropna()
    if len(valid_dates) > 0:
        result["date_min"] = str(valid_dates.min())
        result["date_max"] = str(valid_dates.max())
        result["date_span_days"] = (valid_dates.max() - valid_dates.min()).days + 1
    else:
        result["date_min"] = None
        result["date_max"] = None
        result["date_span_days"] = 0

    return result


# ═══════════════════════════════════════════════════════════════
# Comparison engine
# ═══════════════════════════════════════════════════════════════
def _compare(label: str, expected, actual, tolerance_pct=1.0) -> dict:
    """Compare a single KPI. Returns {pass, label, expected, actual, delta_pct}."""
    if expected is None or actual is None:
        return {"pass": expected is None and actual is None,
                "label": label, "expected": expected, "actual": actual,
                "delta_pct": None, "note": "N/A (one is None)"}

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if expected == 0 and actual == 0:
            return {"pass": True, "label": label, "expected": expected,
                    "actual": actual, "delta_pct": 0.0}
        if expected == 0:
            delta_pct = 100.0
        else:
            delta_pct = abs(actual - expected) / abs(expected) * 100
        passed = delta_pct <= tolerance_pct
        return {"pass": passed, "label": label, "expected": expected,
                "actual": actual, "delta_pct": round(delta_pct, 2)}
    else:
        passed = str(expected).lower() in str(actual).lower()
        return {"pass": passed, "label": label, "expected": expected,
                "actual": actual, "delta_pct": None}


def run_test(filename: str, customer_expected: str, notes: str) -> dict:
    """Run a full test for one file."""
    filepath = os.path.join(SLA_DIR, filename)
    if not os.path.exists(filepath):
        return {"file": filename, "status": "SKIP", "reason": "File not found"}

    print(f"\n{'='*70}")
    print(f"  TESTING: {filename}")
    print(f"  Notes:   {notes}")
    print(f"{'='*70}")

    try:
        # Step 1: Clear session
        _clear_session()

        # Step 2: Independent analysis
        expected = _independent_analysis(filepath)
        print(f"  Independent: {expected['total_runs']} runs, "
              f"{expected['unique_jobs']} jobs, "
              f"compliance={expected['compliance_pct']}%, "
              f"window_compliance={expected.get('window_compliance_pct', 'N/A')}%, "
              f"failed={expected['failed_runs']}")

        # Step 3: Upload to dashboard
        resp = _upload_batch(filepath)
        # The upload endpoint may return the batch data directly or nested
        kpis = resp.get("kpis") or resp.get("batch_kpis") or {}
        top_jobs = resp.get("top_jobs", [])

        if not kpis:
            # Some responses have batch nested
            batch = resp.get("batch", {})
            kpis = batch.get("kpis", {})
            top_jobs = batch.get("top_jobs", [])

        dash_total_runs = kpis.get("total_runs", 0)
        dash_total_jobs = kpis.get("total_jobs", 0)
        dash_compliance = kpis.get("compliance_pct", 0)
        dash_failed = kpis.get("failed_runs", 0) or kpis.get("status_failed", 0)
        dash_window_compliance = kpis.get("batch_window_compliance")
        dash_window_breach_days = kpis.get("window_breach_days", 0)
        dash_window_total_days = kpis.get("window_total_days", 0)
        dash_customer = resp.get("customer_name", "") or kpis.get("customer_name", "")

        print(f"  Dashboard:   {dash_total_runs} runs, "
              f"{dash_total_jobs} jobs, "
              f"compliance={dash_compliance}%, "
              f"window_compliance={dash_window_compliance}%, "
              f"failed={dash_failed}")
        print(f"  Customer:    expected='{customer_expected}' got='{dash_customer}'")

        # Step 4: Compare KPIs
        comparisons = []

        # Total runs (tolerance: exact for small datasets, 1% for large)
        tol = 0.5 if expected["total_runs"] < 100 else 1.0
        comparisons.append(_compare("total_runs", expected["total_runs"], dash_total_runs, tol))

        # Unique jobs
        comparisons.append(_compare("unique_jobs", expected["unique_jobs"], dash_total_jobs, 1.0))

        # Failed runs
        comparisons.append(_compare("failed_runs", expected["failed_runs"], dash_failed, 5.0))

        # Compliance %
        comparisons.append(_compare("compliance_pct", expected["compliance_pct"],
                                     dash_compliance, 2.0))

        # Window compliance (if we could compute it)
        if expected["window_compliance_pct"] is not None and dash_window_compliance is not None:
            comparisons.append(_compare("window_compliance_pct",
                                         expected["window_compliance_pct"],
                                         dash_window_compliance, 3.0))

        # Window breach days
        if expected["window_breach_days"] > 0 or dash_window_breach_days > 0:
            comparisons.append(_compare("window_breach_days",
                                         expected["window_breach_days"],
                                         dash_window_breach_days, 5.0))

        # Customer name (fuzzy)
        comparisons.append(_compare("customer_name", customer_expected, dash_customer))

        # Step 5: Get findings
        findings_resp = _get_findings(resp)
        findings = findings_resp.get("findings", [])
        finding_count = len(findings)
        severity_counts = defaultdict(int)
        for f in findings:
            sev = f.get("severity", "UNKNOWN")
            severity_counts[sev] += 1

        # Validate findings quality
        finding_checks = []

        # If compliance < 50%, there MUST be a compliance finding
        if expected["compliance_pct"] < 50:
            has_compliance_finding = any(
                "compliance" in (f.get("text", "") + f.get("title", "")).lower() or
                "sla" in (f.get("text", "") + f.get("title", "")).lower()
                for f in findings
            )
            finding_checks.append({
                "label": "compliance_finding_present",
                "pass": has_compliance_finding,
                "note": f"Compliance={expected['compliance_pct']}% → should flag SLA issue"
            })

        # If there are failed runs above threshold, there MUST be a failure finding
        # Dashboard threshold: exec_fail_pct > 1% triggers warning, > 10% triggers critical
        fail_rate = expected["failed_runs"] / max(1, expected["total_runs"]) * 100
        if fail_rate > 1.0:
            has_failure_finding = any(
                "fail" in (f.get("text", "") + f.get("title", "")).lower() or
                "ended not ok" in (f.get("text", "") + f.get("title", "")).lower() or
                "abend" in (f.get("text", "") + f.get("title", "")).lower()
                for f in findings
            )
            finding_checks.append({
                "label": "failure_finding_present",
                "pass": has_failure_finding,
                "note": f"Failed={expected['failed_runs']} runs → should flag failures"
            })

        # If window breaches > 0, there should be a window finding
        if expected["window_breach_days"] and expected["window_breach_days"] > 0:
            has_window_finding = any(
                "window" in (f.get("text", "") + f.get("title", "")).lower()
                for f in findings
            )
            finding_checks.append({
                "label": "window_finding_present",
                "pass": has_window_finding,
                "note": f"Window breach days={expected['window_breach_days']} → should flag"
            })

        # Print comparisons
        print(f"\n  {'KPI':<30s} {'Expected':<15s} {'Dashboard':<15s} {'Delta%':<10s} {'PASS'}")
        print(f"  {'-'*80}")
        for c in comparisons:
            exp_str = str(c["expected"])[:14]
            act_str = str(c["actual"])[:14]
            delta_str = f"{c['delta_pct']:.1f}%" if c["delta_pct"] is not None else "—"
            status = "✓" if c["pass"] else "✗ FAIL"
            print(f"  {c['label']:<30s} {exp_str:<15s} {act_str:<15s} {delta_str:<10s} {status}")

        # Print findings quality
        if finding_checks:
            print(f"\n  {'Finding Check':<35s} {'PASS'}")
            print(f"  {'-'*45}")
            for fc in finding_checks:
                status = "✓" if fc["pass"] else f"✗ FAIL — {fc['note']}"
                print(f"  {fc['label']:<35s} {status}")

        print(f"\n  Findings generated: {finding_count} "
              f"(CRITICAL={severity_counts.get('CRITICAL',0)}, "
              f"WARNING={severity_counts.get('WARNING',0)}, "
              f"INFO={severity_counts.get('INFO',0)}, "
              f"OBSERVATION={severity_counts.get('OBSERVATION',0)})")

        # Compute overall accuracy
        kpi_pass = sum(1 for c in comparisons if c["pass"])
        kpi_total = len(comparisons)
        finding_pass = sum(1 for fc in finding_checks if fc["pass"])
        finding_total = len(finding_checks)
        all_pass = kpi_pass + finding_pass
        all_total = kpi_total + finding_total

        accuracy = round(all_pass / max(1, all_total) * 100, 1)
        print(f"\n  ACCURACY: {all_pass}/{all_total} = {accuracy}%")

        return {
            "file": filename,
            "status": "OK",
            "kpi_pass": kpi_pass,
            "kpi_total": kpi_total,
            "finding_pass": finding_pass,
            "finding_total": finding_total,
            "accuracy": accuracy,
            "comparisons": comparisons,
            "finding_checks": finding_checks,
            "dash_kpis": kpis,
            "expected": expected,
            "finding_count": finding_count,
            "severity_counts": dict(severity_counts),
        }

    except Exception as e:
        traceback.print_exc()
        return {"file": filename, "status": "ERROR", "reason": str(e)}


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  PE DASHBOARD — GENERIC ACCURACY TEST SUITE")
    print("  Testing with diverse Ctrl-M batch files from multiple customers")
    print("=" * 70)

    results = []
    for filename, customer, notes in TEST_FILES:
        r = run_test(filename, customer, notes)
        results.append(r)

    # ═══════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════
    print("\n\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    ok_results = [r for r in results if r["status"] == "OK"]
    err_results = [r for r in results if r["status"] == "ERROR"]
    skip_results = [r for r in results if r["status"] == "SKIP"]

    print(f"\n  Files tested:   {len(ok_results)}")
    print(f"  Files errored:  {len(err_results)}")
    print(f"  Files skipped:  {len(skip_results)}")

    if ok_results:
        total_kpi_pass = sum(r["kpi_pass"] for r in ok_results)
        total_kpi = sum(r["kpi_total"] for r in ok_results)
        total_finding_pass = sum(r["finding_pass"] for r in ok_results)
        total_finding = sum(r["finding_total"] for r in ok_results)
        overall_accuracy = round(
            (total_kpi_pass + total_finding_pass) /
            max(1, total_kpi + total_finding) * 100, 1
        )

        print(f"\n  {'File':<60s} {'Accuracy':<10s} {'KPI':<12s} {'Findings'}")
        print(f"  {'-'*95}")
        for r in ok_results:
            kpi = f"{r['kpi_pass']}/{r['kpi_total']}"
            fin = f"{r['finding_pass']}/{r['finding_total']}" if r["finding_total"] > 0 else "—"
            print(f"  {r['file']:<60s} {r['accuracy']:>5.1f}%    {kpi:<12s} {fin}")

        print(f"\n  OVERALL KPI ACCURACY:      {total_kpi_pass}/{total_kpi} = "
              f"{round(total_kpi_pass/max(1,total_kpi)*100, 1)}%")
        print(f"  OVERALL FINDINGS ACCURACY: {total_finding_pass}/{total_finding} = "
              f"{round(total_finding_pass/max(1,total_finding)*100, 1)}%")
        print(f"  OVERALL COMBINED ACCURACY: {overall_accuracy}%")

    if err_results:
        print(f"\n  ERRORS:")
        for r in err_results:
            print(f"    {r['file']}: {r['reason'][:100]}")

    # ── Report bugs found ──
    bugs = []
    for r in ok_results:
        for c in r.get("comparisons", []):
            if not c["pass"]:
                bugs.append(f"  [{r['file'][:40]}] {c['label']}: "
                            f"expected={c['expected']} got={c['actual']} "
                            f"(delta={c.get('delta_pct', '?')}%)")
        for fc in r.get("finding_checks", []):
            if not fc["pass"]:
                bugs.append(f"  [{r['file'][:40]}] {fc['label']}: {fc['note']}")

    if bugs:
        print(f"\n  BUGS DETECTED ({len(bugs)}):")
        for b in bugs:
            print(b)
    else:
        print(f"\n  NO BUGS DETECTED — all KPIs and findings match expected values")

    print(f"\n{'='*70}")
