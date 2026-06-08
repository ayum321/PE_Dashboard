"""
UI Benchmark Comparison router.

POST /api/benchmark
    Accepts a multipart upload of an XLSX/CSV containing baseline + current
    UI performance metrics and returns a structured comparison matrix.

POST /api/benchmark/json
    Same but accepts pre-parsed rows in JSON body.

Supports two formats:
  A) Simple flat file: Transaction | Baseline (sec) | Current (sec) [| SLA]
  B) Multi-sheet PE validation XLSX with auto-detected sheet types:
       - Sheets with "fill" + "rate" in name      → PROD vs TEST fill rates
       - Sheets with "batch" + "run/time" in name  → PROD vs TEST batch runtimes
       - Sheets with "ui" + "perf" in name          → PROD vs TEST UI load times
       - Sheets with "observation" or "sit" in name → Issue tracker
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from services import config_store

router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────────

class BenchmarkRow(BaseModel):
    transaction:    str
    baseline_sec:   float
    current_sec:    float
    delta_pct:      float     # positive = regression, negative = improvement
    sla_sec:        Optional[float] = None
    sla_breach:     bool = False
    status:         str   # GREEN | AMBER | RED | N/A
    category:       str = ""           # UI Perf | Batch | Fill Rate etc.
    pass_fail:      Optional[str] = None   # original Pass/Fail from sheet
    export_time_baseline: Optional[float] = None
    export_time_current:  Optional[float] = None
    rows_baseline:  Optional[int] = None
    rows_current:   Optional[int] = None


class BenchmarkCategory(BaseModel):
    """Summary stats for one category (sheet)."""
    name:       str
    total:      int = 0
    passed:     int = 0
    failed:     int = 0
    degraded:   int = 0
    avg_delta:  float = 0.0


class BenchmarkResponse(BaseModel):
    filename:           str
    total_transactions: int
    degraded:           int      # > threshold %
    improved:           int
    unchanged:          int
    sla_breaches:       int
    avg_delta_pct:      float
    threshold_pct:      float    # configured degradation threshold
    rows:               List[BenchmarkRow]
    summary:            str
    categories:         List[BenchmarkCategory] = []
    fill_rate:          Optional[List[Dict[str, Any]]] = None  # fill rate time-series
    observations:       Optional[List[Dict[str, Any]]] = None  # SIT observations
    batch_perf_summary: Optional[Dict[str, Any]] = None         # batch runtime comparison summary
    ai_narrative:       Optional[str] = None
    ai_model:           Optional[str] = None


class JsonBenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    rows:       List[Dict[str, Any]]
    threshold:  Optional[float] = None
    filename:   Optional[str]   = "benchmark"


# ── Column detection ─────────────────────────────────────────────────────────

_TXN_COLS   = ["transaction", "txn", "page", "scenario", "test_case", "name",
               "transaction_name", "txnname", "step", "action",
               "worksheet", "worksheet_name", "page_name"]
_BASE_COLS  = ["baseline", "baseline_sec", "baseline_time", "ref", "reference",
               "base", "expected", "base_time", "previous", "prod"]
_CUR_COLS   = ["current", "current_sec", "current_time", "test", "actual",
               "result", "cur", "latest", "new"]
_SLA_COLS   = ["sla", "sla_sec", "target", "sla_limit", "threshold", "limit",
               "max_allowed"]


def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    h_map = {h.lower().replace(" ", "_").replace("-", "_"): h for h in headers}
    for c in candidates:
        if c in h_map:
            return h_map[c]
    # Fuzzy partial match
    for c in candidates:
        for h_norm, h_orig in h_map.items():
            if c in h_norm or h_norm in c:
                return h_orig
    return None


def _safe_float(v, default=0.0):
    if v is None:
        return default
    try:
        f = float(v)
        return f if f == f else default  # NaN guard
    except (ValueError, TypeError):
        return default


def _safe_int(v, default=None):
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


# ── Core computation ─────────────────────────────────────────────────────────

def _compute_benchmark(rows: list[dict], threshold_pct: float) -> tuple[list[BenchmarkRow], str]:
    results: list[BenchmarkRow] = []

    for r in rows:
        txn  = str(r.get("transaction", r.get("txn", r.get("name", "?"))))
        base = _safe_float(r.get("baseline_sec", r.get("baseline", 0)))
        cur  = _safe_float(r.get("current_sec",  r.get("current",  0)))
        sla  = r.get("sla_sec") or r.get("sla")
        sla  = float(sla) if sla else None
        cat  = str(r.get("category", ""))
        pf   = r.get("pass_fail")

        if base == 0:
            delta = 0.0
            status = "N/A"
        else:
            delta = round((cur - base) / base * 100, 2)
            if delta > threshold_pct:
                status = "RED"
            elif delta > threshold_pct * 0.5:
                status = "AMBER"
            elif delta < -5:
                status = "GREEN"
            else:
                status = "GREEN"

        # Override status from original Pass/Fail if present and regression
        if pf and str(pf).strip().lower() == "fail" and status != "RED":
            status = "RED"

        sla_breach = bool(sla and cur > sla)

        results.append(BenchmarkRow(
            transaction=txn, baseline_sec=base, current_sec=cur,
            delta_pct=delta, sla_sec=sla, sla_breach=sla_breach, status=status,
            category=cat, pass_fail=pf,
            export_time_baseline=r.get("export_time_baseline"),
            export_time_current=r.get("export_time_current"),
            rows_baseline=r.get("rows_baseline"),
            rows_current=r.get("rows_current"),
        ))

    # Summary line
    reds    = sum(1 for r in results if r.status == "RED")
    greens  = sum(1 for r in results if r.status == "GREEN")
    breaches= sum(1 for r in results if r.sla_breach)
    total   = len(results)

    if reds == 0 and breaches == 0:
        summary = f"✅ All {total} transactions within tolerance. No regressions detected."
    elif reds > 0:
        summary = (f"⚠️ {reds}/{total} transactions regressed >{threshold_pct}%. "
                   f"{breaches} SLA breach(es). Review red rows before go-live.")
    else:
        summary = f"⚠️ {breaches} SLA breach(es) detected. Verify root cause before go-live."

    return results, summary


# ── Parse XLSX/CSV ────────────────────────────────────────────────────────────

def _is_excel(raw_bytes: bytes, filename: str) -> bool:
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("xlsx", "xls", "xlsm", "xlsb"):
        return True
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b'PK\x03\x04':
        return True
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b'\xd0\xcf\x11\xe0':
        return True
    return False


def _is_legacy_xls(raw_bytes: bytes, filename: str) -> bool:
    if len(raw_bytes) >= 4 and raw_bytes[:4] == b'\xd0\xcf\x11\xe0':
        return True
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    return ext == "xls"


# ── Sheet type detection ──────────────────────────────────────────────────────

def _classify_sheet(name: str) -> str:
    """Classify sheet by name into a PE benchmark category."""
    nl = name.lower().strip()
    if "fill" in nl and "rate" in nl:
        return "fill_rate"
    if "batch" in nl and ("run" in nl or "time" in nl):
        return "batch"
    if "ui" in nl and "perf" in nl:
        return "ui_perf"
    if "observation" in nl or "sit" in nl:
        return "observations"
    if any(k in nl for k in ["demand", "esp", "need", "supply"]):
        if "batch" in nl:
            return "batch"
        return "ui_perf"
    return "unknown"


def _clean_category_name(sheet_name: str, suffix: str) -> str:
    """Derive a clean category label from the sheet name.
    Strips common noise words, appends suffix if not already present."""
    name = sheet_name.strip()
    # Remove common noise suffixes
    for noise in ["run time reports", "runtime reports", "run time report",
                  "perf summary", "performance summary", "summary",
                  "report", "reports"]:
        if name.lower().endswith(noise):
            name = name[:len(name) - len(noise)].strip(" -–—_")
    # Append the suffix if not already present
    if suffix.lower() not in name.lower():
        name = f"{name} {suffix}".strip()
    return name or suffix


# ── Sheet parsers ─────────────────────────────────────────────────────────────

def _parse_ui_perf_sheet(ws, sheet_name: str) -> list[dict]:
    """Parse UI Performance Summary sheets.
    Layout: Row 1 = env labels (TEST / PROD), Row 2 = column headers,
    Row 3+ = data. Columns: Worksheet | Search | TEST Load | TEST Export | TEST Rows | (blank) | PROD Load | PROD Export | PROD Rows | Status | Comments"""
    import openpyxl
    rows_out = []
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    if max_r < 3 or max_c < 7:
        return []

    # Find header row: look for a row containing "loading time" or "worksheet"
    hdr_row = None
    for r in range(1, min(max_r + 1, 6)):
        vals = [str(ws.cell(r, c).value or "").lower() for c in range(1, min(max_c + 1, 12))]
        joined = " ".join(vals)
        if "loading time" in joined or "export time" in joined:
            hdr_row = r
            break

    if hdr_row is None:
        return []

    # Determine column positions from header row + row above it (env labels)
    # Pattern: col A = worksheet, col B = search, then TEST block, blank col, PROD block
    # The env row (above header) tells us which side is PROD and which is TEST
    env_row = hdr_row - 1 if hdr_row > 1 else None
    env_labels = {}
    if env_row:
        for c in range(1, min(max_c + 1, 12)):
            v = str(ws.cell(env_row, c).value or "").strip().upper()
            if v and any(k in v for k in ["PROD", "TEST", "BASELINE", "CURRENT"]):
                env_labels[c] = v

    # Find loading-time columns
    load_cols = []
    export_cols = []
    rows_cols = []
    status_col = None
    for c in range(1, min(max_c + 1, 15)):
        v = str(ws.cell(hdr_row, c).value or "").lower()
        if "loading" in v or "load" in v:
            load_cols.append(c)
        elif "export" in v:
            export_cols.append(c)
        elif v in ("rows", "no. of rows", "no of rows"):
            rows_cols.append(c)
        elif "status" in v:
            status_col = c

    # Also check the env row for "Status" column (some sheets put it there)
    if status_col is None and env_row:
        for c in range(1, min(max_c + 1, 15)):
            v = str(ws.cell(env_row, c).value or "").lower().strip()
            if v == "status":
                status_col = c
                break

    if len(load_cols) < 2:
        return []

    # Determine which load col is PROD vs TEST using env_labels
    test_load_col, prod_load_col = load_cols[0], load_cols[1]
    test_export_col = export_cols[0] if len(export_cols) >= 1 else None
    prod_export_col = export_cols[1] if len(export_cols) >= 2 else None
    test_rows_col = rows_cols[0] if len(rows_cols) >= 1 else None
    prod_rows_col = rows_cols[1] if len(rows_cols) >= 2 else None

    # Check if the first group is actually PROD (env label check)
    for col_idx, label in env_labels.items():
        if "PROD" in label and col_idx <= load_cols[0]:
            # PROD is on the LEFT, swap
            test_load_col, prod_load_col = prod_load_col, test_load_col
            if test_export_col and prod_export_col:
                test_export_col, prod_export_col = prod_export_col, test_export_col
            if test_rows_col and prod_rows_col:
                test_rows_col, prod_rows_col = prod_rows_col, test_rows_col
            break

    # Derive category label from actual sheet name (generic)
    cat = _clean_category_name(sheet_name, "UI Performance")

    for r in range(hdr_row + 1, max_r + 1):
        txn = str(ws.cell(r, 1).value or "").strip()
        if not txn:
            continue

        prod_load = _safe_float(ws.cell(r, prod_load_col).value)
        test_load = _safe_float(ws.cell(r, test_load_col).value)
        status_val = str(ws.cell(r, status_col).value or "").strip() if status_col else None

        rec = {
            "transaction": txn,
            "baseline_sec": prod_load,   # PROD = baseline
            "current_sec":  test_load,   # TEST = current
            "category": cat,
            "pass_fail": status_val,
        }
        if prod_export_col and test_export_col:
            rec["export_time_baseline"] = _safe_float(ws.cell(r, prod_export_col).value) or None
            rec["export_time_current"]  = _safe_float(ws.cell(r, test_export_col).value) or None
        if prod_rows_col and test_rows_col:
            rec["rows_baseline"] = _safe_int(ws.cell(r, prod_rows_col).value)
            rec["rows_current"]  = _safe_int(ws.cell(r, test_rows_col).value)

        rows_out.append(rec)

    return rows_out


def _parse_batch_sheet(ws, sheet_name: str) -> list[dict]:
    """Parse Batch Run time report sheets.
    Layout: Row with headers = Job_Name | Start | End | Status | RunTime(sec) | (blank) | Start | End | Status | RunTime(sec) | (blank) | Diff | Pass/Fail | Comments"""
    rows_out = []
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    if max_r < 3:
        return []

    # Find header row containing "Job_Name" or "Run Time"
    hdr_row = None
    for r in range(1, min(max_r + 1, 8)):
        vals = [str(ws.cell(r, c).value or "").lower() for c in range(1, min(max_c + 1, 16))]
        joined = " ".join(vals)
        if "job_name" in joined or ("run time" in joined and "sec" in joined):
            hdr_row = r
            break

    if hdr_row is None:
        return []

    # Find run-time columns (there should be 2 — PROD and TEST)
    rt_cols = []
    status_cols = []
    passfail_col = None
    diff_col = None
    for c in range(1, min(max_c + 1, 16)):
        v = str(ws.cell(hdr_row, c).value or "").lower().strip()
        if "run time" in v or "runtime" in v:
            rt_cols.append(c)
        elif "completion" in v or (v == "status" and not passfail_col):
            status_cols.append(c)
        elif "pass" in v and "fail" in v:
            passfail_col = c
        elif "diff" in v:
            diff_col = c

    if len(rt_cols) < 2:
        return []

    # Determine PROD vs TEST: check env row above headers
    env_row = hdr_row - 1 if hdr_row > 1 else None
    prod_rt_col, test_rt_col = rt_cols[0], rt_cols[1]
    if env_row:
        for c in range(1, min(max_c + 1, 16)):
            v = str(ws.cell(env_row, c).value or "").strip().upper()
            if "TEST" in v and c <= rt_cols[0]:
                prod_rt_col, test_rt_col = rt_cols[1], rt_cols[0]
                break

    cat = _clean_category_name(sheet_name, "Batch Runtime")

    for r in range(hdr_row + 1, max_r + 1):
        job = str(ws.cell(r, 1).value or "").strip()
        if not job:
            continue
        # Skip section headers (e.g. "Daily Run", "Weekly Run" sub-sections)
        prod_rt = _safe_float(ws.cell(r, prod_rt_col).value)
        test_rt = _safe_float(ws.cell(r, test_rt_col).value)
        pf = str(ws.cell(r, passfail_col).value or "").strip() if passfail_col else None

        rows_out.append({
            "transaction": job,
            "baseline_sec": prod_rt,
            "current_sec":  test_rt,
            "category": cat,
            "pass_fail": pf,
        })

    return rows_out


def _parse_fill_rate_sheet(ws) -> list[dict]:
    """Parse Fill Rate sheet — multiple date blocks, each with Type | PROD | TEST | Diff | Comments."""
    entries = []
    max_r = ws.max_row or 0
    current_date = None

    for r in range(1, max_r + 1):
        a = str(ws.cell(r, 1).value or "").strip()
        if not a:
            continue

        # Detect date row (DD.MM.YYYY or YYYY-MM-DD)
        if re.match(r"^\d{2}\.\d{2}\.\d{4}$", a) or re.match(r"^\d{4}-\d{2}-\d{2}", a):
            current_date = a
            continue

        # Skip label rows
        al = a.lower()
        if al in ("type", "live data", "live", "") or al.startswith("live"):
            continue

        # Data row
        prod_val = _safe_float(ws.cell(r, 2).value)
        test_val = _safe_float(ws.cell(r, 3).value)
        diff_val = _safe_float(ws.cell(r, 4).value)
        comment  = str(ws.cell(r, 5).value or "").strip()

        if prod_val > 0 or test_val > 0:
            entries.append({
                "date": current_date,
                "type": a,
                "prod": round(prod_val, 6),
                "test": round(test_val, 6),
                "diff": round(diff_val, 6),
                "status": comment if comment else ("Pass" if abs(diff_val) < 1.0 else "Fail"),
            })

    return entries


def _parse_observations_sheet(ws) -> list[dict]:
    """Parse SIT Observations sheet."""
    obs = []
    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    if max_r < 2:
        return []

    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, min(max_c + 1, 10))]

    for r in range(2, max_r + 1):
        vals = [str(ws.cell(r, c).value or "").strip() for c in range(1, min(max_c + 1, 10))]
        if not any(vals):
            continue
        entry = {}
        for i, h in enumerate(headers):
            if i < len(vals):
                entry[h.lower().replace(" ", "_").replace("/", "_")] = vals[i]
        obs.append(entry)

    return obs


# ── Batch performance (RUNTIME_<new> / RUNTIME_<old>) parser ────────────────

# Job-column synonyms — any of these as the first header = job name column
_BP_JOB_COLS = {"job", "job_name", "job name", "task", "task_name", "batch_job",
                "step", "process", "name"}

# Runtime column keyword fragments — column names containing any of these
# AND at least two present = before/after runtime columns
_BP_RT_FRAGMENTS = ("runtime", "run_time", "run time", "elapsed", "duration",
                    "time_new", "time_old", "new_time", "old_time",
                    "time_prod", "time_test", "time_pre", "time_post",
                    "before", "after", "prod_time", "test_time")


def _detect_batch_perf_headers(ws) -> tuple | None:
    """Return (hdr_row, first_rt_col, second_rt_col) if the sheet looks like a
    before/after batch runtime comparison, else None.

    Detection is purely column-name based — no customer-specific strings.
    Scans the first 8 rows to handle files with date/run-label rows above headers.

    Accepted job-column names:  job, job_name, task, name, step, process, …
    Accepted runtime columns:   any pair containing 'runtime', 'elapsed',
                                'duration', 'before', 'after', 'time_new', etc.
    """
    max_r = ws.max_row or 0
    for r in range(1, min(max_r + 1, 9)):
        vals = [str(ws.cell(r, c).value or "").strip() for c in range(1, 15)]
        first = vals[0].lower().replace(" ", "_").replace("-", "_")
        if first not in _BP_JOB_COLS:
            continue
        # Collect columns whose names contain any runtime fragment
        rt_cols = [
            i + 1
            for i, v in enumerate(vals)
            if v and any(frag in v.lower().replace(" ", "_").replace("-", "_")
                         for frag in _BP_RT_FRAGMENTS)
        ]
        if len(rt_cols) >= 2:
            return r, rt_cols[0], rt_cols[1]   # (hdr_row, new_col, old_col)
    return None


def _parse_batch_perf_sheet(ws, sheet_name: str) -> list[dict] | None:
    """Parse a RUNTIME_<new>/RUNTIME_<old> batch comparison sheet.
    Returns a list of row dicts or None if the format is not detected."""
    detected = _detect_batch_perf_headers(ws)
    if detected is None:
        return None
    hdr_row, new_col, old_col = detected
    rows_out: list[dict] = []
    for r in range(hdr_row + 1, (ws.max_row or 0) + 1):
        job = str(ws.cell(r, 1).value or "").strip()
        if not job:
            continue
        raw_new = ws.cell(r, new_col).value
        raw_old = ws.cell(r, old_col).value
        new_secs = _safe_float(raw_new) if raw_new is not None else 0.0
        old_secs = _safe_float(raw_old) if raw_old is not None else 0.0
        if new_secs == 0.0 and old_secs == 0.0:
            continue  # no data for this job
        rows_out.append({
            "transaction": job,
            "baseline_sec": old_secs,   # old runtime = baseline
            "current_sec":  new_secs,   # new runtime = current
            "category": "Batch Performance",
        })
    return rows_out if rows_out else None


def _build_batch_perf_summary(rows: list[dict], threshold_pct: float) -> dict:
    """Compute regression / improvement breakdown and top-10 lists from batch perf rows."""
    regressions: list[dict] = []
    improvements: list[dict] = []
    no_change = 0
    new_only  = 0

    for r in rows:
        old, new = r["baseline_sec"], r["current_sec"]
        job = r["transaction"]
        if old == 0:
            new_only += 1
            continue
        delta_pct  = (new - old) / old * 100
        delta_secs = old - new  # positive = time saved
        entry = {
            "job":       job,
            "old_secs":  round(old,  1),
            "new_secs":  round(new,  1),
            "delta_secs": round(delta_secs, 1),
            "delta_pct":  round(delta_pct,  1),
        }
        if delta_pct > threshold_pct:
            regressions.append(entry)
        elif delta_pct < -5:
            improvements.append(entry)
        else:
            no_change += 1

    regressions.sort(key=lambda x: x["delta_pct"], reverse=True)   # worst first
    improvements.sort(key=lambda x: x["delta_pct"])                 # best first

    comparable = [r for r in rows if r["baseline_sec"] > 0]
    net_delta  = sum(r["baseline_sec"] - r["current_sec"] for r in comparable)

    return {
        "total_jobs":      len(rows),
        "comparable":      len(comparable),
        "regressions":     len(regressions),
        "improvements":    len(improvements),
        "new_only":        new_only,
        "no_change":       no_change,
        "net_delta_secs":  round(net_delta, 1),
        "top_regressions": regressions[:10],
        "top_improvements": improvements[:10],
    }


# ── Master multi-sheet parser ────────────────────────────────────────────────

def _parse_benchmark_file(raw_bytes: bytes, filename: str) -> dict:
    """Parse benchmark file. Returns a dict with keys:
       rows, fill_rate, observations, is_multi_sheet"""
    import openpyxl

    result = {"rows": [], "fill_rate": None, "observations": None,
               "is_multi_sheet": False, "batch_perf_all_rows": None}

    if not _is_excel(raw_bytes, filename):
        # CSV fallback — simple flat format
        result["rows"] = _parse_flat_csv(raw_bytes, filename)
        return result

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
    except Exception as e:
        raise ValueError(f"Excel parse failed: {e}") from e

    sheet_names = wb.sheetnames

    # ── Batch performance format detection (RUNTIME_<new>/RUNTIME_<old>) ──────
    # Check every sheet before falling through to generic parsers
    for sn in sheet_names:
        bp_rows = _parse_batch_perf_sheet(wb[sn], sn)
        if bp_rows is not None:
            result["batch_perf_all_rows"] = bp_rows
            result["rows"] = bp_rows           # will be processed by endpoint
            wb.close()
            return result
    # ─────────────────────────────────────────────────────────────────────────

    if len(sheet_names) <= 1:
        # Single sheet — try flat format first, then smart detect
        ws = wb[sheet_names[0]]
        rows = _try_parse_single_sheet(ws, sheet_names[0])
        result["rows"] = rows
        wb.close()
        return result

    # Multi-sheet — classify each sheet
    result["is_multi_sheet"] = True
    all_rows = []

    for sn in sheet_names:
        ws = wb[sn]
        stype = _classify_sheet(sn)

        if stype == "fill_rate":
            result["fill_rate"] = _parse_fill_rate_sheet(ws)
        elif stype == "batch":
            all_rows.extend(_parse_batch_sheet(ws, sn))
        elif stype == "ui_perf":
            all_rows.extend(_parse_ui_perf_sheet(ws, sn))
        elif stype == "observations":
            result["observations"] = _parse_observations_sheet(ws)
        else:
            # Try generic parse
            rows = _try_parse_single_sheet(ws, sn)
            if rows:
                all_rows.extend(rows)

    result["rows"] = all_rows
    wb.close()
    return result


def _try_parse_single_sheet(ws, sheet_name: str) -> list[dict]:
    """Try to parse a single sheet as UI perf, batch, or flat format."""
    # First try UI perf
    rows = _parse_ui_perf_sheet(ws, sheet_name)
    if rows:
        return rows
    # Then try batch
    rows = _parse_batch_sheet(ws, sheet_name)
    if rows:
        return rows
    # Fallback: flat format
    return _parse_flat_sheet(ws)


def _parse_flat_sheet(ws) -> list[dict]:
    """Parse a simple flat sheet: Transaction | Baseline | Current [| SLA]."""
    import pandas as pd

    max_r = ws.max_row or 0
    max_c = ws.max_column or 0
    if max_r < 2 or max_c < 3:
        return []

    headers = [str(ws.cell(1, c).value or "").strip() for c in range(1, max_c + 1)]
    txn_col  = _find_col(headers, _TXN_COLS)
    base_col = _find_col(headers, _BASE_COLS)
    cur_col  = _find_col(headers, _CUR_COLS)
    sla_col  = _find_col(headers, _SLA_COLS)

    if not txn_col:
        txn_col = headers[0] if headers else None
    if not base_col and len(headers) >= 2:
        base_col = headers[1]
    if not cur_col and len(headers) >= 3:
        cur_col = headers[2]

    if not txn_col or not base_col or not cur_col:
        return []

    h_idx = {h: i + 1 for i, h in enumerate(headers)}
    rows = []
    for r in range(2, max_r + 1):
        txn = str(ws.cell(r, h_idx.get(txn_col, 1)).value or "").strip()
        if not txn:
            continue
        base = _safe_float(ws.cell(r, h_idx.get(base_col, 2)).value)
        cur  = _safe_float(ws.cell(r, h_idx.get(cur_col, 3)).value)
        sla  = _safe_float(ws.cell(r, h_idx.get(sla_col, 0)).value) if sla_col and sla_col in h_idx else None

        rows.append({
            "transaction": txn,
            "baseline_sec": base,
            "current_sec":  cur,
            "sla_sec":      sla if sla else None,
        })
    return rows


def _parse_flat_csv(raw_bytes: bytes, filename: str) -> list[dict]:
    """Parse CSV fallback."""
    import pandas as pd
    try:
        df = pd.read_csv(io.BytesIO(raw_bytes))
    except Exception:
        try:
            df = pd.read_excel(io.BytesIO(raw_bytes), engine="openpyxl")
        except Exception as e:
            raise ValueError(f"Cannot decode file: {e}") from e

    headers = list(df.columns)
    txn_col  = _find_col(headers, _TXN_COLS)
    base_col = _find_col(headers, _BASE_COLS)
    cur_col  = _find_col(headers, _CUR_COLS)
    sla_col  = _find_col(headers, _SLA_COLS)
    if not txn_col or not base_col or not cur_col:
        cols = list(df.columns)
        if len(cols) >= 3:
            txn_col = txn_col or cols[0]
            base_col = base_col or cols[1]
            cur_col = cur_col or cols[2]
        else:
            return []

    rows = []
    for _, row in df.iterrows():
        txn = str(row.get(txn_col, "?"))
        if not txn or txn == "nan":
            continue
        base = _safe_float(row.get(base_col, 0))
        cur  = _safe_float(row.get(cur_col, 0))
        sla  = _safe_float(row.get(sla_col)) if sla_col else None
        rows.append({
            "transaction": txn,
            "baseline_sec": base,
            "current_sec": cur,
            "sla_sec": sla if sla else None,
        })
    return rows


# ── Build category summaries ──────────────────────────────────────────────────

def _build_categories(result_rows: list[BenchmarkRow]) -> list[BenchmarkCategory]:
    cats: dict[str, BenchmarkCategory] = {}
    for r in result_rows:
        c = r.category or "General"
        if c not in cats:
            cats[c] = BenchmarkCategory(name=c)
        cats[c].total += 1
        if r.status == "RED":
            cats[c].degraded += 1
            cats[c].failed += 1
        elif r.pass_fail and r.pass_fail.strip().lower() == "fail":
            cats[c].failed += 1
        else:
            cats[c].passed += 1
    for cat in cats.values():
        vals = [r.delta_pct for r in result_rows if (r.category or "General") == cat.name]
        cat.avg_delta = round(sum(vals) / len(vals), 2) if vals else 0.0
    return list(cats.values())


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/benchmark", response_model=BenchmarkResponse)
async def benchmark_upload(
    file:      UploadFile = File(...),
    threshold: float      = Form(0.0),
) -> BenchmarkResponse:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    thresh = threshold if threshold > 0 else float(
        config_store.get("benchmark_threshold", 10.0))

    try:
        parsed = _parse_benchmark_file(raw, file.filename or "")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot parse benchmark file: {exc}") from exc

    rows         = parsed.get("rows", [])
    fill_rate    = parsed.get("fill_rate")
    observations = parsed.get("observations")
    bp_all_rows  = parsed.get("batch_perf_all_rows")   # present only for RUNTIME_ files

    if not rows and not fill_rate:
        raise HTTPException(status_code=422, detail=(
            "Could not identify any benchmark data. "
            "Expected: UI Perf / Batch / Fill Rate sheets, or "
            "flat Transaction | Baseline (sec) | Current (sec) columns."))

    # Build batch perf summary before _compute_benchmark (which processes all rows)
    batch_perf_summary = None
    if bp_all_rows is not None:
        batch_perf_summary = _build_batch_perf_summary(bp_all_rows, thresh)

    result_rows, summary = _compute_benchmark(rows, thresh)
    categories = _build_categories(result_rows)

    reds      = sum(1 for r in result_rows if r.status == "RED")
    greens    = sum(1 for r in result_rows if r.status in ("GREEN", "N/A"))
    unchanged = sum(1 for r in result_rows if abs(r.delta_pct) <= 1.0)
    breaches  = sum(1 for r in result_rows if r.sla_breach)
    avg_delta = round(sum(r.delta_pct for r in result_rows) / len(result_rows), 2) if result_rows else 0.0

    # For batch perf files override summary to something meaningful
    if batch_perf_summary is not None:
        bp = batch_perf_summary
        net = bp["net_delta_secs"]
        direction = "saved" if net >= 0 else "added"
        summary = (
            f"{'⚠️' if bp['regressions'] > 0 else '✅'} "
            f"{bp['regressions']} regression(s), {bp['improvements']} improvement(s) "
            f"across {bp['total_jobs']} jobs. "
            f"Net: {abs(net):.0f}s {direction} per run."
        )

    resp = BenchmarkResponse(
        filename=file.filename or "",
        total_transactions=len(result_rows),
        degraded=reds, improved=greens, unchanged=unchanged,
        sla_breaches=breaches, avg_delta_pct=avg_delta,
        threshold_pct=thresh, rows=result_rows, summary=summary,
        categories=categories,
        fill_rate=fill_rate,
        observations=observations,
        batch_perf_summary=batch_perf_summary,
    )
    try:
        from services.ai_narrator import narrate
        text, model = narrate("benchmark", {
            "summary":      summary,
            "counts":       {"total": len(result_rows), "degraded": reds,
                             "improved": greens, "unchanged": unchanged,
                             "sla_breaches": breaches},
            "avg_delta_pct": avg_delta,
            "threshold_pct": thresh,
            "top_rows":     [r.model_dump() for r in result_rows[:8]],
        })
        if text:
            resp.ai_narrative = text
            resp.ai_model     = model
    except Exception:
        pass
    return resp


@router.post("/benchmark/json", response_model=BenchmarkResponse)
def benchmark_json(body: JsonBenchmarkRequest) -> BenchmarkResponse:
    thresh = float(body.threshold or config_store.get("benchmark_threshold", 10.0))
    result_rows, summary = _compute_benchmark(body.rows or [], thresh)
    categories = _build_categories(result_rows)

    reds      = sum(1 for r in result_rows if r.status == "RED")
    greens    = sum(1 for r in result_rows if r.status in ("GREEN", "N/A"))
    unchanged = sum(1 for r in result_rows if abs(r.delta_pct) <= 1.0)
    breaches  = sum(1 for r in result_rows if r.sla_breach)
    avg_delta = round(sum(r.delta_pct for r in result_rows) / len(result_rows), 2) if result_rows else 0.0

    resp = BenchmarkResponse(
        filename=body.filename or "",
        total_transactions=len(result_rows),
        degraded=reds, improved=greens, unchanged=unchanged,
        sla_breaches=breaches, avg_delta_pct=avg_delta,
        threshold_pct=thresh, rows=result_rows, summary=summary,
        categories=categories,
    )
    try:
        from services.ai_narrator import narrate
        text, model = narrate("benchmark", {
            "summary":      summary,
            "counts":       {"total": len(result_rows), "degraded": reds,
                             "improved": greens, "unchanged": unchanged,
                             "sla_breaches": breaches},
            "avg_delta_pct": avg_delta,
            "threshold_pct": thresh,
            "top_rows":     [r.model_dump() for r in result_rows[:8]],
        })
        if text:
            resp.ai_narrative = text
            resp.ai_model     = model
    except Exception:
        pass
    return resp
