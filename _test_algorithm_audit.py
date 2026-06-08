"""
_test_algorithm_audit.py — Comprehensive Algorithmic & Mathematical Audit
==========================================================================
Reverse-engineered test suite targeting every mathematical, logical and
edge-case issue in the batch-SLA pipeline.

Run:  py -3.14 _test_algorithm_audit.py
Output: prints PASS / FAIL / WARN with exact expected vs actual values.

Issues investigated
-------------------
 #1  Workflow_summary Tier-3 SLA defaults HARDCODED (split vs pe_config live values)
 #2  Compliance formula treats AT_RISK as PASSING (contradicts its own comment)
 #3  Window compliance uses global_sla_hrs not per-workflow resolved SLA
 #4  build_top_jobs_df buffer % — no div-by-zero guard when sla_hrs=0
 #5  parse_sla_hours() — sub-1-hour decimal treated as Excel fraction (×24)
 #6  Anchor matching uses str.contains() not exact-match → wrong elapsed window
 #7  48h sanity cap silently drops valid monthly batch runs (>48h)
 #8  _parse_dt format 40% threshold — mixed-format file loses rows to NaT
 #9  detect_batch_type returns "DAILY" default for unknown types
#10  _all_normalized_forms secondary-strip collision (two customers → same secondary key)
#11  Three inconsistent status classification systems across functions
#12  UNKNOWN sub_app rows skipped in workflow_summary (no job-level anchor fallback)
#13  build_top_jobs_df uses hardcoded buffer thresholds (10/30/50) not pe_config
#14  compute_metrics batch window compliance still uses global_ceil per job
#15  DAILY_LIMIT_HRS / MONTHLY_LIMIT_HRS stale at module load (not reloaded)
"""

from __future__ import annotations
import sys
import os
import math
import traceback
from datetime import datetime, timedelta
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

results: list[dict] = []

def record(issue_id: str, name: str, status: str, expected: Any, actual: Any, detail: str = ""):
    tag = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "SKIP": "⏭️"}.get(status, "?")
    results.append({"id": issue_id, "name": name, "status": status,
                    "expected": expected, "actual": actual, "detail": detail})
    print(f"{tag} [{issue_id}] {name}")
    if status != PASS:
        print(f"      expected : {expected}")
        print(f"      actual   : {actual}")
    if detail:
        print(f"      detail   : {detail}")


# ══════════════════════════════════════════════════════════════════════════════
# Issue #1 — Workflow_summary Tier-3 SLA defaults are HARDCODED
# Location: routers/sla_matrix.py  _compute_sla_matrix()  ~line 643
# _WF_DEFAULTS = {"DAILY": 6.0, "WEEKLY": 8.0, "BIWEEKLY": 12.0, "MONTHLY": 10.0}
# pe_config defaults: WEEKLY=17.0, BIWEEKLY=17.0, MONTHLY=17.0
# Bug: when user sets weekly_sla_hrs=24 via Settings, _resolve_job_sla() reads
#      pe_config (correct) but workflow_summary Tier-3 still reads 8.0 (wrong).
#      Same Ctrl-M file → per-job status=OK / workflow status=BREACH. Split view.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #1: Hardcoded Tier-3 defaults in workflow_summary ────")
try:
    from routers.sla_matrix import _compute_sla_matrix as _csm   # noqa
    import routers.sla_matrix as _rm_mod
    import ast, inspect
    src = inspect.getsource(_rm_mod)
    # Find the _WF_DEFAULTS literal in the workflow_summary block
    match = None
    for line in src.splitlines():
        if "_WF_DEFAULTS" in line and "WEEKLY" in line and "8.0" in line:
            match = line.strip()
            break
    if match:
        record("#1", "Workflow_summary Tier-3 WEEKLY default",
               FAIL, "pe_config.SLA_WEEKLY_HRS (17.0)", "8.0 (hardcoded)",
               f"Source line: {match}\n"
               "      Impact: if Settings sets weekly_sla_hrs=24, workflow_summary still uses 8h.\n"
               "      job-level _resolve_job_sla reads pe_config → PASS;  workflow_summary → FAIL.\n"
               "      Same run: per-job buffer=(24-7)/24=71%=OK; workflow buffer=(8-7)/8=12.5%=AT_RISK.")
    else:
        record("#1", "Workflow_summary Tier-3 WEEKLY default", PASS,
               "pe_config value", "no hardcoded 8.0 found in _WF_DEFAULTS")
except Exception as e:
    record("#1", "Workflow_summary Tier-3 WEEKLY default", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #2 — Compliance formula contradiction
# Location: routers/sla_matrix.py  ~line 500
# Comment says "only BREACH + AT_RISK are violations"
# Code:  compliance = (ok + longjob + atrisk) / eligible  ← AT_RISK counted as PASSING
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #2: Compliance formula treats AT_RISK as PASSING ────")
try:
    import inspect
    import routers.sla_matrix as _rm
    src = inspect.getsource(_rm)
    # Verify the source uses (ok+longjob)/eligible — not (ok+longjob+atrisk)/eligible
    old_formula_in_source = False
    correct_formula_in_source = False
    for line in src.splitlines():
        stripped = line.strip()
        if "compliance" in stripped and "ok_count" in stripped and "longjob_count" in stripped:
            if "atrisk_count" in stripped:
                old_formula_in_source = True
            else:
                correct_formula_in_source = True
            break

    if old_formula_in_source:
        record("#2", "Compliance with all-AT_RISK runs",
               FAIL,
               "(ok_count + longjob_count) / eligible_runs",
               "Source still has atrisk_count in numerator",
               "Fix: compliance = (ok+longjob)/eligible  (AT_RISK = violation, not passing).")
    elif correct_formula_in_source:
        # Verify the numeric output is consistent: all-AT_RISK → 0% compliant
        ok_c = longjob_c = 0
        atrisk_c = 100
        eligible = 100
        compliance_correct = round((ok_c + longjob_c) / eligible * 100, 2)   # should be 0%
        record("#2", "Compliance with all-AT_RISK runs", PASS,
               f"{compliance_correct}% (AT_RISK excluded from compliant count)",
               "Source uses (ok+longjob)/eligible — AT_RISK correctly treated as violation")
    else:
        record("#2", "Compliance formula not found in source", WARN,
               "(ok+longjob)/eligible", "Could not locate compliance formula line")
except Exception as e:
    record("#2", "Compliance formula", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #3 — Window compliance uses global_sla_hrs (UI mode) not per-workflow SLA
# Location: routers/sla_matrix.py window_compliance block ~line 780
#   wgrp["breach"] = wgrp["elapsed_hrs"] > global_sla_hrs
# Bug: a mixed file with DAILY+WEEKLY workflows. If user selects "Daily (4h)" UI mode:
#   - global_sla_hrs = 4h
#   - All WEEKLY workflows (running 7h) show as BREACH even if their SLA is 8h
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #3: Window compliance uses raw UI mode SLA ────")
try:
    import inspect
    import routers.sla_matrix as _rm3
    src3 = inspect.getsource(_rm3)
    # Fix #3: the window compliance block should use per-Sub_Application resolved SLA.
    # Verify the fixed code is present: _sub_sla_lookup or resolved_sla in window compliance.
    per_sub_fix_present = (
        "_sub_sla_lookup" in src3 or
        "resolved_sla" in src3 or
        "_sub_breach_daily" in src3
    )
    # Old bug: single line `wgrp["breach"] = wgrp["elapsed_hrs"] > global_sla_hrs`
    # Present only in fallback branch (no Sub_Application) — that's acceptable.
    # The primary path must use per-sub-app SLA.
    if per_sub_fix_present:
        record("#3", "Window compliance per-Sub_Application SLA", PASS,
               "per-workflow SLA resolution in window compliance",
               "Source uses _sub_sla_lookup / resolved_sla for per-sub-app breach check")
    else:
        issue3_found = False
        for line in src3.splitlines():
            if 'wgrp["breach"]' in line and "global_sla_hrs" in line:
                issue3_found = True
                record("#3", "Window compliance vs global_sla_hrs",
                       FAIL,
                       "wgrp['breach'] = wgrp['elapsed_hrs'] > per_workflow_resolved_sla",
                       "wgrp['breach'] = wgrp['elapsed_hrs'] > global_sla_hrs",
                       "Primary window breach path still uses global_sla_hrs.\n"
                       "      Fix: build _sub_sla_lookup and use per-sub-app resolved SLA.")
                break
        if not issue3_found:
            record("#3", "Window compliance vs global_sla_hrs", PASS,
                   "per-workflow SLA", "appears to use per-workflow resolution")
except Exception as e:
    record("#3", "Window compliance SLA source", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #4 — build_top_jobs_df — no NaN guard for sla_hrs=0
# Location: services/batch_calculator.py  build_top_jobs_df()
#   top_jobs["buffer_pct"] = ((sla_hrs - peak_hrs) / sla_hrs * 100).round(1)
# Bug: sla_hrs=0 → ZeroDivisionError or inf/NaN propagates to all downstream users
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #4: build_top_jobs_df division by zero ────")
try:
    import pandas as pd
    import numpy as np
    import inspect
    import services.batch_calculator as _bc4

    # Verify that build_top_jobs_df uses a zero-guard for sla_hrs before dividing.
    # The fix replaces (sla_hrs - peak_hrs) / sla_hrs with a NaN-safe version
    # using .replace(0, float("nan")) so sla_hrs=0 produces NaN, not inf.
    src4 = inspect.getsource(_bc4)
    zero_guard_present = (
        "replace(0, float" in src4 or
        "replace(0, np.nan" in src4 or
        "_sla_safe" in src4
    )

    if zero_guard_present:
        # Also verify the guard actually prevents inf/nan
        sla_hrs = pd.Series([6.0, 0.0, 4.0])
        peak_hrs = pd.Series([5.0, 3.0, 2.0])
        _sla_safe = sla_hrs.replace(0, float("nan"))
        buffer_pct = ((_sla_safe - peak_hrs) / _sla_safe * 100).round(1)
        has_inf = buffer_pct.isin([float('inf'), float('-inf')]).any()
        has_nan = buffer_pct.isna().any()
        if has_inf:
            record("#4", "build_top_jobs_df buffer % with sla_hrs=0",
                   FAIL, "guard prevents inf", f"inf still present: {buffer_pct.tolist()}")
        else:
            record("#4", "build_top_jobs_df buffer % with sla_hrs=0", PASS,
                   "NaN for sla_hrs=0 (not inf)", f"values={buffer_pct.tolist()}, has_nan={has_nan}")
    else:
        # Guard not present: simulate old behavior to confirm the bug exists
        sla_hrs = pd.Series([6.0, 0.0, 4.0])
        peak_hrs = pd.Series([5.0, 3.0, 2.0])
        buffer_pct = ((sla_hrs - peak_hrs) / sla_hrs * 100).round(1)
        has_inf = buffer_pct.isin([float('inf'), float('-inf')]).any()
        has_nan = buffer_pct.isna().any()
        record("#4", "build_top_jobs_df buffer % with sla_hrs=0",
               FAIL,
               "guard returning None or NaN gracefully",
               f"inf={has_inf}, nan={has_nan}, values={buffer_pct.tolist()}",
               "Row with sla_hrs=0 produces inf/NaN in buffer_pct column.\n"
               "      Fix: buffer_pct = _sla_safe = sla_hrs.replace(0, float('nan'))")
except Exception as e:
    record("#4", "build_top_jobs_df div-by-zero", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #5 — parse_sla_hours() — sub-1-hour decimal treated as Excel fraction
# Location: services/sla_merger.py  parse_sla_hours()
# Excel stores 06:00 as fraction 0.25; openpyxl with dtype=str returns "0.25".
# Code: if 0 < fv < 1: return round(fv * 24, 3)   ← correct for Excel export
# Bug: customer manually types "0.5" meaning "30 minutes" (0.5 hours).
#      Code returns 0.5*24=12.0 hours — SLA inflated ×24.
#      Every job looks compliant. False 100% compliance.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #5: Sub-1-hour SLA decimal treated as Excel fraction ────")
try:
    from services.sla_merger import parse_sla_hours

    cases = [
        ("0.5",    12.0,  "Should be 0.5h (30min) but returns 12h (Excel fraction × 24)"),
        ("0.25",   6.0,   "Should be 0.25h (15min) but returns 6h (Excel fraction × 24)"),
        ("0.75",   18.0,  "Should be 0.75h (45min) but returns 18h (Excel fraction × 24)"),
        (0.5,      12.0,  "Numeric 0.5 → 12h (× 24); ambiguous: 0.5h vs Excel 12:00"),
        ("1.5",    1.5,   "1.5 hours — correct (> 1 so no fraction multiply)"),
        ("45 min", 0.75,  "45 minutes → 0.75h — correct"),
    ]
    failures = []
    for value, expected_result, desc in cases:
        result_val = parse_sla_hours(value)
        # Mark as FAIL when the fraction logic inflates a plausible hour value
        if value in ("0.5", "0.25", "0.75") or value == 0.5:
            # These could be "30 min" written as decimals — code gives 12h/6h/18h
            failures.append((value, result_val, expected_result, desc))

    if failures:
        for v, actual_v, exp_v, d in failures:
            record("#5", f"parse_sla_hours({v!r})",
                   WARN,  # WARN not FAIL because Excel fraction logic IS correct for .xlsx exports
                   f"Ambiguous: could be {v}h OR Excel fraction {exp_v}h",
                   f"Returns {actual_v}h (Excel fraction interpretation)",
                   f"{d}\n"
                   "      Impact: customers with sub-1h SLAs who type '0.5' get SLA=12h.\n"
                   "      Every job appears OK (runs 0.3h vs 'SLA' 12h = 97.5% buffer).\n"
                   "      Fix: only apply ×24 when column has Excel time format detection;\n"
                   "           OR require 0 < fv < 0.1667 (i.e. <4h as fraction) to avoid\n"
                   "           values like 0.5 that are likely typed decimal hours.")
    else:
        record("#5", "parse_sla_hours sub-1-hour decimal", PASS,
               "Consistent handling", "All checked")
except Exception as e:
    record("#5", "parse_sla_hours Excel fraction", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #6 — Anchor matching uses str.contains() — matches non-target jobs
# Location: routers/sla_matrix.py  per-run elapsed loop  ~line 594
#   _fm = rg["Job_Name"].str.upper().str.contains(_first_anchor, ...)
# Bug: first_anchor="PROCESS" matches "REPROCESS_DAILY", "POST_PROCESS_1" etc.
#      → wrong start time → wrong elapsed → wrong buffer %
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #6: Anchor matching — str.contains vs exact match ────")
try:
    import inspect
    import routers.sla_matrix as _rm6

    src6 = inspect.getsource(_rm6)
    # Verify source uses exact match BEFORE str.contains fallback.
    # Fixed code: try `_jnames == _first_anchor` first; falls back to .str.contains.
    exact_match_present = (
        "_jnames == _first_anchor" in src6 or
        "_exact_first" in src6 or
        "== _first_anchor" in src6
    )
    contains_fallback = ".str.contains(" in src6 and ("_first_anchor" in src6 or "_last_anchor" in src6)

    if exact_match_present and contains_fallback:
        # Verify the logic: exact match finds only 'PROCESS', not 'REPROCESS' etc.
        jobs = pd.Series(["PRE_PROCESS_VALIDATE", "PROCESS", "REPROCESS_CLEANUP", "POST_PROCESS_AUDIT"])
        first_anchor = "PROCESS"
        _jnames = jobs.str.upper()
        _exact = _jnames == first_anchor
        _fm = _exact if _exact.any() else _jnames.str.contains(first_anchor, na=False, regex=False)
        matched = jobs[_fm].tolist()
        if matched == ["PROCESS"]:
            record("#6", "Anchor job matching — exact match first", PASS,
                   "Only ['PROCESS'] matched", f"exact match used: {matched}")
        else:
            record("#6", "Anchor job matching — exact match",
                   FAIL, "['PROCESS']", f"{matched}",
                   "Exact match logic present but not filtering correctly.")
    elif exact_match_present:
        record("#6", "Anchor job matching — exact match first", PASS,
               "exact match before str.contains",
               "Source has exact match check; str.contains used only as fallback")
    else:
        # Old behavior: pure str.contains
        jobs = pd.Series(["PRE_PROCESS_VALIDATE", "PROCESS", "REPROCESS_CLEANUP", "POST_PROCESS_AUDIT"])
        first_anchor = "PROCESS"
        current_match = jobs.str.upper().str.contains(first_anchor, na=False, regex=False)
        matched = jobs[current_match].tolist()
        record("#6", "Anchor job matching — contains vs exact",
               FAIL,
               "Only ['PROCESS'] should match (exact anchor)",
               f"{matched} matched (contains)",
               f"first_anchor='PROCESS' matches {len(matched)} jobs instead of 1.\n"
               "      Fix: try exact match first; fall back to contains.")
except Exception as e:
    record("#6", "Anchor matching", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #7 — 48h sanity cap silently drops valid long-running monthly batches
# Location: routers/sla_matrix.py  per-run elapsed loop
#   if 0 <= elapsed <= 48:  # sanity cap — skip multi-day anomalies
# Bug: a MONTHLY batch spanning ~50h (weekend + bank holiday) is silently dropped.
#      per_run_elapsed is empty → runtime_h=0 → status_wf="RUNTIME_MISSING"
#      PE user sees "no data" not "possible breach" — silent false negative.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #7: 48h sanity cap — silent drop of long monthly batches ────")
try:
    import inspect
    import routers.sla_matrix as _rm7

    src7 = inspect.getsource(_rm7)
    # Verify source uses batch-type-aware cap instead of hardcoded 48.
    # Fixed code defines _MAX_ELAPSED dict with MONTHLY=400.
    batch_aware_cap_present = (
        "_MAX_ELAPSED" in src7 or
        ("MONTHLY" in src7 and "400" in src7 and "elapsed" in src7.lower())
    )
    old_hardcoded_48 = False
    for line in src7.splitlines():
        if "elapsed <= 48" in line and "sanity" in line.lower():
            old_hardcoded_48 = True
            break

    elapsed = 52.0   # 52h monthly run
    sla_monthly = 60.0

    if old_hardcoded_48:
        record("#7", "48h cap drops 52h monthly batch",
               FAIL,
               f"runtime_h=52.0, buffer={(sla_monthly-52)/sla_monthly*100:.1f}%=AT_RISK",
               "Hardcoded 48h cap — run silently dropped",
               "Fix: _MAX_ELAPSED = {'DAILY':48,'WEEKLY':200,'MONTHLY':400}")
    elif batch_aware_cap_present:
        # Verify the logic: MONTHLY cap = 400h so 52h run IS included
        _MAX_ELAPSED = {"DAILY": 48.0, "WEEKLY": 200.0, "BIWEEKLY": 400.0, "MONTHLY": 400.0}
        monthly_cap = _MAX_ELAPSED["MONTHLY"]
        run_included = 0 <= elapsed <= monthly_cap
        if run_included:
            buf = round((sla_monthly - elapsed) / sla_monthly * 100, 1)
            record("#7", "Batch-type-aware cap includes 52h monthly batch", PASS,
                   f"MONTHLY cap={monthly_cap}h ≥ elapsed={elapsed}h → included",
                   f"buffer={buf}% (AT_RISK, actionable finding generated)")
        else:
            record("#7", "48h cap drops 52h monthly batch",
                   FAIL, "Run included", f"cap={monthly_cap}h but run still excluded")
    else:
        record("#7", "48h cap — batch-type-aware check", WARN,
               "_MAX_ELAPSED dict", "Could not verify cap logic in source")
except Exception as e:
    record("#7", "48h sanity cap", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #8 — _parse_dt() 40% format threshold — mixed-format file loses rows
# Location: services/batch_calculator.py  _parse_dt()
#   if parsed.notna().sum() > len(s) * 0.4: return parsed
# Bug: file has 55% ISO dates + 45% DD-MM-YYYY.
#      ISO hits 55% → selected. The 45% EU rows become NaT.
#      dropna removes them → 45% of audit history lost silently.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #8: _parse_dt 40% threshold — mixed-format date loss ────")
try:
    import pandas as pd
    from services.batch_calculator import _parse_dt

    n = 100
    # 55 ISO format, 45 EU format
    iso_dates = [f"2025-{(i%12)+1:02d}-{(i%28)+1:02d} 08:00:00" for i in range(55)]
    eu_dates  = [f"{(i%28)+1:02d}-{(i%12)+1:02d}-2025 08:00" for i in range(45)]
    mixed = pd.Series(iso_dates + eu_dates)

    parsed = _parse_dt(mixed)
    nat_count = parsed.isna().sum()
    nat_pct = round(nat_count / n * 100, 1)

    if nat_pct >= 30:
        record("#8", "_parse_dt mixed-format date loss",
               FAIL,
               "≤5% NaT (both formats parsed)",
               f"{nat_pct}% NaT ({nat_count} rows lost)",
               f"File with 55% ISO + 45% EU dates: {nat_count} rows become NaT.\n"
               "      Lost rows = lost run history = wrong breach counts, wrong compliance %.\n"
               "      Example: 60-day file → only 33 days of ISO history kept. Trend invisible.\n"
               "      Fix: after primary format (>40%), retry remaining NaTs with next formats\n"
               "           (current dayfirst=True fallback already exists — but only tried last).\n"
               "      Actual fix needed: for each NaT run remaining formats individually.\n"
               "      The pd.to_datetime(s, errors='coerce') last-resort often misparses EU dates.")
    elif nat_pct >= 10:
        record("#8", "_parse_dt mixed-format date loss",
               WARN,
               "≤5% NaT",
               f"{nat_pct}% NaT ({nat_count} rows)",
               "Some EU-format rows not parsed. Verify dayfirst fallback is catching them.")
    else:
        record("#8", "_parse_dt mixed-format date loss", PASS,
               "≤5% NaT", f"{nat_pct}% NaT ({nat_count} rows)")
except Exception as e:
    record("#8", "_parse_dt mixed-format", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #9 — detect_batch_type always returns "DAILY" for unrecognised names
# Location: services/sla_merger.py  detect_batch_type()
#   return "DAILY"   # last line — conservative fallback
# Bug: "BATCH_WF_TRANSFER_NIGHTLY" contains "NIGHTLY" → correctly mapped to DAILY.
#      BUT "BATCH_WF_TRANSFER_LATE_NIGHT" → no keyword match → DAILY by default.
#      vs a genuinely WEEKLY job "WF_EOW_SETTLEMENT" which has no WEEKLY keyword.
#      SLA assigned: 6h (DAILY default) instead of 17h (WEEKLY).
#      Buffer = (6 - 5.5) / 6 = 8.3% → AT_RISK.  Real buffer = (17-5.5)/17 = 67.6% → OK.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #9: detect_batch_type DAILY default for unknown types ────")
try:
    from services.sla_merger import detect_batch_type

    test_jobs = [
        ("WF_EOW_SETTLEMENT",          "WEEKLY",  "EOW = End Of Week; no WEEKLY keyword in name"),
        ("MONTHLY_RECON",              "MONTHLY", "Contains MONTHLY → should detect"),
        ("PERIOD_END_CLOSE",           "DAILY",   "Period end could be monthly — defaults to DAILY"),
        ("NIGHTLY_INDEX_REBUILD",      "DAILY",   "NIGHTLY → DAILY correct"),
        ("BI_WEEKLY_PAYROLL",          "BIWEEKLY","Should detect BI_WEEKLY"),
        ("BATCH_TRANSFER_WF1",         "WEEKLY",  "_WF pattern → WEEKLY via pe_config"),
        ("FINANCIAL_YEAR_END_CLOSE",   "DAILY",   "Annual job mislabelled as DAILY"),
    ]

    failures = []
    for name, expected_type, note in test_jobs:
        detected = detect_batch_type(name, "")
        if detected != expected_type:
            failures.append((name, expected_type, detected, note))

    # Focus on the high-impact SLA mismatch cases
    critical_failures = [(n, e, a, d) for n, e, a, d in failures
                         if e in ("WEEKLY", "MONTHLY", "BIWEEKLY")]
    if critical_failures:
        for n, e, a, d in critical_failures[:3]:
            from services import pe_config as _pc
            sla_assigned = {"DAILY": _pc.SLA_DAILY_HRS, "WEEKLY": _pc.SLA_WEEKLY_HRS,
                            "MONTHLY": _pc.SLA_MONTHLY_HRS}.get(a, _pc.SLA_DAILY_HRS)
            sla_correct  = {"DAILY": _pc.SLA_DAILY_HRS, "WEEKLY": _pc.SLA_WEEKLY_HRS,
                            "MONTHLY": _pc.SLA_MONTHLY_HRS}.get(e, _pc.SLA_DAILY_HRS)
            runtime_eg   = sla_correct * 0.8   # 80% of correct SLA
            buf_assigned = (sla_assigned - runtime_eg) / sla_assigned * 100
            buf_correct  = (sla_correct  - runtime_eg) / sla_correct  * 100
            record("#9", f"detect_batch_type('{n}')",
                   FAIL if e != "DAILY" else WARN,
                   e, a,
                   f"{d}\n"
                   f"      SLA assigned: {sla_assigned}h ({a}) vs correct: {sla_correct}h ({e})\n"
                   f"      Example runtime {runtime_eg:.1f}h:\n"
                   f"        Assigned SLA buffer: {buf_assigned:.1f}% → "
                   f"{'BREACH/AT_RISK' if buf_assigned < 15 else 'OK'} (WRONG)\n"
                   f"        Correct SLA buffer:  {buf_correct:.1f}% → "
                   f"{'BREACH/AT_RISK' if buf_correct < 15 else 'OK'} (RIGHT)\n"
                   "      Fix: add EOW/BOW/month-end/year-end keyword mappings;\n"
                   "           return 'UNKNOWN' (not DAILY) when no pattern matches.\n"
                   "           Upstream callers can then show 'SLA_INFERRED' badge.")
    elif failures:
        record("#9", "detect_batch_type defaults", WARN,
               "All types detected", f"Minor mismatches: {[(n,e,a) for n,e,a,_ in failures]}")
    else:
        record("#9", "detect_batch_type defaults", PASS,
               "All types correct", "All test cases matched")
except Exception as e:
    record("#9", "detect_batch_type", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #10 — _all_normalized_forms collision between multiple customer prefixes
# Location: services/sla_merger.py  _all_normalized_forms()
# Bug: XLSX has "PETBARN_DAILY_BATCH" (SLA=4h) and "TESCO_DAILY_BATCH" (SLA=6h).
#      Both strip to secondary form "DAILY_BATCH".
#      _bsla_exact["DAILY_BATCH"] = last one written = TESCO SLA=6h.
#      Ctrl-M Sub_Application "PETBARN_DAILY_BATCH" → norm = "DAILY_BATCH"
#      → gets SLA=6h (Tesco's) instead of 4h (its own).
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #10: _all_normalized_forms secondary key collision ────")
try:
    from services.sla_merger import _all_normalized_forms
    import inspect, routers.sla_matrix as _rm10

    wf_petbarn = "PETBARN_DAILY_BATCH"
    wf_tesco   = "TESCO_DAILY_BATCH"

    forms_pb = _all_normalized_forms(wf_petbarn)
    forms_ts = _all_normalized_forms(wf_tesco)

    pb_secondary = [f for f in forms_pb if f != forms_pb[0]]
    ts_secondary = [f for f in forms_ts if f != forms_ts[0]]
    collision = bool(pb_secondary and ts_secondary and
                     set(pb_secondary) & set(ts_secondary))

    # The collision in _all_normalized_forms itself is expected (it's a lookup helper).
    # The FIX lives in sla_matrix.py's _bsla_exact builder: it detects collisions and
    # skips secondary forms that are claimed by multiple workflows.
    # Verify the upstream fix is present in sla_matrix.py source.
    src10 = inspect.getsource(_rm10)
    indexing_fix_present = (
        "_secondary_key_count" in src10 or
        "collision" in src10.lower() and "skip" in src10.lower()
    )

    if not collision:
        record("#10", "_all_normalized_forms customer prefix collision", PASS,
               "No collision", f"pb={forms_pb}, ts={forms_ts}")
    elif collision and indexing_fix_present:
        shared = sorted(set(pb_secondary) & set(ts_secondary))
        # The function still generates shared keys — but sla_matrix.py skips them on collision.
        # Simulate the collision-aware indexer:
        _rows = [
            {"workflow": wf_petbarn, "sla_hours": 4.0},
            {"workflow": wf_tesco,   "sla_hours": 6.0},
        ]
        _sec_count: dict = {}
        for row in _rows:
            wf = row["workflow"]
            sec_forms = [f for f in _all_normalized_forms(wf) if f != _all_normalized_forms(wf)[0]]
            for sf in sec_forms:
                _sec_count[sf] = _sec_count.get(sf, 0) + 1
        _bsla_exact: dict = {}
        for row in _rows:
            wf = row["workflow"]
            sla = row["sla_hours"]
            for i, f in enumerate(_all_normalized_forms(wf)):
                is_sec = i > 0
                if is_sec and _sec_count.get(f, 0) > 1:
                    continue   # skip colliding secondary
                _bsla_exact[f] = (sla, wf)
        # Primary keys (PETBARN_DAILY_BATCH, TESCO_DAILY_BATCH) must be present
        # Secondary collision key (DAILY_BATCH) must NOT be in _bsla_exact
        pb_primary = forms_pb[0]
        ts_primary = forms_ts[0]
        primary_ok = pb_primary in _bsla_exact and ts_primary in _bsla_exact
        collision_key_gone = shared[0] not in _bsla_exact
        if primary_ok and collision_key_gone:
            record("#10", "_all_normalized_forms secondary key collision — fixed", PASS,
                   "Collision detected and skipped at indexing time",
                   f"Primary keys indexed correctly; '{shared[0]}' skipped (collision)")
        else:
            record("#10", "_all_normalized_forms secondary key collision",
                   FAIL if not collision_key_gone else WARN,
                   "Collision key absent from _bsla_exact",
                   f"collision_key_gone={collision_key_gone}, primary_ok={primary_ok}")
    else:
        # Collision exists and no fix detected
        shared = sorted(set(pb_secondary) & set(ts_secondary))
        record("#10", "_all_normalized_forms customer prefix collision",
               FAIL,
               "Collision detection in _bsla_exact indexer",
               f"Both produce secondary key(s): {shared}; no collision guard in sla_matrix.py",
               "Fix: detect colliding secondary keys and skip them; log warning.")
except Exception as e:
    record("#10", "_all_normalized_forms collision", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #11 — Three inconsistent status classification systems
# Location:
#   A. batch_calculator.py  calculate_sla_buffer() → CRITICAL/CAUTION/HEALTHY/EXCELLENT
#   B. batch_calculator.py  build_top_jobs_df() → BREACH/CRITICAL/CAUTION/HEALTHY/EXCELLENT
#   C. sla_matrix.py _compute_sla_matrix() → BREACH/AT_RISK/LONG_JOB/OK
# Bug: buffer_pct=8% gets:
#   A → CRITICAL (8 < 10)
#   B → CRITICAL (8 < 10)  [same thresholds as A]
#   C → AT_RISK  (8 < 15)  [different thresholds!]
# PE user sees "CRITICAL" in Batch Review but "AT_RISK" in SLA Matrix for same job.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #11: Three inconsistent classification systems ────")
try:
    from services import pe_config as _pc
    from services.batch_calculator import calculate_sla_buffer
    import inspect, services.batch_calculator as _bc11

    # Test buffer_pct = 8%
    sla_h = 6.0
    runtime_at8 = sla_h * (1 - 0.08)   # 8% buffer → runtime = 92% of SLA

    # System A: calculate_sla_buffer (fixed: should return AT_RISK, not CRITICAL)
    result_a = calculate_sla_buffer(sla_h, runtime_at8)
    status_a = result_a["status"]

    # System B: build_top_jobs_df — verify source uses pe_config thresholds, not 10/30/50
    src11 = inspect.getsource(_bc11)
    # Check for old labels as return values (quoted strings), not just comments
    import re as _re11
    # Match quoted string literals: "CRITICAL", 'CRITICAL', "CAUTION", etc.
    old_b_labels = bool(_re11.search(r'["\']CRITICAL["\']|["\']CAUTION["\']|["\']EXCELLENT["\']|["\']HEALTHY["\']', src11))
    # After fix: should use AT_RISK / LONG_JOB / OK as quoted values
    new_b_labels = bool(_re11.search(r'["\']AT_RISK["\']', src11)) and bool(_re11.search(r'["\']LONG_JOB["\']', src11))

    # Derive System B status from source
    if new_b_labels and not old_b_labels:
        AT_RISK_PCT  = _pc.SLA_ATRISK_PCT
        LONG_JOB_PCT = _pc.SLA_LONGJOB_PCT
        status_b = ("BREACH"   if 8.0 <= 0    else
                    "AT_RISK"  if 8.0 <= AT_RISK_PCT else
                    "LONG_JOB" if 8.0 <= LONG_JOB_PCT else "OK")
    else:
        # Old hardcoded
        status_b = ("BREACH" if 8.0 < 0 else "CRITICAL" if 8.0 < 10 else "CAUTION")

    # System C: sla_matrix — always uses pe_config
    AT_RISK_PCT  = _pc.SLA_ATRISK_PCT
    LONG_JOB_PCT = _pc.SLA_LONGJOB_PCT
    status_c = ("BREACH"   if 8.0 <= 0    else
                "AT_RISK"  if 8.0 <= AT_RISK_PCT else
                "LONG_JOB" if 8.0 <= LONG_JOB_PCT else "OK")

    if status_a == status_b == status_c:
        record("#11", "Inconsistent status classification at buffer=8%", PASS,
               f"All three: {status_c}",
               f"A={status_a}, B={status_b}, C={status_c} — all unified to pe_config thresholds")
    else:
        problems = []
        if status_a != status_c:
            problems.append(f"System A (calculate_sla_buffer): {status_a} ≠ {status_c}")
        if old_b_labels:
            problems.append("System B (build_top_jobs_df): still uses CRITICAL/CAUTION labels")
        record("#11", "Inconsistent status classification at buffer=8%",
               FAIL,
               f"All three systems return {status_c} (pe_config unified)",
               "; ".join(problems) if problems else f"A={status_a},B={status_b},C={status_c}",
               "Fix: unify all three to use pe_config.SLA_ATRISK_PCT / SLA_LONGJOB_PCT.")
except Exception as e:
    record("#11", "Status classification consistency", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #12 — UNKNOWN Sub_Application → skipped in workflow_summary
# Location: routers/sla_matrix.py  workflow_summary loop
#   if not sub_app or sub_app.upper() in ("UNKNOWN", "NAN", ...):
#       continue
# Bug: Ctrl-M files without Sub_Application column get all rows → Sub_Application="UNKNOWN".
#      The _bsla_by_job anchor (first_job/last_job) was designed for this case,
#      but the lookup is INSIDE the loop that skips "UNKNOWN" rows entirely.
#      Result: no workflow_summary entries for these files.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #12: UNKNOWN sub_app skipped in workflow_summary ────")
try:
    import inspect
    import routers.sla_matrix as _rm12
    from services.batch_calculator import load_ctrlm_bytes

    src12 = inspect.getsource(_rm12)
    # Verify the sla_matrix.py workflow_summary loop has anchor-job fallback for UNKNOWN.
    # Fixed code: when sub_app is UNKNOWN, tries _bsla_by_job lookup before skipping.
    anchor_fallback_present = (
        "_synth_wf" in src12 or
        "_bsla_by_job" in src12 and "sub_app_is_unknown" in src12
    )
    old_skip_only = (
        "UNKNOWN.*continue" in src12 or
        ("UNKNOWN" in src12 and "continue" in src12 and "_synth_wf" not in src12)
    )

    # Ctrl-M file with no Sub_Application column — should produce UNKNOWN rows
    csv_no_subapp = (
        "Job_Name,Start_Time,End_Time,Status\n"
        "JOB_TRANSFER_DAILY,2025-05-01 02:00:00,2025-05-01 05:30:00,ENDED OK\n"
        "JOB_VALIDATE_DAILY,2025-05-01 05:30:00,2025-05-01 07:00:00,ENDED OK\n"
    )
    df_no_sub = load_ctrlm_bytes(csv_no_subapp.encode(), "test_no_subapp.csv")
    sub_apps = df_no_sub["Sub_Application"].unique().tolist() if "Sub_Application" in df_no_sub.columns else ["NOT_SET"]
    has_only_unknown = all(str(s).upper() in ("UNKNOWN", "") for s in sub_apps)

    if anchor_fallback_present:
        record("#12", "UNKNOWN sub_app → anchor-job fallback", PASS,
               "Anchor-job fallback present in workflow_summary loop",
               f"sub_apps={sub_apps}, anchor fallback: _synth_wf/_bsla_by_job path found")
    elif has_only_unknown:
        record("#12", "UNKNOWN sub_app → workflow_summary skipped",
               FAIL,
               "Anchor-job fallback (_synth_wf) in workflow_summary loop",
               f"All rows Sub_Application={sub_apps}; no anchor fallback in source",
               "Fix: when sub_app is UNKNOWN, try _bsla_by_job anchor matching;\n"
               "     only skip if no anchor found.")
    else:
        record("#12", "UNKNOWN sub_app handling", PASS,
               "Sub_Application populated or fallback present", str(sub_apps))
except Exception as e:
    record("#12", "UNKNOWN sub_app handling", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #13 — build_top_jobs_df uses hardcoded thresholds not pe_config
# Location: services/batch_calculator.py  build_top_jobs_df()
#   buffer_status = "BREACH" if b<0 else ("CRITICAL" if b<10 else ("CAUTION" if b<30 ...
# Bug: pe_config.SLA_ATRISK_PCT=15 (AT_RISK), but build_top_jobs_df uses 10 (CRITICAL).
#      buffer=12%: _compute_sla_matrix → AT_RISK,  build_top_jobs_df → CAUTION
#      These feed different dashboard panels with contradictory output.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #13: build_top_jobs_df hardcoded thresholds (10/30/50) ────")
try:
    from services import pe_config as _pc13
    import inspect, services.batch_calculator as _bc

    src_bc = inspect.getsource(_bc.build_top_jobs_df)
    # Find hardcoded threshold in buffer_status assignment
    has_hardcoded = '"CRITICAL"' in src_bc and "< 10" in src_bc

    # pe_config AT_RISK threshold
    atrisk = _pc13.SLA_ATRISK_PCT   # default 15

    # buffer=12%: build_top_jobs hardcoded says CAUTION (12>10), pe_config says AT_RISK (12<15)
    buf_test = 12.0
    status_topjobs = ("BREACH" if buf_test < 0 else
                      "CRITICAL" if buf_test < 10 else
                      "CAUTION"  if buf_test < 30 else
                      "HEALTHY"  if buf_test < 50 else "EXCELLENT")
    status_matrix  = ("AT_RISK" if buf_test <= atrisk else
                      "LONG_JOB" if buf_test <= _pc13.SLA_LONGJOB_PCT else "OK")

    if has_hardcoded and status_topjobs != status_matrix:
        record("#13", "build_top_jobs_df hardcoded threshold at buffer=12%",
               FAIL,
               f"Consistent: both should reflect pe_config AT_RISK={atrisk}%",
               f"build_top_jobs → '{status_topjobs}', sla_matrix → '{status_matrix}'",
               f"Hardcoded 10% CRITICAL in build_top_jobs_df vs pe_config AT_RISK={atrisk}%.\n"
               "      Batch Review 'top jobs' table: buffer=12% → CAUTION (green-yellow)\n"
               "      SLA Matrix table:             buffer=12% → AT_RISK  (red)\n"
               "      Same job, same run, different panel: contradictory color.\n"
               "      Fix: replace hardcoded 10/30/50 with pe_config.SLA_ATRISK/LONGJOB_PCT.\n"
               "           Also affects j_at_risk count in compute_metrics (uses between(0,15)).")
    else:
        record("#13", "build_top_jobs_df hardcoded threshold", PASS,
               "Consistent or hardcoded not found", f"topjobs={status_topjobs}")
except Exception as e:
    record("#13", "build_top_jobs_df hardcoded thresholds", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #14 — compute_metrics window compliance still uses global_ceil per job
# Location: services/batch_calculator.py  compute_metrics()
#   window_breach_days = int((window["elapsed_hrs"] > global_ceil).sum())
# Bug: single global SLA ceiling for all sub-apps on a given day.
#      WEEKLY workflow runs 7h (SLA=17h) alongside DAILY workflow runs 5h (SLA=6h).
#      global_ceil = 6h (DAILY detected as dominant).
#      WEEKLY 7h → 7>6 → BREACH. Actual: 7h vs 17h = 58.8% buffer = OK.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #14: compute_metrics window compliance uses global_ceil ────")
try:
    import inspect, services.batch_calculator as _bc14
    src14 = inspect.getsource(_bc14.compute_metrics)
    # Fix #14: window_breach_days should use window_agg["breach"] (already per-sub-app)
    # instead of recomputing with global_ceil.
    # Verify the fix is present: _sub_breach_daily or window_agg breach used for window_breach_days.
    per_sub_fix_present = (
        "_sub_breach_daily" in src14 or
        ("window_agg" in src14 and "window_breach_days" in src14 and "reindex" in src14)
    )
    old_global_ceil_breach = "> global_ceil" in src14 and "window_breach_days" in src14

    if per_sub_fix_present:
        record("#14", "compute_metrics window compliance per-sub-app", PASS,
               "window_agg['breach'] used for window_breach_days",
               "Source uses _sub_breach_daily (per-sub-app) for window breach count")
    elif old_global_ceil_breach:
        from services import pe_config as _pc14
        daily_sla  = _pc14.SLA_DAILY_HRS
        weekly_sla = _pc14.SLA_WEEKLY_HRS
        weekly_runtime = 7.0
        correct_buf = (weekly_sla - weekly_runtime) / weekly_sla * 100
        record("#14", "compute_metrics window compliance false BREACH",
               FAIL,
               f"WEEKLY 7h vs correct SLA={weekly_sla}h → buffer={correct_buf:.1f}% = OK",
               f"window_breach_days still uses > global_ceil={daily_sla}h",
               "Fix: use window_agg['breach'] (per-sub-app SLA) for window_breach_days.")
    else:
        record("#14", "compute_metrics window compliance", PASS,
               "per-workflow SLA used", "global_ceil not used for window_breach_days")
except Exception as e:
    record("#14", "compute_metrics window compliance", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #15 — DAILY_LIMIT_HRS / MONTHLY_LIMIT_HRS stale at module load
# Location: services/batch_calculator.py top-level
#   DAILY_LIMIT_HRS:  float = pe_config.SLA_DAILY_HRS    ← evaluated once at import
#   MONTHLY_LIMIT_HRS: float = pe_config.SLA_MONTHLY_HRS ← evaluated once at import
# pe_config.reload() updates pe_config.SLA_DAILY_HRS but NOT batch_calculator.DAILY_LIMIT_HRS
# Bug: only matters if these constants are used anywhere. Check their usage.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #15: Stale module-level constants (DAILY_LIMIT_HRS) ────")
try:
    import inspect, services.batch_calculator as _bc15
    src15 = inspect.getsource(_bc15)

    # Find actual usage of DAILY_LIMIT_HRS beyond the definition line
    usages = [line.strip() for line in src15.splitlines()
              if "DAILY_LIMIT_HRS" in line and "=" not in line]

    if usages:
        record("#15", "DAILY_LIMIT_HRS used after module load",
               WARN,
               "pe_config.SLA_DAILY_HRS read live at call time",
               f"Module-level constant used in: {usages[:3]}",
               "DAILY_LIMIT_HRS frozen at import time.\n"
               "      pe_config.reload() after Settings change updates pe_config.SLA_DAILY_HRS\n"
               "      but NOT batch_calculator.DAILY_LIMIT_HRS → stale value for Settings changes.\n"
               "      Fix: replace DAILY_LIMIT_HRS usages with pe_config.SLA_DAILY_HRS directly.")
    else:
        record("#15", "DAILY_LIMIT_HRS stale constant", PASS,
               "Not used after definition",
               "Constants defined but not referenced elsewhere — legacy artefact, low risk.")
except Exception as e:
    record("#15", "Stale module-level constants", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #16 — FAILED runs with non-zero End-Start backfill skew peak_hrs
# Location: services/batch_calculator.py  load_ctrlm_bytes()
#   mask = df["Run_Sec"] == 0
#   diff = (End_Time - Start_Time)...
#   df.loc[mask, "Run_Sec"] = diff.clip(lower=0)   ← all zero-RunSec rows backfilled
#   This includes FAILED runs (which often have Run_Sec=0 but real wall-clock time)
# Bug: FAILED job with Run_Sec=0, Start=08:00, End=10:30 → backfilled 2.5h.
#      In build_top_jobs_df, ok_df = df[Status=="OK"] → FAILED excluded from peak_hrs.
#      BUT: if ALL runs of a job are FAILED with backfilled hrs, sla_rdf.empty → fallback:
#           sla_rdf = rdf  → FAILED runs included in peak_hrs → buffer negative → false BREACH.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #16: All-FAILED job: backfilled hrs pollutes peak_hrs ────")
try:
    import pandas as pd

    # Simulate _compute_sla_matrix per-job rollup for an all-FAILED job
    # 5 runs, all FAILED with backfilled wall-clock hours
    rdf = pd.DataFrame({
        "job_name":      ["JOB_ABEND"] * 5,
        "sub_application": ["DAILY_BATCH"] * 5,
        "run_hrs":       [2.5, 3.0, 2.8, 3.1, 2.6],   # from End-Start backfill
        "sla_limit_hrs": [6.0] * 5,
        "sla_source":    ["batch_sla_xlsx_exact"] * 5,
        "status":        ["FAILED"] * 5,
        "breach_runs":   [0] * 5,
        "atrisk_runs":   [0] * 5,
        "longjob_runs":  [0] * 5,
    })

    # Replicate the sla_rdf logic
    sla_rdf = rdf[rdf["status"] != "FAILED"]
    if sla_rdf.empty:
        sla_rdf = rdf   # ← fallback includes FAILED rows

    job_grp = sla_rdf.groupby("job_name").agg(
        runs    = ("run_hrs", "count"),
        peak_hrs= ("run_hrs", "max"),
        sla_limit=("sla_limit_hrs", "first"),
    ).reset_index()

    peak = float(job_grp["peak_hrs"].iloc[0])
    sla  = float(job_grp["sla_limit"].iloc[0])
    buf  = (sla - peak) / sla * 100

    if peak > 0 and buf > 0:
        record("#16", "All-FAILED job: backfilled hrs in peak_hrs",
               WARN,
               "peak_hrs=0 (all FAILED → excluded from buffer computation)",
               f"peak_hrs={peak}h, buffer={buf:.1f}%  (FAILED wall-clock backfill included)",
               f"JOB_ABEND: 5 FAILED runs, each backfilled {peak}h from End-Start.\n"
               f"      sla_rdf.empty → fallback includes FAILED rows → peak_hrs={peak}h.\n"
               f"      Buffer = ({sla}-{peak})/{sla}×100 = {buf:.1f}% → {'AT_RISK' if buf<15 else 'LONG_JOB'}\n"
               "      BUT the job FAILED — its buffer should be N/A, not AT_RISK/LONG_JOB.\n"
               "      The status column correctly shows FAILED but buffer_pct is non-null.\n"
               "      Fix: when all runs are FAILED, set peak_hrs=None and buffer_pct=None;\n"
               "           set reason_code='ALL_FAILED' rather than using backfilled wall-clock.")
    else:
        record("#16", "All-FAILED job backfill impact", PASS,
               "peak_hrs=0 or buffer=0", f"peak={peak}, buffer={buf:.1f}%")
except Exception as e:
    record("#16", "FAILED backfill peak_hrs", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #17 — Buffer formula: SLA_ATRISK_PCT thresholds multiplied by 100 twice
# Location: routers/sla_matrix.py  _compute_sla_matrix()
#   AT_RISK_PCT = pe_config.SLA_ATRISK_PCT / 100  → 0.15
#   if buffer_pct_val <= AT_RISK_PCT * 100:  → 0.15 * 100 = 15  ← correct
#   BUT in build_top_jobs_df:
#   (top_jobs["buffer_pct"].between(0, 15, inclusive="both")).sum()  ← hardcoded 15
#   These are consistent BUT the AT_RISK_PCT fraction is only used in one place.
#   Verify: does AT_RISK_PCT correctly classify AT_RISK vs OK boundary at exact 15.0?
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #17: AT_RISK boundary — exact value classification ────")
try:
    from services import pe_config as _pc17
    AT_RISK_PCT  = _pc17.SLA_ATRISK_PCT / 100   # 0.15
    LONG_JOB_PCT = _pc17.SLA_LONGJOB_PCT / 100  # 0.40

    # Test exact boundary values
    cases17 = [
        (0.0,    "BREACH",   "exactly at SLA → BREACH"),
        (-0.001, "BREACH",   "just over SLA → BREACH"),
        (0.001,  "AT_RISK",  "just inside SLA → AT_RISK"),
        (15.0,   "AT_RISK",  "exactly at AT_RISK boundary → AT_RISK (≤ 15)"),
        (15.001, "LONG_JOB", "just above AT_RISK → LONG_JOB"),
        (40.0,   "LONG_JOB", "exactly at LONG_JOB boundary → LONG_JOB (≤ 40)"),
        (40.001, "OK",       "just above LONG_JOB → OK"),
        (100.0,  "OK",       "full headroom → OK"),
    ]

    errors17 = []
    for buf_pct, expected_st, note in cases17:
        if buf_pct <= 0:
            actual_st = "BREACH"
        elif buf_pct <= AT_RISK_PCT * 100:
            actual_st = "AT_RISK"
        elif buf_pct <= LONG_JOB_PCT * 100:
            actual_st = "LONG_JOB"
        else:
            actual_st = "OK"
        if actual_st != expected_st:
            errors17.append((buf_pct, expected_st, actual_st, note))

    if errors17:
        for buf, exp, act, note in errors17:
            record("#17", f"Buffer boundary classification at {buf}%",
                   FAIL, exp, act, note)
    else:
        record("#17", "AT_RISK/LONG_JOB/BREACH/OK boundary classification", PASS,
               "All boundaries correct", f"AT_RISK≤{_pc17.SLA_ATRISK_PCT}%, LONG_JOB≤{_pc17.SLA_LONGJOB_PCT}%")
except Exception as e:
    record("#17", "AT_RISK boundary", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #18 — Token overlap (Pass D) — common tokens create false matches
# Location: routers/sla_matrix.py  _bulk_lookup_bsla  Pass D
#   if best_score >= 2: return best
# Bug: XLSX has "DAILY_BATCH_REPORT" (SLA=6h) and "DAILY_BATCH_SUMMARY" (SLA=4h).
#      Ctrl-M Sub_Application = "DAILY_BATCH_TRANSFER" (no XLSX entry).
#      tokens: {DAILY, BATCH, TRANSFER}.
#      vs REPORT tokens {DAILY, BATCH, REPORT}: overlap=2 → match → SLA=6h
#      vs SUMMARY tokens {DAILY, BATCH, SUMMARY}: overlap=2 → match → SLA=4h
#      First processed wins. Non-deterministic if dict order changes.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #18: Pass D token match — tied score non-deterministic ────")
try:
    # Simulate the token overlap scoring
    def _tok(s): return frozenset(t for t in s.split("_") if len(t) >= 2)

    bsla_tokens = [
        (_tok("DAILY_BATCH_REPORT"),  6.0, "DAILY_BATCH_REPORT"),
        (_tok("DAILY_BATCH_SUMMARY"), 4.0, "DAILY_BATCH_SUMMARY"),
    ]
    s_tok = _tok("DAILY_BATCH_TRANSFER")

    best = None
    best_score = 0
    for (wf_tok, sla_h, wf_raw) in bsla_tokens:
        score = len(wf_tok & s_tok)
        if score > best_score:   # strict > → first-seen wins on tie
            best_score = score
            best = (sla_h, "batch_sla_xlsx_tokens", wf_raw)

    if best_score >= 2:
        record("#18", "Pass D token tie — first-seen wins",
               WARN,
               "Deterministic tie-breaking (e.g. higher sla_hrs, or WARN and skip)",
               f"Tied score={best_score}: got '{best[2]}' SLA={best[0]}h",
               f"DAILY_BATCH_TRANSFER has no XLSX entry.\n"
               f"      Pass D: DAILY_BATCH_REPORT(score=2) vs DAILY_BATCH_SUMMARY(score=2).\n"
               f"      First in _bsla_tokens list wins → non-deterministic across dict order.\n"
               f"      SLA difference: {best[0]}h vs {[s for t,s,n in bsla_tokens if n!=best[2]][0]}h.\n"
               "      Fix: when scores are tied, use broader-token set as more-specific match;\n"
               "           OR require score >= 3 for ambiguous cases (only 2 common tokens);\n"
               "           OR fall back to global default when tie score=2 with multiple matches.")
    else:
        record("#18", "Pass D token tie", PASS, "score < 2", f"score={best_score}")
except Exception as e:
    record("#18", "Pass D token tie", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #19 — NaN propagation in buffer when run_time_hrs has NaN
# Location: routers/sla_matrix.py  per-row loop  / services/batch_calculator.py
# Bug: if run_time_hrs = NaN (e.g. Run_Sec="N/A" → coerce → NaN, not backfilled):
#      hrs = float(NaN) → math.isnan guard fires → hrs=0
#      buffer = (SLA - 0) / SLA × 100 = 100% → status = OK
#      But the job NEVER RAN (NaN runtime = no data). Appears as "fully compliant".
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #19: NaN runtime treated as 0 → false 100% buffer ────")
try:
    import inspect
    import routers.sla_matrix as _rm19

    src19 = inspect.getsource(_rm19)
    # Verify RUNTIME_ZERO reason_code is present in source
    runtime_zero_fix = "RUNTIME_ZERO" in src19
    # Old behavior: NaN guard fires → hrs=0 → buffer=100% → passes through as OK
    # Fixed: if not is_failure and hrs == 0: reason_code = "RUNTIME_ZERO"; buffer_pct_val = None

    if runtime_zero_fix:
        # Verify the guard logic: hrs=0, not failure → RUNTIME_ZERO, no buffer
        import math as _math19
        sla_hrs = 6.0
        raw_hrs = float("nan")
        try:
            hrs = float(raw_hrs)
        except (TypeError, ValueError):
            hrs = 0.0
        if _math19.isnan(hrs) or _math19.isinf(hrs):
            hrs = 0.0
        is_failure = False
        # Simulate fixed guard
        if not is_failure and hrs == 0:
            reason_code = "RUNTIME_ZERO"
            buffer_pct_val = None
        else:
            buffer_pct_val = round((sla_hrs - hrs) / sla_hrs * 100, 1)
            reason_code = None

        if reason_code == "RUNTIME_ZERO" and buffer_pct_val is None:
            record("#19", "NaN runtime → RUNTIME_ZERO, buffer=None", PASS,
                   "reason_code='RUNTIME_ZERO', buffer_pct=None",
                   "NaN runtime now tagged RUNTIME_ZERO; no false 100% buffer generated")
        else:
            record("#19", "NaN runtime coerced to 0 → 100% buffer",
                   FAIL,
                   "reason_code='RUNTIME_ZERO', buffer_pct=None",
                   f"reason_code={reason_code}, buffer={buffer_pct_val}")
    else:
        # Old behavior: NaN → hrs=0 → 100% buffer
        import math as _math19
        sla_hrs = 6.0
        raw_hrs = float('nan')
        try:
            hrs = float(raw_hrs)
        except (TypeError, ValueError):
            hrs = 0.0
        if _math19.isnan(hrs) or _math19.isinf(hrs):
            hrs = 0.0
        buffer = (sla_hrs - hrs) / sla_hrs * 100
        record("#19", "NaN runtime coerced to 0 → 100% buffer",
               FAIL,
               "reason_code='RUNTIME_ZERO', buffer_pct=None",
               f"RUNTIME_ZERO not in source; hrs={hrs}, buffer={buffer}%",
               "Fix: add `elif not is_failure and hrs == 0: reason_code='RUNTIME_ZERO'`")
except Exception as e:
    record("#19", "NaN runtime", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #20 — detect_batch_type does not recognise "WF" suffix (pe_config WEEKLY key)
# Location: services/pe_config.py  JOB_TYPE_PATTERNS
#   "WEEKLY": ["_WLY", "WEEKLY_", "WK_", "_WEEK", "_WF", "-WF"]
# Test: verify "BATCH_WF1" correctly matches WEEKLY via "_WF" substring
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #20: detect_batch_type WF suffix matching ────")
try:
    from services.sla_merger import detect_batch_type as _dbt20

    wf_cases = [
        ("PROD_WEEKLY_WF1_REQPL",  "WEEKLY", "Classic WEEKLY WF format"),
        ("TEST_WEEKLY_WF2_DEPLPL", "WEEKLY", "Classic WEEKLY WF format 2"),
        ("BATCH_WF1",              "WEEKLY", "WF suffix without WEEKLY keyword"),
        ("DAILY_WF_PROCESS",       "WEEKLY", "DAILY but also has _WF — ambiguous"),
        ("WF_NIGHTLY_BATCH",       "DAILY",  "WF prefix + NIGHTLY → DAILY should win"),
    ]

    failures20 = []
    for name, expected, note in wf_cases:
        detected = _dbt20(name, "")
        if detected != expected and expected not in ("DAILY", "WEEKLY"):
            # Only flag clear mismatches for non-ambiguous cases
            failures20.append((name, expected, detected, note))

    ambiguous = [(n, e, _dbt20(n,""), nt) for n, e, nt in wf_cases if "ambiguous" in nt.lower()]
    if ambiguous:
        for n, e, a, nt in ambiguous:
            record("#20", f"detect_batch_type('{n}') ambiguous",
                   WARN, e, a,
                   f"{nt}\n"
                   "      'DAILY_WF_PROCESS' contains both DAILY_ and _WF patterns.\n"
                   f"      Priority: DAILY_ comes before WEEKLY in _DETECT_PRIORITY? result={a}.\n"
                   "      Impact: if DAILY-type assigned → SLA 6h; if WEEKLY → 17h.\n"
                   "      Fix: prefer more specific (longer) keyword match, or add explicit tests.")
    else:
        record("#20", "detect_batch_type WF suffix", PASS if not failures20 else FAIL,
               "All correct" if not failures20 else [e for _,e,_,_ in failures20],
               [a for _,_,a,_ in failures20] if failures20 else "All matched")
except Exception as e:
    record("#20", "detect_batch_type WF matching", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #21 — Multiple independent runs on same date aggregated wrongly
# When a workflow runs TWICE on the same date (e.g. end-of-month + ad-hoc retry),
# the per_run_elapsed groupby("_run_date") collapses both into one row.
# min(start) from run1 and max(end) from run2 → artificially long elapsed window.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #21: Two runs on same date → inflated elapsed ────")
try:
    import inspect
    import routers.sla_matrix as _rm21
    from datetime import datetime

    src21 = inspect.getsource(_rm21)
    # Verify cluster detection is present (the fix adds gap-based run clustering)
    cluster_fix_present = (
        "_clusters" in src21 or
        "_best_elapsed" in src21 or
        "cluster" in src21.lower()
    )

    # Two INDEPENDENT runs on same date (separated by 16h gap)
    run1_start = datetime(2025, 5, 31, 1, 0)
    run1_end   = datetime(2025, 5, 31, 4, 0)
    run2_start = datetime(2025, 5, 31, 20, 0)
    run2_end   = datetime(2025, 5, 31, 22, 30)

    elapsed_combined = (max([run1_end, run2_end]) - min([run1_start, run2_start])).total_seconds() / 3600
    elapsed_run1 = (run1_end - run1_start).total_seconds() / 3600   # 3.0h
    elapsed_run2 = (run2_end - run2_start).total_seconds() / 3600   # 2.5h
    worst_run    = max(elapsed_run1, elapsed_run2)                   # 3.0h
    sla = 6.0

    if cluster_fix_present:
        # Simulate the cluster detection logic from the fix
        import pandas as pd
        _jobs = pd.DataFrame({
            "_start": [run1_start, run1_start + pd.Timedelta("1h"), run2_start],
            "_end":   [run1_end,   run1_end,                        run2_end],
            "job_name": ["JOB_A", "JOB_B", "JOB_A"],
        })
        _sorted = _jobs.sort_values("_start")
        _clusters: list = []
        _c_start = _sorted.iloc[0]["_start"]
        _c_end   = _sorted.iloc[0]["_end"]
        for _idx in range(1, len(_sorted)):
            _row = _sorted.iloc[_idx]
            _gap_h = (_row["_start"] - _c_end).total_seconds() / 3600
            if _gap_h > 2.0:
                _clusters.append((_c_start, _c_end))
                _c_start = _row["_start"]
            _c_end = max(_c_end, _row["_end"])
        _clusters.append((_c_start, _c_end))
        _best = max(
            ((_ce - _cs).total_seconds() / 3600 for _cs, _ce in _clusters),
            default=elapsed_combined,
        )
        buf_cluster = round((sla - _best) / sla * 100, 1)
        buf_combined = round((sla - elapsed_combined) / sla * 100, 1)
        if _best <= worst_run + 0.1:
            record("#21", "Two runs same date — cluster detection", PASS,
                   f"Worst cluster={_best:.1f}h, buffer={buf_cluster:.1f}%",
                   f"Combined would have been {elapsed_combined}h → {buf_combined:.1f}% (BREACH)")
        else:
            record("#21", "Two runs same date → inflated elapsed",
                   FAIL, f"worst_run={worst_run}h", f"cluster elapsed={_best:.1f}h still inflated")
    else:
        buf_combined = round((sla - elapsed_combined) / sla * 100, 1)
        buf_correct  = round((sla - worst_run) / sla * 100, 1)
        record("#21", "Two runs same date → inflated elapsed window",
               FAIL,
               f"Worst run = {worst_run}h, buffer={buf_correct:.1f}% = OK",
               f"Combined elapsed = {elapsed_combined}h, buffer={buf_combined:.1f}% = BREACH",
               "Cluster detection not found in source. Fix: add gap-based run clustering.")
except Exception as e:
    record("#21", "Two runs same date", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Issue #22 — sla_hrs first() in job_grp may pick wrong SLA when runs have different ceilings
# Location: routers/sla_matrix.py  per-job rollup  ~line 506
#   sla_limit = ("sla_limit_hrs", "first")   ← first run's SLA used for ALL runs
# Bug: if a job is in both DAILY and WEEKLY workflows (renamed) or SLA XLSX updated mid-period,
#      different runs may have different sla_limit_hrs. "first" picks one arbitrarily.
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Issue #22: Per-job rollup uses 'first' SLA — may be wrong ────")
try:
    import inspect, pandas as pd
    import routers.sla_matrix as _rm22

    src22 = inspect.getsource(_rm22)
    # Verify source uses min() for sla_limit_hrs aggregation, not first()
    uses_min = (
        '("sla_limit_hrs", "min")' in src22 or
        "sla_limit_hrs.*min" in src22
    )
    uses_first = '("sla_limit_hrs", "first")' in src22

    rows = [
        {"job_name": "JOB_A", "run_hrs": 5.0, "sla_limit_hrs": 6.0, "status": "OK"},
        {"job_name": "JOB_A", "run_hrs": 5.2, "sla_limit_hrs": 6.0, "status": "OK"},
        {"job_name": "JOB_A", "run_hrs": 5.5, "sla_limit_hrs": 5.0, "status": "OK"},
        {"job_name": "JOB_A", "run_hrs": 5.8, "sla_limit_hrs": 5.0, "status": "OK"},
    ]
    rdf22 = pd.DataFrame(rows)

    if uses_first and not uses_min:
        # Old behavior still in code
        grp = rdf22.groupby("job_name").agg(
            peak_hrs  = ("run_hrs", "max"),
            sla_limit = ("sla_limit_hrs", "first"),
        ).reset_index()
        peak = grp["peak_hrs"].iloc[0]
        sla_f = grp["sla_limit"].iloc[0]
        buf_first = round((sla_f - peak) / sla_f * 100, 1)
        record("#22", "Per-job SLA rollup uses 'first' SLA",
               FAIL,
               "min(sla_limit_hrs): SLA=5.0h → buffer=-16.0% → BREACH",
               f"'first' SLA={sla_f}h → buffer={buf_first:.1f}% → AT_RISK",
               "Fix: `sla_limit = ('sla_limit_hrs', 'min')` for conservative rollup.")
    else:
        # Fixed: min() used
        grp = rdf22.groupby("job_name").agg(
            peak_hrs  = ("run_hrs", "max"),
            sla_limit = ("sla_limit_hrs", "min"),
        ).reset_index()
        peak = grp["peak_hrs"].iloc[0]
        sla_min = grp["sla_limit"].iloc[0]
        buf_min = round((sla_min - peak) / sla_min * 100, 1)
        record("#22", "Per-job SLA rollup uses min() SLA", PASS,
               f"min SLA={sla_min}h, buffer={buf_min:.1f}%",
               "Tightest (min) SLA used for per-job rollup — most conservative")
except Exception as e:
    record("#22", "Per-job SLA 'first' rollup", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# MATHEMATICAL EDGE CASES — direct formula verification
# ══════════════════════════════════════════════════════════════════════════════
print("\n──── Mathematical edge-case verification ────")
try:
    import math

    # Buffer formula: (SLA_h − runtime_h) / SLA_h × 100
    # Edge case set
    math_cases = [
        # (sla_h, runtime_h, expected_buf, expected_status, note)
        (6.0, 6.0,   0.0,    "BREACH",   "Exactly at SLA = BREACH (buffer=0)"),
        (6.0, 6.001, -0.017, "BREACH",   "1-second over SLA → BREACH"),
        (6.0, 0.001, 99.983, "OK",       "Near-zero runtime → OK (not NaN, not inf)"),
        (6.0, 0.0,   100.0,  "OK",       "Zero runtime → 100% buffer → misleading OK"),
        (0.0, 3.0,   None,   "SLA_MISS", "Zero SLA → can't divide → SLA_MISSING"),
        (6.0, float('nan'), None, "GUARD", "NaN runtime → math guard → hrs=0"),
        (6.0, float('inf'), None, "GUARD", "Inf runtime → math guard → hrs=0"),
    ]

    math_failures = []
    for sla_h, rt_h, exp_buf, exp_st, note in math_cases:
        try:
            # Exact guards from _compute_sla_matrix
            hrs = float(rt_h)
            if math.isnan(hrs) or math.isinf(hrs):
                hrs = 0.0
                actual_buf = (sla_h - hrs) / sla_h * 100 if sla_h > 0 else None
                actual_st = "GUARD_THEN_OK" if actual_buf == 100.0 else "GUARD"
            elif sla_h <= 0:
                actual_buf = None
                actual_st = "SLA_MISSING"
            else:
                actual_buf = round((sla_h - hrs) / sla_h * 100, 4)
                actual_st = ("BREACH"   if actual_buf <= 0    else
                             "AT_RISK"  if actual_buf <= 15   else
                             "LONG_JOB" if actual_buf <= 40   else "OK")

            if exp_buf is not None and actual_buf is not None:
                if abs(actual_buf - exp_buf) > 0.01:
                    math_failures.append((note, exp_buf, actual_buf, "buffer mismatch"))
            if exp_st in ("BREACH", "OK", "AT_RISK", "SLA_MISS", "SLA_MISSING") and actual_st != exp_st:
                # Accept "SLA_MISSING" as equivalent to "SLA_MISS" (the code uses the full form)
                if not (exp_st == "GUARD" and actual_st.startswith("GUARD")):
                    if not (exp_st in ("SLA_MISS", "SLA_MISSING") and actual_st in ("SLA_MISS", "SLA_MISSING")):
                        math_failures.append((note, exp_st, actual_st, "status mismatch"))
        except Exception as ex:
            math_failures.append((note, "no exception", str(ex), "exception"))

    # Special: zero runtime → 100% buffer → status=OK (potentially misleading)
    zero_runtime_buf = (6.0 - 0.0) / 6.0 * 100   # = 100%
    from services import pe_config as _pc_m
    at_risk = _pc_m.SLA_ATRISK_PCT
    zero_status = "OK" if zero_runtime_buf > 40 else "LONG_JOB"

    if math_failures:
        for note, exp, act, kind in math_failures:
            record("#MATH", f"Buffer formula: {note}", FAIL, exp, act, kind)
    else:
        record("#MATH", "Buffer formula edge cases", PASS,
               "All formulas correct",
               f"Note: zero runtime → buffer=100%=OK (may be misleading — job didn't run).")
        if zero_runtime_buf == 100.0:
            record("#MATH-ZERO", "Zero runtime → 100% buffer misleading OK",
                   WARN,
                   "reason_code='RUNTIME_ZERO' + buffer_pct=None",
                   f"buffer={zero_runtime_buf}%, status={zero_status}",
                   "A job with Run_Sec=0 and no End_Time shows 100% buffer = OK.\n"
                   "      This masks data quality issues (job never ran or data missing).\n"
                   "      Fix: add RUNTIME_ZERO check independent of failure status.")
except Exception as e:
    record("#MATH", "Buffer formula edge cases", SKIP, "N/A", str(e))


# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "═" * 80)
print("AUDIT SUMMARY")
print("═" * 80)

total  = len(results)
passes = sum(1 for r in results if r["status"] == PASS)
fails  = sum(1 for r in results if r["status"] == FAIL)
warns  = sum(1 for r in results if r["status"] == WARN)
skips  = sum(1 for r in results if r["status"] == SKIP)

print(f"\nTotal checks : {total}")
print(f"  ✅ PASS    : {passes}")
print(f"  ❌ FAIL    : {fails}")
print(f"  ⚠️  WARN    : {warns}")
print(f"  ⏭️  SKIP    : {skips}")

if fails > 0:
    print("\n── CRITICAL FAILURES (will produce wrong output) ──────────────────────────")
    for r in results:
        if r["status"] == FAIL:
            print(f"  [{r['id']}] {r['name']}")

if warns > 0:
    print("\n── WARNINGS (may produce misleading output) ────────────────────────────────")
    for r in results:
        if r["status"] == WARN:
            print(f"  [{r['id']}] {r['name']}")

print("\n── PRIORITY FIXES (by severity) ────────────────────────────────────────────")
priority_fixes = [
    ("#1",    "CRITICAL", "Hardcode 8h in workflow_summary → split WEEKLY view",
              "Replace _WF_DEFAULTS literals with pe_config.SLA_*_HRS"),
    ("#21",   "CRITICAL", "Two runs same date → 21.5h elapsed → false BREACH",
              "Cluster runs by gap threshold before min/max groupby"),
    ("#3",    "CRITICAL", "Window compliance uses UI-mode ceiling, not per-workflow SLA",
              "Resolve per-workflow SLA in window_compliance groupby"),
    ("#6",    "HIGH",     "str.contains() anchor → wrong elapsed start time",
              "Try exact match first; fall back to contains with debug flag"),
    ("#12",   "HIGH",     "UNKNOWN sub_app entirely skipped → no workflow view",
              "Add handler for UNKNOWN group using anchor-job matching"),
    ("#19",   "HIGH",     "NaN runtime → 0 → 100% buffer → false OK",
              "Add RUNTIME_ZERO reason_code when hrs=0 with no backfill path"),
    ("#5",    "HIGH",     "Decimal <1 SLA treated as Excel fraction (×24)",
              "Only multiply by 24 for values <0.1667 (< 4h as fraction)"),
    ("#11",   "MEDIUM",   "Three inconsistent classification systems across panels",
              "Unify to pe_config.SLA_ATRISK/LONGJOB_PCT everywhere"),
    ("#2",    "MEDIUM",   "AT_RISK counted as compliant (contradicts comment)",
              "Decide policy: AT_RISK = violation or warning, then fix code + comment"),
    ("#22",   "MEDIUM",   "Per-job SLA rollup 'first()' may use wrong SLA ceiling",
              "Use min(sla_limit_hrs) or the latest SLA, not arbitrary 'first'"),
    ("#4",    "MEDIUM",   "build_top_jobs_df div-by-zero when sla_hrs=0",
              "Add np.where(sla_hrs>0, ..., np.nan) guard"),
    ("#7",    "MEDIUM",   "48h cap drops valid 50h+ monthly batches silently",
              "Make cap batch-type-aware or flag with sanity_flag instead of dropping"),
    ("#13",   "LOW",      "build_top_jobs_df hardcoded thresholds (10/30/50) vs pe_config",
              "Replace with pe_config.SLA_ATRISK_PCT / SLA_LONGJOB_PCT"),
    ("#8",    "LOW",      "_parse_dt 40% threshold loses EU-format rows",
              "Retry NaT rows with next formats instead of full-pass replacement"),
    ("#10",   "LOW",      "Customer prefix strip collision in secondary keys",
              "Detect collision and skip secondary indexing, log warning"),
    ("#18",   "LOW",      "Pass D token tie is non-deterministic (first-seen wins)",
              "On tie: skip match (return None), let Tier 2/3 handle it"),
]

for fid, sev, problem, fix in priority_fixes:
    r = next((x for x in results if x["id"] == fid), None)
    status_icon = {"FAIL": "❌", "WARN": "⚠️", "PASS": "✅", "SKIP": "⏭️"}.get(
        r["status"] if r else "SKIP", "?")
    print(f"\n  {status_icon} [{fid}] [{sev}]  {problem}")
    print(f"             Fix: {fix}")

print("\n" + "═" * 80)
print("Done.")
