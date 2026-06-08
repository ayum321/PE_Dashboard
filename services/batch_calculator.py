"""
Ctrl-M batch analytics — extracted from app_v2.py and decoupled from
Streamlit. Pure pandas/numpy, no globals, no `st.cache_data`.

Public API:
    load_ctrlm_bytes(raw: bytes, filename: str) -> pandas.DataFrame
    compute_metrics(df: pandas.DataFrame)        -> dict
    build_top_jobs_df(df: pandas.DataFrame)      -> pandas.DataFrame
    build_batch_payload(df: pandas.DataFrame)    -> dict   (JSON-ready)

The original thresholds, groupings and classification rules are
preserved verbatim from `app_v2.py`:
    - DAILY_LIMIT_HRS   = 6.0
    - MONTHLY_LIMIT_HRS = 8.0
    - Buffer bands: BREACH < 0, CRITICAL < 10, CAUTION < 30, HEALTHY < 50, EXCELLENT
    - At-risk band: buffer_pct in [0, 15]
    - F6 z-score anomaly threshold: |z| > 2.0
"""
from __future__ import annotations

import difflib
import io
import logging
import os
import re
from typing import Any, Dict

import numpy as np
import pandas as pd

from services import pe_config

logger = logging.getLogger("pe_dashboard.batch_calculator")


# ── Constants — read from pe_config (single source of truth) ────
DAILY_LIMIT_HRS: float = pe_config.SLA_DAILY_HRS
MONTHLY_LIMIT_HRS: float = pe_config.SLA_MONTHLY_HRS


# ─────────────────────────────────────────────────────────────────
# RULE 3 — normalise_ctrlm_csv (call immediately after pd.read_csv)
# ─────────────────────────────────────────────────────────────────
def normalise_ctrlm_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rule 3 — Call immediately after pd.read_csv() / pd.read_excel().

    Normalises the Status column so downstream code sees only "OK" or "FAILED".
    Handles all Ctrl-M completion status variants:
        ENDED OK, Ended Ok, ended ok, ENDED_OK, COMPLETED, SUCCESS, DONE → OK
        ABENDED, FAILED, TERMINATED, <blank>           → FAILED

    Never mutates the original DataFrame — always returns a copy.
    """
    if df is None or df.empty or "Status" not in df.columns:
        return df

    try:
        from services.pe_utils import SUCCESS_STATUSES, _normalise_status_str

        def _norm(raw: str) -> str:
            cleaned = _normalise_status_str(str(raw))
            return "OK" if cleaned in SUCCESS_STATUSES else "FAILED"

        df = df.copy()
        df["Status"] = df["Status"].fillna("").astype(str).apply(_norm)
    except Exception:
        # Non-fatal: keep Status as-is if pe_utils unavailable
        pass
    return df


# ─────────────────────────────────────────────────────────────────
# Ctrl-M file loader (extracted verbatim from app_v2.py:1644)
# ─────────────────────────────────────────────────────────────────
def _parse_duration_str(s: str) -> float:
    """Parse human-readable duration to seconds.

    Handles: '2hr 21min', '1h 30m', '01:30:00', '5400', '90' (assumes secs).
    """
    sv = str(s).strip()
    # "2hr 21min" / "2h 21m" / "2 hours 21 minutes"
    m = re.match(r"(\d+)\s*h[r]?[s]?\s*(\d+)\s*m", sv, re.I)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.match(r"(\d+)\s*h[r]?[s]?$", sv, re.I)
    if m:
        return int(m.group(1)) * 3600
    m = re.match(r"(\d+)\s*m[in]?[s]?$", sv, re.I)
    if m:
        return int(m.group(1)) * 60
    # "01:30:00" HH:MM:SS
    m = re.match(r"(\d+):(\d+):(\d+)$", sv)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    # "01:30" — treat as HH:MM
    m = re.match(r"(\d+):(\d+)$", sv)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    try:
        return float(sv)
    except Exception:
        return 0.0


def _parse_dt(s: "pd.Series | str") -> "pd.Series | pd.Timestamp":
    """Parse datetime from a Series or scalar covering all customer export formats.

    Formats seen across 35+ customer files:
      2025-11-28 06:50:00   — ISO (most common)
      26-06-2025 23:45      — DD-MM-YYYY (BJS, EU)
      3/2/2025 13:21        — M/D/YYYY US
      06-NOV-24 11.00.05.100271000 PM GMT — Oracle DB timestamp (BATCH_RUN_TIME)
    """
    _ORACLE_PAT = re.compile(
        r"(\d{2}-[A-Z]{3}-\d{2})\s+(\d{2})\.(\d{2})\.(\d{2})\.\d+\s+(AM|PM)\s+\w+",
        re.IGNORECASE)
    _FMTS = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d-%m-%Y %H:%M",    "%d-%m-%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
        "%d/%m/%Y %H:%M",    "%d/%m/%Y %H:%M:%S",
        "%d-%b-%y %I:%M:%S %p",   # Oracle after dot→colon normalisation
    ]

    if isinstance(s, pd.Series):
        # Vectorised Oracle normalisation: "06-NOV-24 11.00.05.100271000 PM GMT" → "06-NOV-24 11:00:05 PM"
        s = s.astype(str).str.replace(
            r"(\d{2}-[A-Z]{3}-\d{2})\s+(\d{2})\.(\d{2})\.(\d{2})\.\d+\s+(AM|PM)\s+\w+",
            lambda m: f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)} {m.group(5)}",
            regex=True, flags=re.IGNORECASE,
        )
        # Multi-pass: pick primary format (>40%), then retry remaining NaTs with
        # subsequent formats instead of discarding them.  Fixes mixed-format files
        # (e.g. 55% ISO + 45% EU) that previously lost 45% of rows to NaT.
        result = pd.Series([pd.NaT] * len(s), dtype="datetime64[ns]", index=s.index)
        remaining_mask = pd.Series([True] * len(s), index=s.index)
        for fmt in _FMTS:
            if not remaining_mask.any():
                break
            try:
                partial = pd.to_datetime(s[remaining_mask], format=fmt, errors="coerce")
                filled  = partial.notna()
                if filled.sum() > 0:
                    result[filled[filled].index] = partial[filled]
                    remaining_mask[filled[filled].index] = False
            except Exception:
                pass
        # Last-resort for any still-NaT rows
        if remaining_mask.any():
            last = pd.to_datetime(s[remaining_mask], errors="coerce")
            result[last.notna()[last.notna()].index] = last[last.notna()]
        return result
    else:
        sv = str(s).strip()
        m = _ORACLE_PAT.match(sv)
        if m:
            sv = f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)} {m.group(5)}"
        for fmt in _FMTS:
            try:
                return pd.to_datetime(sv, format=fmt)
            except Exception:
                pass
        return pd.to_datetime(sv, errors="coerce")


def _find_data_row(df_raw: pd.DataFrame) -> int:
    """Detect the actual header row in files with title/merged-cell preamble.

    Scans the first 15 rows for the row with the most non-null, non-'Unnamed'
    cells — that is the real column-header row.  Returns 0 if the file already
    starts clean.
    """
    best_row, best_score = 0, -1
    for i, row in df_raw.head(15).iterrows():
        vals = [str(v).strip() for v in row if str(v).strip() and not str(v).startswith("Unnamed")]
        # Prefer rows that contain Ctrl-M header keywords
        score = len(vals) + sum(2 for v in vals
                                if any(k in v.lower() for k in
                                       ("job", "start", "end", "status", "run", "time", "sec", "process")))
        if score > best_score:
            best_score, best_row = score, i
    return int(best_row)


def _score_sheet_ctrlm(df: pd.DataFrame) -> int:
    """Score a DataFrame sheet for Ctrl-M job-execution content.

    Higher score = more likely to contain per-job Ctrl-M execution rows.
    Used by load_ctrlm_bytes() to pick the right sheet(s) from multi-sheet XLSX.
    """
    score = 0
    cls = {
        str(c).lower().strip().replace(" ", "_").replace(".", "_")
        for c in df.columns
    }
    # Strong Ctrl-M structural signals
    if any("job" in c and ("name" in c or c == "job") for c in cls): score += 4
    if any("sub" in c and "app" in c for c in cls):                   score += 3
    if any("status" in c and "message" not in c for c in cls):        score += 3
    if any("run" in c and "sec" in c for c in cls):                   score += 2
    # Temporal signals
    if any("start" in c and ("time" in c or "date" in c or c == "start") for c in cls): score += 2
    if any("end"   in c and ("time" in c or "date" in c or c == "end")   for c in cls): score += 1
    # Row-count bonus: job-level data has many rows
    n = len(df)
    if n >= 100:  score += 3
    elif n >= 20: score += 2
    elif n >= 5:  score += 1
    # Penalise aggregate summary sheets
    _summary_sigs = {"duration", "concurrent_batch", "gmp_live", "gmp_drp"}
    if _summary_sigs & cls: score -= 2
    if n < 20:              score -= 1
    return score


def load_ctrlm_bytes(raw: bytes, filename: str = "") -> pd.DataFrame:
    """Load Ctrl-M batch execution data from CSV or XLSX bytes.

    Handles all column-naming conventions discovered across 35+ customer files:
      • Standard Ctrl-M export:  Job_Name, Start_Time, End_Time, Completion Status, Run Time (Sec.)
      • BATCH_RUN_TIME style:    PROCESS, START_DATE, END_DATE, STATUS/STATUS_MESSAGE
      • Daily summary style:     date, job_count, total_run_time_sec  (synthesizes Job_Name)
      • Multi-sheet XLSX:        auto-selects Ctrl-M data sheets (skips summary sheets)
      • Merged-header XLSX:      detects real header row automatically
      • Legacy .xls (OLE/CFB):   xlrd engine
      • Corrupted/binary CSV:    raises ValueError with clear message
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    buf = io.BytesIO(raw)

    # ── Detect if raw bytes are binary/corrupted (not UTF-8 or Latin-1 text) ──
    if ext not in (".xlsx", ".xls") and len(raw) >= 4 and raw[:4] in (
            b'\xd0\xcf\x11\xe0', b'PK\x03\x04'):
        # OLE/ZIP magic bytes in a file named .csv → it's actually an Excel file
        ext = ".xlsx" if raw[:4] == b'PK\x03\x04' else ".xls"

    if ext in (".xlsx", ".xls"):
        engine = "xlrd" if (ext == ".xls" or raw[:4] == b'\xd0\xcf\x11\xe0') else "openpyxl"
        try:
            # Read ALL sheets at once, then pick the ones containing Ctrl-M job data.
            # This handles files like "Batch Report Demand + ESP.xlsx" where the first
            # sheet is a human-readable summary and the actual Ctrl-M rows are on
            # subsequent sheets (ESP, Demand, etc.).
            buf.seek(0)
            _all: dict = pd.read_excel(buf, sheet_name=None, engine=engine, dtype=str)  # type: ignore
            _scores = {sh: _score_sheet_ctrlm(sdf) for sh, sdf in _all.items()}
            _best_sc = max(_scores.values(), default=0)
            # A sheet qualifies when score ≥ 5 AND ≥ 60 % of the top-scoring sheet.
            _CTRLM_MIN = 5
            _good = [
                sh for sh, sc in _scores.items()
                if sc >= _CTRLM_MIN and sc >= _best_sc * 0.6
            ]
            if not _good:
                _good = [list(_all.keys())[0]]   # graceful fall-back: first sheet
            logger.info(
                "load_ctrlm_bytes: selected sheet(s) %s from '%s' (scores: %s)",
                _good, filename, _scores,
            )
            # Build per-sheet DataFrames; fix buried headers where needed
            _parts: list[pd.DataFrame] = []
            for _sh in _good:
                _sdf = _all[_sh].copy()
                # Detect merged-title preamble: many Unnamed columns in row-0 header
                _unnamed = sum(1 for c in _sdf.columns if str(c).startswith("Unnamed"))
                if _unnamed > len(_sdf.columns) * 0.4 and len(_sdf) > 1:
                    _hdr_idx = _find_data_row(_sdf.head(20))
                    _new_cols = _sdf.iloc[_hdr_idx]
                    _sdf = _sdf.iloc[_hdr_idx + 1:].copy()
                    _sdf.columns = pd.Index([str(v).strip() for v in _new_cols])
                    _sdf = _sdf.reset_index(drop=True)
                if len(_good) > 1:
                    _sdf["_source_sheet"] = _sh   # tag origin for multi-sheet merges
                _parts.append(_sdf)
            df = pd.concat(_parts, ignore_index=True) if len(_parts) > 1 else _parts[0]
        except Exception as e:
            raise ValueError(f"Cannot parse Ctrl-M file: {e}") from e
    else:
        # Try UTF-8 first, fall back to Latin-1, then try as Excel
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                buf.seek(0)
                df = pd.read_csv(buf, encoding=enc, on_bad_lines="skip", dtype=str)
                # Reject if result looks corrupt (e.g. CCH_MPWB — binary XLS named .csv)
                if len(df.columns) == 1 and df.columns[0].startswith("\xd0"):
                    raise ValueError("Binary content detected in CSV")
                break
            except UnicodeDecodeError:
                continue
            except Exception:
                break
        else:
            buf.seek(0)
            try:
                df = pd.read_excel(buf, sheet_name=0, engine="openpyxl", dtype=str)
            except Exception as e:
                raise ValueError(f"Cannot parse Ctrl-M file: {e}") from e

    # ── Numeric coercion (dtype=str used above for safe read; convert now) ──
    for col in df.columns:
        try:
            numeric = pd.to_numeric(df[col], errors="coerce")
            # Only coerce if > 50% of non-null values are numeric
            if numeric.notna().sum() > df[col].notna().sum() * 0.5:
                df[col] = numeric
        except Exception:
            pass

    df.columns = pd.Index([str(c).strip() for c in df.columns])
    col_map: Dict[str, str] = {}
    for c in df.columns:
        cl = c.lower().replace(" ", "_").replace("-", "_").replace(".", "_")
        # Skip unnamed / index columns
        if cl.startswith("unnamed"):
            continue
        if "folder" in cl:
            col_map[c] = "Folder"
        elif ("sub" in cl and "app" in cl) or cl in (
                "sub_application", "sub_app", "stream", "component",
                "batch", "workflow", "batch_type", "schedule", "schedule_type", "pipeline"):
            col_map[c] = "Sub_Application"
        elif "application" in cl and "sub" not in cl and "sub_application" not in col_map.values():
            col_map[c] = "Application"
        elif "job" in cl and "name" in cl:
            col_map[c] = "Job_Name"
        elif cl in ("job", "jobname", "job_id", "jobid", "taskname", "task_name",
                    "task", "process_name", "processname", "ordername", "order_name",
                    "step_name", "stepname", "script_name", "scriptname", "name",
                    "process",  # BATCH_RUN_TIME pattern: PROCESS column
                    ):
            col_map[c] = "Job_Name"
        elif "start" in cl and ("time" in cl or "date" in cl or cl in ("start", "start_date")):
            col_map[c] = "Start_Time"
        elif "end" in cl and ("time" in cl or "date" in cl or cl in ("end", "end_date")):
            col_map[c] = "End_Time"
        elif "status" in cl and "message" not in cl:   # STATUS_MESSAGE is not a status flag
            col_map[c] = "Status"
        elif "completion" in cl and "status" in cl:
            col_map[c] = "Status"
        # Human-readable runtime strings: "2hr 21min", "1:30:00" — check BEFORE numeric runtime
        # Only map columns that clearly represent total batch runtime in H:M format
        elif cl in ("total_batch_runtime", "batch_runtime",
                    "runtime_hhmm", "total_time_hhmm",
                    "total_batch_time", "batch_elapsed", "batch_duration"):
            col_map[c] = "_duration_str"
        # Runtime columns in seconds — ordered most-specific first to avoid clobbering
        elif cl in ("run_time_(sec_)", "run_time_(sec.)", "run_sec", "runtime_sec",
                    "total_runtime_sec", "total_run_time_sec", "total_runime_sec",
                    "total_runtimesec", "total_run_time_seconds",
                    "total_run_time_limited"):
            col_map[c] = "Run_Sec"
        elif "run" in cl and "sec" in cl and "avg" not in cl:
            col_map[c] = "Run_Sec"
        elif "total" in cl and "runtime" in cl:
            col_map[c] = "Run_Sec"
        elif "duration" in cl or "elapsed" in cl:
            col_map[c] = "Run_Sec"
        # Comparison XLSX format: RUNTIME_<new> / RUNTIME_<old> — pick the
        # "new" column as the current Run_Sec.  Skip "old" so the pipeline uses
        # the latest measurement.  Matches: runtime_new, runtime_<new>, etc.
        elif cl.startswith("runtime") and ("new" in cl or "current" in cl or "latest" in cl):
            col_map[c] = "Run_Sec"
        # Bare "runtime" column (no suffix) — also valid as Run_Sec
        elif cl == "runtime":
            col_map[c] = "Run_Sec"
        # Daily-summary files: synthesise Job_Name from date, derive Run_Sec from totals
        elif cl in ("date", "run_date", "week", "week_start", "week_ending"):
            col_map[c] = "Start_Time"   # date/week column doubles as Start_Time
        elif cl == "job_count":
            col_map[c] = "_job_count"   # preserve for synthetic row expansion

    # Comparison XLSX fallback: if RUNTIME_<old> exists but Run_Sec was never
    # mapped (no "new" column), use the first runtime_* column as Run_Sec.
    if "Run_Sec" not in col_map.values():
        for c in df.columns:
            cl2 = c.lower().replace(" ", "_").replace("-", "_").replace(".", "_")
            if cl2.startswith("runtime") and c not in col_map:
                col_map[c] = "Run_Sec"
                break

    df.rename(columns=col_map, inplace=True)

    # ── Daily-summary files: synthesise per-row job representation ────────
    # Files like Daily_Activity_Summary.csv have one row per day with a total
    # runtime but no Job_Name column.  We synthesise a "DAILY_BATCH" job so
    # the rest of the pipeline sees consistent data.
    if "Job_Name" not in df.columns and "Run_Sec" in df.columns:
        if "_job_count" in df.columns:
            # day-level summary — represent as a single synthetic job per day
            df["Job_Name"] = df["Start_Time"].astype(str).apply(
                lambda d: f"DAILY_BATCH_{str(d)[:10]}")
            df["Sub_Application"] = "DAILY_SUMMARY"
        elif "Start_Time" in df.columns:
            df["Job_Name"] = "BATCH"
            df["Sub_Application"] = "UNKNOWN"

    # RULE 3 — normalise Status after column mapping (Status may have been
    # renamed from "Completion Status", "State", "completion", etc.)
    df = normalise_ctrlm_csv(df)

    # ── CRITICAL: drop duplicate column names (keep first) ────────────────
    if df.columns.duplicated().any():
        dupes = df.columns[df.columns.duplicated(keep=False)].unique().tolist()
        logger.warning("Duplicate columns detected and deduplicated (kept first): %s", dupes)
        df = df.loc[:, ~df.columns.duplicated(keep="first")]

    # ── CRITICAL: sanitize all string columns before ANY .str accessor ─────
    # Fixes: 'float' object has no attribute 'lower'  (NaN cells in mixed cols)
    for _scol in ("Status", "Job_Name", "Folder", "Sub_Application", "Application"):
        if _scol in df.columns:
            col = df[_scol]
            # Extra guard: if duplicate columns still sneak through, take first
            if hasattr(col, "columns"):  # it's a DataFrame, not a Series
                col = col.iloc[:, 0]
                df[_scol] = col
            df[_scol] = df[_scol].fillna("").astype(str).str.strip()
    # Drop fully-empty rows that produce garbage after coercion
    _jcol = df["Job_Name"] if "Job_Name" in df.columns else df.iloc[:, 0]
    if hasattr(_jcol, "columns"):
        _jcol = _jcol.iloc[:, 0]
    df = df[_jcol.astype(str).str.len() > 0]

    # ── Fuzzy fallback ───────────────────────────────────────────
    _FUZZY_TARGETS = {
        "Job_Name":        ["jobname", "job_name", "jobid", "taskname", "task", "process", "name"],
        "Start_Time":      ["starttime", "start_time", "startdate", "start_date", "begin", "started"],
        "End_Time":        ["endtime", "end_time", "enddate", "end_date", "finish", "finished", "completed"],
        "Run_Sec":         ["runsec", "run_sec", "duration", "elapsed", "runtime", "exectime", "seconds"],
        "Sub_Application": ["subapp", "sub_app", "subapplication", "stream", "module", "component"],
    }
    unmapped_cols = [c for c in df.columns if c not in
                     ("Folder", "Sub_Application", "Application", "Job_Name",
                      "Start_Time", "End_Time", "Status", "Run_Sec",
                      "_duration_str", "_job_count")]
    for target, candidates in _FUZZY_TARGETS.items():
        if target in df.columns:
            continue
        for uc in list(unmapped_cols):
            normalised = uc.lower().replace(" ", "").replace("_", "").replace("-", "")
            matches = difflib.get_close_matches(normalised, candidates, n=1, cutoff=0.75)
            if matches:
                df.rename(columns={uc: target}, inplace=True)
                unmapped_cols.remove(uc)
                break

    # ── Reject clearly non-Ctrl-M files ─────────────────────────────────
    # These are summary/resource/metadata files that have no job run records.
    _col_set = {str(c).lower().strip() for c in df.columns}
    _NON_CTRLM = [
        {"metric", "value"},                               # pe_summary.csv style
        {"server", "category"},                            # resource/SLA-breach report
        {"index_name", "table_name"},                      # DBA metadata
    ]
    for _sig in _NON_CTRLM:
        if _sig.issubset(_col_set) and len(_col_set) <= len(_sig) + 4:
            raise ValueError(
                f"File does not contain Ctrl-M job execution records "
                f"(columns: {list(df.columns)[:6]}). Upload a file with one row per job run."
            )

    # ── Final fallbacks ──────────────────────────────────────────
    if "Job_Name" not in df.columns:
        # For date-level summary files (one row per batch date, no per-job breakdown):
        # synthesise Job_Name from Sub_Application so run-time columns are not mistakenly
        # used as the job identifier.  Pattern: Sub_Application already mapped + Start_Time
        # present + no explicit job column exists → this IS a summary-per-date file.
        _cl_set = {c.lower().replace(" ", "_") for c in df.columns}
        _has_time_col = any(
            ("run" in c and "time" in c) or "gmp" in c or c in ("comments",)
            for c in _cl_set
        )
        if ("Sub_Application" in df.columns and "Start_Time" in df.columns and _has_time_col):
            # Use Sub_Application value as the job name (e.g. "Daily", "Weekly")
            # Each row becomes one run of that workflow type — correct for summary files.
            df["Job_Name"] = df["Sub_Application"].fillna("BATCH")
        else:
            # dtype == object OR StringDtype (pandas 3.x uses 'string[python]' etc.)
            # Exclude duration/time-string columns — they should not become job identifiers.
            _skip_job_kw = ("run_time", "runtime", "elapsed", "duration", "gmp", "comments")
            str_cols = [c for c in df.columns
                        if (pd.api.types.is_string_dtype(df[c]) or
                            str(df[c].dtype).startswith(("object", "string"))) and c not in
                        ("Folder", "Sub_Application", "Application", "Status",
                         "Start_Time", "End_Time", "Run_Sec", "_job_count", "_duration_str",
                         "_source_sheet")
                        and not any(kw in c.lower().replace(" ", "_") for kw in _skip_job_kw)]
            if str_cols:
                df.rename(columns={str_cols[0]: "Job_Name"}, inplace=True)
            else:
                df["Job_Name"] = "UNKNOWN"

    if "Sub_Application" not in df.columns:
        if "Application" in df.columns:
            # Promote the top-level Application column to Sub_Application when no
            # dedicated Sub_Application column was mapped — handles Ctrl-M exports
            # that use "Application" as the workflow grouping identifier.
            df["Sub_Application"] = df["Application"]
        else:
            df["Sub_Application"] = "UNKNOWN"
    if "Status" not in df.columns:
        logger.debug("load_ctrlm_bytes: Status column absent — defaulting all rows to 'ENDED OK'")
        df["Status"] = "ENDED OK"

    if "Run_Sec" not in df.columns:
        # Prefer human-readable duration column if present
        if "_duration_str" in df.columns:
            parsed_secs = df["_duration_str"].apply(
                lambda x: _parse_duration_str(str(x)) if pd.notna(x) else 0.0)
            if parsed_secs.max() > 0:
                df["Run_Sec"] = parsed_secs
        if "Run_Sec" not in df.columns:
            nums = df.select_dtypes(include="number").columns.tolist()
            if nums:
                df.rename(columns={nums[-1]: "Run_Sec"}, inplace=True)
            else:
                df["Run_Sec"] = 0

    df["Run_Sec"] = pd.to_numeric(df["Run_Sec"], errors="coerce").fillna(0)

    # If Run_Sec is still all zeros and there's a human-readable duration column, parse it
    if "_duration_str" in df.columns and df["Run_Sec"].eq(0).all():
        parsed_secs = df["_duration_str"].apply(
            lambda x: _parse_duration_str(str(x)) if pd.notna(x) else 0.0)
        if parsed_secs.max() > 0:
            df["Run_Sec"] = parsed_secs

    # Also scan remaining unmapped columns for duration string patterns when Run_Sec still 0
    if df["Run_Sec"].eq(0).all():
        _DUR_KW = ("runtime", "run_time", "total_time", "batch_time")
        _skip = {"Job_Name", "Sub_Application", "Application", "Folder",
                 "Status", "Start_Time", "End_Time", "Run_Sec", "_duration_str"}
        for _col in df.columns:
            if _col in _skip:
                continue
            _cl = str(_col).lower().replace(" ", "_")
            if any(k in _cl for k in _DUR_KW):
                _parsed = df[_col].apply(
                    lambda x: _parse_duration_str(str(x)) if pd.notna(x) else 0.0)
                if _parsed.max() > 0:
                    df["Run_Sec"] = _parsed
                    break

    if "Start_Time" not in df.columns:
        _dt_exclude = {"Job_Name", "Folder", "Sub_Application", "Application",
                       "Status", "Run_Sec", "_job_count"}
        for c in df.columns:
            if c in _dt_exclude:
                continue
            try:
                parsed = pd.to_datetime(df[c], errors="coerce")
                # Require > 50% of rows to parse AND at least one non-epoch date
                if parsed.notna().sum() > len(df) * 0.5 and (
                        parsed.dropna() > pd.Timestamp("2000-01-01")).any():
                    df.rename(columns={c: "Start_Time"}, inplace=True)
                    break
            except Exception:
                pass
        if "Start_Time" not in df.columns:
            import logging as _log
            _log.getLogger(__name__).warning(
                "No Start_Time column detected in Ctrl-M data — "
                "all runs default to current timestamp. "
                "Window compliance and trend charts will be unreliable."
            )
            df["Start_Time"] = pd.Timestamp.now()

    if "End_Time" in df.columns:
        df["Start_Time"] = _parse_dt(df["Start_Time"])
        df["End_Time"]   = _parse_dt(df["End_Time"])
        # Derive runtime from End−Start for ANY row where Run_Sec is zero.
        # Previous mask (Status == "OK") was too narrow — it left ENDED_OK, LONG, etc.
        # with zero runtime, causing wrong buffer % calculations in the SLA matrix.
        mask = df["Run_Sec"] == 0
        # Preserve original zero-runtime flag BEFORE backfill — used by anomaly
        # detection to distinguish timeout/wait jobs from genuinely slow execution.
        df["_orig_zero_runtime"] = False
        df.loc[mask, "_orig_zero_runtime"] = True
        diff_raw = (df.loc[mask, "End_Time"] - df.loc[mask, "Start_Time"]).dt.total_seconds()
        # Stage 1C: midnight crossover — job ran past midnight → End_Time < Start_Time
        # Fix: add 86400s when diff is negative (single-job crossover, not multi-day)
        diff_corrected = diff_raw.where(diff_raw >= 0, diff_raw + 86400.0)
        # Cap at 168h (1 week) — prevents corrupt timestamp pairs from creating absurd runtimes
        df.loc[mask, "Run_Sec"] = diff_corrected.clip(lower=0, upper=168 * 3600)
    else:
        df["Start_Time"] = _parse_dt(df["Start_Time"])

    # Stage 1B: NaT > 5% warning — surface before rows are silently dropped
    _nat_total = len(df)
    _nat_count = int(df["Start_Time"].isna().sum()) if "Start_Time" in df.columns else 0
    if _nat_total > 0 and _nat_count / _nat_total > 0.05:
        logger.warning(
            "load_ctrlm_bytes '%s': %.0f%% of rows (%d/%d) have unparseable Start_Time "
            "— these rows are dropped and metrics will be incomplete. "
            "Check date format in source file.",
            filename, _nat_count / _nat_total * 100, _nat_count, _nat_total,
        )
    df.dropna(subset=["Start_Time"], inplace=True)
    df["run_time_hrs"] = df["Run_Sec"] / 3600.0
    df["run_date"]     = df["Start_Time"].dt.date
    df["month"]        = df["Start_Time"].dt.to_period("M").astype(str)
    df["Hour_Bucket"]  = df["Start_Time"].dt.hour   # 0-23 for heatmap
    return df


# ─────────────────────────────────────────────────────────────────
# F3 — SLA Buffer (extracted from app_v2.py:2035)
# ─────────────────────────────────────────────────────────────────
def calculate_sla_buffer(sla_window_hrs: float, max_runtime_hrs: float) -> Dict[str, Any]:
    """F3 — SLA Buffer: remaining headroom between peak runtime and SLA window."""
    if sla_window_hrs <= 0 or max_runtime_hrs <= 0:
        return {"buffer_hrs": 0.0, "buffer_pct": 0.0,
                "growth_multiplier": 0.0, "growth_capacity_pct": 0.0, "status": "INVALID"}
    buffer_hrs        = sla_window_hrs - max_runtime_hrs
    buffer_pct        = (buffer_hrs / sla_window_hrs) * 100
    growth_multiplier = sla_window_hrs / max_runtime_hrs
    growth_capacity   = (buffer_hrs / max_runtime_hrs) * 100 if max_runtime_hrs > 0 else 0.0
    # Use pe_config thresholds — same as sla_matrix _compute_sla_matrix to keep
    # all panels consistent.  BREACH/AT_RISK/LONG_JOB/OK replaces the old
    # CRITICAL/CAUTION/HEALTHY/EXCELLENT so PE users see the same label everywhere.
    _at = float(getattr(pe_config, 'SLA_ATRISK_PCT',  15.0))
    _lj = float(getattr(pe_config, 'SLA_LONGJOB_PCT', 40.0))
    if   buffer_pct <= 0:  status = "BREACH"
    elif buffer_pct <= _at: status = "AT_RISK"
    elif buffer_pct <= _lj: status = "LONG_JOB"
    else:                   status = "OK"
    return {"buffer_hrs": round(buffer_hrs, 2),
            "buffer_pct": round(buffer_pct, 1),
            "growth_multiplier": round(growth_multiplier, 2),
            "growth_capacity_pct": round(growth_capacity, 1),
            "status": status}


# ─────────────────────────────────────────────────────────────────
# F6 — Anomaly detection (extracted from app_v2.py:2104)
# ─────────────────────────────────────────────────────────────────
def detect_job_anomalies(df: pd.DataFrame) -> list:
    """F6 — z-score anomaly detection on job peak runtimes.

    Enriches each anomaly with failure context (fail_count, total_runs,
    has_zero_sec_failures) so the findings engine can distinguish timeout/wait
    anomalies from genuine slow execution.
    """
    from services import pe_config
    z_threshold = float(getattr(pe_config, 'ANOMALY_Z_THRESHOLD', 2.0))
    anomalies: list = []
    if df is None or df.empty:
        return anomalies

    _jc = next((c for c in df.columns if "job_name" in c.lower() or c.lower() == "job"), None)
    _hc = next((c for c in df.columns if "run_time_hrs" in c.lower() or "peak_hrs" in c.lower()), None)
    if _jc is None or _hc is None:
        return anomalies

    top = (df.groupby(_jc)[_hc]
             .agg(peak_hrs="max", avg_hrs="mean").reset_index()
             .rename(columns={_jc: "Job_Name"}))

    # Pre-compute per-job failure context for enrichment
    _has_status = "Status" in df.columns
    _has_run_sec = "Run_Sec" in df.columns
    _fail_info: dict = {}
    if _has_status:
        _has_orig_zero = "_orig_zero_runtime" in df.columns
        for jn, grp in df.groupby(_jc):
            _total = len(grp)
            _fails = int((grp["Status"] != "OK").sum())
            # Zero-sec failures: status != OK AND original Run_Sec was 0
            # (before End-Start backfill). These are timeout/wait jobs — the
            # peak_hrs comes from wall-clock wait time, not actual execution.
            _zero_sec = 0
            if _has_orig_zero:
                _zero_sec = int(((grp["Status"] != "OK") & (grp["_orig_zero_runtime"])).sum())
            elif _has_run_sec:
                _zero_sec = int(((grp["Status"] != "OK") & (grp["Run_Sec"] <= 1)).sum())
            _fail_info[jn] = {
                "total_runs": _total,
                "fail_count": _fails,
                "has_zero_sec_failures": _zero_sec > 0,
                "zero_sec_fail_count": _zero_sec,
            }

    if len(top) >= 3:
        mu  = top["peak_hrs"].mean()
        std = top["peak_hrs"].std()
        if std > 0.001:
            top["z_score"] = ((top["peak_hrs"] - mu) / std).round(2)
            stat = top[top["z_score"] > z_threshold]
            for _, row in stat.iterrows():
                jn = row["Job_Name"]
                fi = _fail_info.get(jn, {})
                anomalies.append({
                    "job_name":             jn,
                    "peak_hrs":             round(float(row["peak_hrs"]), 3),
                    "avg_hrs":              round(float(row["avg_hrs"]), 3),
                    "z_score":              float(row["z_score"]),
                    "variance_pct":         None,
                    "status":               "STATISTICAL_OUTLIER",
                    "severity":             3,
                    "total_runs":           fi.get("total_runs", 0),
                    "fail_count":           fi.get("fail_count", 0),
                    "has_zero_sec_failures": fi.get("has_zero_sec_failures", False),
                })
    return sorted(anomalies, key=lambda x: x["severity"], reverse=True)


# ─────────────────────────────────────────────────────────────────
# build_top_jobs_df — name per Phase 3 brief
# ─────────────────────────────────────────────────────────────────
def build_top_jobs_df(df: pd.DataFrame,
                      sla_index: Dict[str, Any] | None = None) -> pd.DataFrame:
    """Group by composite key [Sub_Application, Job_Name] → peak/avg/total hours + SLA buffer.

    RULE 6: Always use ['Sub_Application', 'Job_Name'] composite key to prevent
    collision between DAILY and WEEKLY jobs sharing the same base name.

    SLA resolution priority (per job, not one global ceiling):
      1. Per-job lookup from uploaded SLA matrix (via build_sla_index / resolve_sla)
      2. Schedule-type detected from Sub_Application name
      3. System pe_config default (fallback only)
    """
    # RULE 6 — composite key: Sub_Application + Job_Name
    group_cols = (["Sub_Application", "Job_Name"]
                  if "Sub_Application" in df.columns
                  else ["Job_Name"])

    # Peak/avg from OK runs only — FAILED runs with 0-sec or inflated
    # wall-clock times must not skew SLA peak metrics.
    has_status = "Status" in df.columns
    ok_df = df[df["Status"] == "OK"] if has_status else df
    if ok_df.empty:
        ok_df = df  # fallback: all rows if every run failed

    top_jobs = (ok_df.groupby(group_cols)["run_time_hrs"]
                  .agg(["max", "mean", "sum"]).reset_index()
                  .rename(columns={"max": "peak_hrs", "mean": "avg_hrs", "sum": "total_hrs"})
                  .sort_values("peak_hrs", ascending=False))

    # Include jobs that only have FAILED runs (missing from ok_df groupby)
    if has_status:
        all_keys = df.groupby(group_cols).size().reset_index(name="_n")[group_cols]
        top_jobs = all_keys.merge(top_jobs, on=group_cols, how="left")
        top_jobs["peak_hrs"]  = top_jobs["peak_hrs"].fillna(0.0)
        top_jobs["avg_hrs"]   = top_jobs["avg_hrs"].fillna(0.0)
        top_jobs["total_hrs"] = top_jobs["total_hrs"].fillna(0.0)
        top_jobs = top_jobs.sort_values("peak_hrs", ascending=False)

        # Failure count per job
        fail_df = df[df["Status"] == "FAILED"]
        if not fail_df.empty:
            fail_counts = fail_df.groupby(group_cols).size().reset_index(name="fail_count")
            top_jobs = top_jobs.merge(fail_counts, on=group_cols, how="left")
            top_jobs["fail_count"] = top_jobs["fail_count"].fillna(0).astype(int)
        else:
            top_jobs["fail_count"] = 0
    else:
        top_jobs["fail_count"] = 0

    # ── Per-job SLA resolution ───────────────────────────────────
    # Build SLA index if caller didn't supply one
    if sla_index is None:
        sla_index = build_sla_index(df)

    job_sla_map = sla_index.get("job_sla", {})
    global_ceil = sla_index.get("global_ceiling", _detect_sla_ceiling(df))

    def _get_job_sla(row) -> float:
        """Look up per-job SLA. Falls back gracefully through index → global."""
        sub_app  = str(row.get("Sub_Application", "")) if "Sub_Application" in row.index else ""
        job_name = str(row.get("Job_Name", ""))
        key      = f"{sub_app}|{job_name}" if sub_app else job_name
        entry = job_sla_map.get(key)
        if entry and entry.get("sla_hrs", 0) > 0:
            return float(entry["sla_hrs"])
        # Alt key (job only)
        entry2 = job_sla_map.get(job_name)
        if entry2 and entry2.get("sla_hrs", 0) > 0:
            return float(entry2["sla_hrs"])
        return global_ceil

    top_jobs["sla_hrs"]      = top_jobs.apply(_get_job_sla, axis=1)
    top_jobs["sla_source"]   = top_jobs.apply(
        lambda r: (job_sla_map.get(
            f"{r.get('Sub_Application','') if 'Sub_Application' in r.index else ''}|{r.get('Job_Name','')}",
            job_sla_map.get(str(r.get("Job_Name", "")), {})
        ) or {}).get("source", "default"),
        axis=1,
    )

    # Gap 3 — pre-agreed buffer from SLA contract (contractual, from SLA file)
    def _get_pre_agreed(r):
        entry = (job_sla_map.get(
            f"{r.get('Sub_Application','') if 'Sub_Application' in r.index else ''}|{r.get('Job_Name','')}",
            job_sla_map.get(str(r.get("Job_Name", "")), {})) or {})
        # Prefer explicit pre_agreed_buffer_hrs, fall back to buffer_minutes/60
        pab = entry.get("pre_agreed_buffer_hrs")
        if pab is not None:
            return round(float(pab), 3)
        bm = entry.get("buffer_minutes")
        if bm is not None:
            return round(float(bm) / 60, 3)
        return None

    top_jobs["pre_agreed_buffer_hrs"] = top_jobs.apply(_get_pre_agreed, axis=1)

    # Two-tier SLA contract type per job (JOB_SPECIFIC | SOW_SCHEDULE | INFERRED)
    def _get_contract_type(r):
        entry = (job_sla_map.get(
            f"{r.get('Sub_Application','') if 'Sub_Application' in r.index else ''}|{r.get('Job_Name','')}",
            job_sla_map.get(str(r.get("Job_Name", "")), {})) or {})
        return entry.get("sla_contract_type", "INFERRED")

    top_jobs["sla_contract_type"] = top_jobs.apply(_get_contract_type, axis=1)

    # F3 — observed SLA Buffer from Ctrl-M data (sla_hrs - peak_hrs)
    # Guard against sla_hrs=0 (SLA_MISSING rows) — produces inf/NaN without the guard.
    # Use np.where so the entire column is vectorised; NaN propagates correctly downstream.
    _sla_safe = top_jobs["sla_hrs"].replace(0, float("nan"))
    top_jobs["buffer_pct"]   = ((_sla_safe - top_jobs["peak_hrs"]) / _sla_safe * 100).round(1)
    top_jobs["sla_used_pct"] = (top_jobs["peak_hrs"] / _sla_safe * 100).round(1)
    # Unify status labels with pe_config thresholds (same as sla_matrix _compute_sla_matrix).
    # Removes CRITICAL/CAUTION/HEALTHY/EXCELLENT labels that differ from AT_RISK/LONG_JOB/OK.
    _at  = float(getattr(pe_config, 'SLA_ATRISK_PCT',  15.0))
    _lj  = float(getattr(pe_config, 'SLA_LONGJOB_PCT', 40.0))
    import math as _math_tj
    def _buf_status(b):
        if b is None or (isinstance(b, float) and _math_tj.isnan(b)):
            return "SLA_MISSING"
        if b < 0:      return "BREACH"
        if b <= _at:   return "AT_RISK"
        if b <= _lj:   return "LONG_JOB"
        return "OK"
    top_jobs["buffer_status"] = top_jobs["buffer_pct"].apply(_buf_status)

    # Round floats for JSON cleanliness
    for col in ("peak_hrs", "avg_hrs", "total_hrs", "sla_hrs"):
        top_jobs[col] = top_jobs[col].round(3)

    # ── Tag utility jobs (FileWatcher, DB backup, export, health-check) ──────
    # is_utility=True → frontend can filter these out of the SLA buffer table.
    # Backend keeps them in the dataset so run/fail counts are still tracked.
    #
    # Sub_Application confirmation guard (Area 1 tightening):
    # Patterns fall into two tiers:
    #   STRONG — unambiguous utility regardless of context (file_watcher, ping_job)
    #   WEAK   — everything else; only auto-excluded when the job's sub_app is NOT
    #            batch-productive (i.e. the sub_app has < 2 non-utility jobs).
    # This prevents false-positive exclusion of legitimate batch jobs that happen
    # to contain a matching substring in a productive batch sub_app.
    _STRONG_UTIL: frozenset = frozenset({
        "file_watcher", "filewatcher", "ctrl_m_file_watcher", "ping_job",
    })
    _util_patterns = [str(p).lower() for p in getattr(pe_config, "UTILITY_JOB_PATTERNS", [])]
    if _util_patterns and "Job_Name" in top_jobs.columns:
        import re as _re_util

        def _norm_name(n: str) -> str:
            return _re_util.sub(r"[\s\-]+", "_", str(n)).lower()

        def _util_hit(name: str) -> tuple:
            norm = _norm_name(name)
            for pat in _util_patterns:
                if pat in norm:
                    return True, pat
            return False, ""

        # First pass: find all pattern-based candidates
        _util_results = top_jobs["Job_Name"].apply(_util_hit)
        top_jobs["_is_cand"]  = _util_results.apply(lambda t: t[0])
        top_jobs["_cand_pat"] = _util_results.apply(lambda t: t[1])

        # Build batch-productive sub_apps: sub_apps where ≥ 2 jobs are NOT
        # pattern-candidates (they do real batch work). Only meaningful when
        # Sub_Application is present in the frame (composite-key grouping).
        _batch_subs: set = set()
        if "Sub_Application" in top_jobs.columns:
            _non_cand = top_jobs[~top_jobs["_is_cand"]]
            if not _non_cand.empty:
                _sub_cnts = _non_cand.groupby("Sub_Application").size()
                _batch_subs = set(_sub_cnts[_sub_cnts >= 2].index)

        def _is_util(row) -> bool:
            if not row["_is_cand"]:
                return False
            if row["_cand_pat"] in _STRONG_UTIL:
                return True   # strong match — utility regardless of sub_app context
            # Weak match: only utility when the sub_app is NOT batch-productive
            sub = str(row.get("Sub_Application", "")) if "Sub_Application" in row.index else ""
            return sub not in _batch_subs

        top_jobs["is_utility"]     = top_jobs.apply(_is_util, axis=1)
        top_jobs["utility_reason"] = top_jobs.apply(
            lambda r: r["_cand_pat"] if r["is_utility"] else "", axis=1
        )
        top_jobs.drop(columns=["_is_cand", "_cand_pat"], inplace=True)
    else:
        top_jobs["is_utility"]     = False
        top_jobs["utility_reason"] = ""

    return top_jobs


def _detect_sla_mode(df: pd.DataFrame) -> str:
    """Return the dominant schedule type string: 'DAILY', 'WEEKLY', or 'MONTHLY'.

    Inspects sub-application names to detect the dominant schedule type.
    Returns 'DAILY' when inconclusive.
    """
    try:
        from services.sla_engine import classify_schedule as _cs
    except Exception:
        return "DAILY"

    schedule_votes: Dict[str, int] = {"DAILY": 0, "WEEKLY": 0, "MONTHLY": 0}
    for col in ("Sub_Application", "Folder", "Application"):
        if col not in df.columns:
            continue
        for name in df[col].dropna().unique():
            stype = _cs(str(name))
            if stype in schedule_votes:
                schedule_votes[stype] += 1

    dominant, votes = max(schedule_votes.items(), key=lambda kv: kv[1])
    return dominant if votes > 0 else "DAILY"


def _detect_sla_ceiling(df: pd.DataFrame) -> float:
    """Return the appropriate SLA ceiling for this batch dataset.

    Reads the sub-application names to detect whether the batch runs DAILY,
    WEEKLY, or MONTHLY (e.g. "PROD_WEEKLY_WF1_REQPL" → WEEKLY), then returns
    the matching pe_config ceiling.  Falls back to SLA_DAILY_HRS when the
    schedule type cannot be determined.

    NOTE: This is the *global* fallback used when no SLA matrix has been
    uploaded.  When contracts are available, build_sla_index() + per-job
    resolution is used instead (see build_top_jobs_df).
    """
    try:
        from services.sla_engine import classify_schedule as _cs
    except Exception:
        return pe_config.SLA_DAILY_HRS

    schedule_votes: Dict[str, int] = {"DAILY": 0, "WEEKLY": 0, "MONTHLY": 0}
    for col in ("Sub_Application", "Folder", "Application"):
        if col not in df.columns:
            continue
        for name in df[col].dropna().unique():
            stype = _cs(str(name))
            if stype in schedule_votes:
                schedule_votes[stype] += 1

    dominant, votes = max(schedule_votes.items(), key=lambda kv: kv[1])
    if votes == 0:
        return pe_config.SLA_DAILY_HRS

    ceiling_map: Dict[str, float] = {
        "DAILY":   pe_config.SLA_DAILY_HRS,
        "WEEKLY":  pe_config.SLA_WEEKLY_HRS,
        "MONTHLY": pe_config.SLA_MONTHLY_HRS,
    }
    return ceiling_map.get(dominant, pe_config.SLA_DAILY_HRS)


def build_sla_index(df: pd.DataFrame) -> Dict[str, Any]:
    """Build a per-job SLA index by joining the batch DataFrame against the
    uploaded SLA contracts (via resolve_sla's adaptive name matching).

    Returns a dict with two keys:
      "job_sla"   : {job_key → sla_hrs}   (job_key = "SubApp|JobName")
      "global_ceiling" : float             (dominant schedule ceiling, fallback)
      "contracts" : list[SlaContract]      (raw contracts for audit trail)
      "ceilings"  : dict                   (schedule-type → hrs from customer file)
      "source"    : "sla_matrix" | "default"

    When no SLA file has been uploaded, job_sla is empty and global_ceiling is
    the schedule-aware default.
    """
    from services import config_store as _cs
    from services.sla_engine import resolve_sla, classify_schedule, SlaContract

    global_ceiling = _detect_sla_ceiling(df)
    result: Dict[str, Any] = {
        "job_sla": {},
        "global_ceiling": global_ceiling,
        "contracts": [],
        "ceilings": {},
        "source": "default",
    }

    # Load SLA intelligence from config_store (set when SLA file uploaded)
    intel = None
    try:
        intel = _cs.get("_sla_intelligence")
    except Exception:
        pass

    if not intel or not isinstance(intel, dict):
        return result

    raw_contracts = intel.get("contracts", [])
    raw_ceilings  = intel.get("ceilings", {})

    if not raw_contracts:
        return result

    # Reconstruct SlaContract objects from the stored dicts
    contracts: list = []
    for c in raw_contracts:
        if not isinstance(c, dict):
            continue
        try:
            # Reconstruct SlaContract — include sla_start/sla_end for absolute-time
            # comparison (Gap 2) and pre_agreed_buffer_hrs (Gap 1)
            from datetime import time as _dtime
            def _t(v):  # parse ISO time string → datetime.time, or None
                try:
                    return _dtime.fromisoformat(str(v)) if v else None
                except Exception:
                    return None
            contracts.append(SlaContract(
                batch_name=c.get("batch_name", ""),
                schedule_type=c.get("schedule_type", "UNKNOWN"),
                schedule_raw=c.get("schedule_raw", ""),
                sla_model=c.get("sla_model", "WINDOW"),
                sla_start=_t(c.get("sla_start")),
                sla_end=_t(c.get("sla_end")),
                sla_window_hrs=c.get("sla_window_hrs"),
                sla_duration_hrs=c.get("sla_duration_hrs"),
                buffer_minutes=c.get("buffer_minutes"),
                pre_agreed_buffer_hrs=c.get("pre_agreed_buffer_hrs"),
                source_row=c.get("source_row", 0),
                completeness=c.get("completeness", "complete"),
                # Two-tier classification: restore from cache (or fall back to INFERRED)
                sla_source_type=c.get("sla_source_type", "INFERRED"),
            ))
        except Exception:
            continue

    result["contracts"] = contracts
    result["ceilings"]  = {k: float(v) for k, v in raw_ceilings.items() if v}
    result["source"]    = "sla_matrix"

    # ── Best-sheet contract selection (Fix 2) ────────────────────────────────
    # When the SLA XLSX has multiple sheets (e.g. a generic C&A template on
    # Sheet1 and a customer-specific layout on Sheet2), contracts from ALL
    # sheets are stored but the generic sheet's batch names ("DAILY", "WEEKLY")
    # fuzzy-match everything and mask more-specific contracts.
    #
    # Algorithm: score each source_sheet by how many of its contract batch
    # names appear in (or are contained by) the current CSV's sub_application
    # names.  Keep contracts from sheets that score at least 70% of the best
    # sheet's score.  If all sheets score 0 (no match at all), keep everything
    # so we never discard potentially useful data.
    if len(contracts) > 1 and "Sub_Application" in df.columns:
        _sub_app_set: set[str] = {
            str(s).upper() for s in df["Sub_Application"].dropna().unique()
            if s and str(s).upper() not in ("UNKNOWN", "")
        }
        if _sub_app_set:
            _sheet_stats: dict[str, dict] = {}
            for _c in contracts:
                _sh = (_c.source_sheet or "unknown") if hasattr(_c, "source_sheet") else "unknown"
                _sh_entry = _sheet_stats.setdefault(_sh, {"hits": 0, "total": 0})
                _sh_entry["total"] += 1
                _bname = (_c.batch_name or "").upper()
                if _bname and any(_bname in _sa or _sa in _bname for _sa in _sub_app_set):
                    _sh_entry["hits"] += 1
            # Only filter when we actually have multiple sheets with SLA data
            if len(_sheet_stats) > 1:
                _scores = {sh: v["hits"] / max(v["total"], 1) for sh, v in _sheet_stats.items()}
                _max_score = max(_scores.values())
                if _max_score > 0:
                    # Keep all sheets scoring ≥70% of the best sheet
                    _good_sheets = {
                        sh for sh, sc in _scores.items() if sc >= _max_score * 0.7
                    }
                    contracts = [
                        c for c in contracts
                        if (getattr(c, "source_sheet", None) or "unknown") in _good_sheets
                    ]
                    result["contracts"] = contracts

    # Resolve SLA for every unique (Sub_Application, Job_Name) pair
    group_cols = (["Sub_Application", "Job_Name"]
                  if "Sub_Application" in df.columns else ["Job_Name"])
    name_col   = "Sub_Application" if "Sub_Application" in df.columns else "Job_Name"

    for _, row in df[group_cols].drop_duplicates().iterrows():
        job_name = str(row.get("Job_Name", ""))
        sub_app  = str(row.get("Sub_Application", "")) if "Sub_Application" in row else ""
        # Use Sub_Application as primary name hint (closer to SLA file naming)
        primary  = sub_app or job_name
        sched_hint = primary

        resolved = resolve_sla(primary, sched_hint, contracts, result["ceilings"])
        # Fallback: try Job_Name if Sub_App didn't match
        if resolved.source == "assumed" and job_name and job_name != primary:
            alt = resolve_sla(job_name, job_name, contracts, result["ceilings"])
            if alt.source != "assumed":
                resolved = alt

        key = f"{sub_app}|{job_name}" if sub_app else job_name
        _mc = resolved.matched_contract
        result["job_sla"][key] = {
            "sla_hrs":              resolved.sla_hrs,
            "source":               resolved.source,
            "confidence":           resolved.confidence,
            "detail":               resolved.source_detail,
            "schedule_type":        resolved.schedule_type,
            # Gap 3: carry contractual buffer through to top_jobs_df
            "buffer_minutes":       resolved.buffer_minutes,
            "pre_agreed_buffer_hrs": getattr(_mc, "pre_agreed_buffer_hrs", None) if _mc else None,
            "sla_end_time":         getattr(_mc, "sla_end", None).isoformat()
                                    if _mc and getattr(_mc, "sla_end", None) else None,
            # Two-tier SLA classification: JOB_SPECIFIC | SOW_SCHEDULE | INFERRED
            "sla_contract_type":    getattr(_mc, "sla_source_type", "INFERRED") if _mc else "INFERRED",
        }

    return result


# ─────────────────────────────────────────────────────────────────
# detect_cyclic_subs — generic cyclic/polling sub-app filter
# ─────────────────────────────────────────────────────────────────
def detect_cyclic_subs(df: pd.DataFrame, threshold: int = 20) -> set:
    """Return a set of Sub_Application names that match cyclic/polling behaviour.

    Uses a TWO-GUARD algorithm (both must pass — neither alone is sufficient):

    Guard 1 — Frequency (MEDIAN-based, not max-based):
      median(runs_per_day) > 5 on active dates
      AND avg_runs_per_job > 3 (repetition per individual job, not just high total count)

    Using MEDIAN (not max) is critical:
      max > 5 alone catches retry storms — a batch job failing 1435 times on one
      date creates max=1435 while median=1 (normal). That's NOT cyclic, it's a
      RETRY STORM (automated job retry cascade after failure).

      cyclic:      median=24, max=26  → both guards fire → CYCLIC
      retry storm: median=1,  max=1435 → Guard 1 fails (median ≤5) → NOT CYCLIC

    Guard 2 — Duration:
      avg_runtime_hrs < CYCLIC_MAX_RUNTIME_HRS (default 0.25h / 15 min)
      Real batch jobs rarely complete in under 15 minutes.
      CDC/polling loops typically complete in seconds.

    Returns (cyclic_subs, retry_storm_info) — retry_storm_info is a list of
    {"sub_app", "job_name", "date", "run_count"} for warning surface.
    Use detect_cyclic_subs.retry_storms attribute for warnings.
    """
    if "Sub_Application" not in df.columns or "run_date" not in df.columns:
        detect_cyclic_subs.retry_storms = []
        return set()

    runs_per_day = (
        df.groupby(["Sub_Application", "run_date"])
          .size()
          .reset_index(name="daily_runs")
    )

    # Guard 1 — Frequency: MEDIAN > 5 (not max) to exclude retry storms
    med_per_day = runs_per_day.groupby("Sub_Application")["daily_runs"].median()
    freq_candidates = set(med_per_day[med_per_day > 5].index)

    # ── Retry storm detection (separate from cyclic) ──────────────────────────
    # max >> median (ratio > 20×) on dates NOT in freq_candidates → retry storm
    max_per_day   = runs_per_day.groupby("Sub_Application")["daily_runs"].max()
    retry_storms: list = []
    for sa in runs_per_day["Sub_Application"].unique():
        sa_str = str(sa)
        med = float(med_per_day.get(sa, 0))
        mx  = float(max_per_day.get(sa, 0))
        if med < 5 and mx > 0 and mx / max(med, 1) >= 20:
            # Retry storm: find the spike dates
            sa_daily = runs_per_day[runs_per_day["Sub_Application"] == sa]
            spike_rows = sa_daily[sa_daily["daily_runs"] >= max(mx * 0.5, 20)]
            for _, row in spike_rows.iterrows():
                # Find the most frequent job on that date
                day_jobs = df[
                    (df["Sub_Application"] == sa) &
                    (df["run_date"] == row["run_date"]) &
                    ("Job_Name" in df.columns)
                ]
                top_job = ""
                if "Job_Name" in day_jobs.columns and not day_jobs.empty:
                    top_job = str(day_jobs["Job_Name"].value_counts().index[0])
                retry_storms.append({
                    "sub_app":   sa_str,
                    "job_name":  top_job,
                    "date":      str(row["run_date"]),
                    "run_count": int(row["daily_runs"]),
                })
    # Expose retry storms as function attribute so compute_metrics can surface them
    detect_cyclic_subs.retry_storms = retry_storms

    if not freq_candidates:
        return set()

    # Guard 1b: avg runs-per-unique-job-per-day > 3
    jobs_per_day = (
        df.groupby(["Sub_Application", "run_date"])["Job_Name"]
          .nunique()
          .reset_index(name="daily_jobs")
    )
    avg_per_day      = runs_per_day.groupby("Sub_Application")["daily_runs"].mean()
    avg_jobs         = jobs_per_day.groupby("Sub_Application")["daily_jobs"].mean()
    avg_runs_per_job = avg_per_day / avg_jobs.clip(lower=1)
    freq_confirmed   = {s for s in freq_candidates if avg_runs_per_job.get(s, 0) > 3}

    if not freq_confirmed:
        return set()

    # Guard 2 — Duration: avg_runtime_hrs < CYCLIC_MAX_RUNTIME_HRS (15 min)
    if "run_time_hrs" in df.columns:
        avg_runtime = df.groupby("Sub_Application")["run_time_hrs"].mean()
        _min_hrs    = float(getattr(pe_config, "CYCLIC_MAX_RUNTIME_HRS", 0.25))
        return {s for s in freq_confirmed if avg_runtime.get(s, 999.0) < _min_hrs}

    # run_time_hrs not yet computed — fall back to frequency guard only
    return freq_confirmed


# ─────────────────────────────────────────────────────────────────
# compute_metrics — extracted from app_v2.py:2314
# ─────────────────────────────────────────────────────────────────
def compute_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """Aggregate Ctrl-M runs into KPIs, time-series and top-jobs slices.

    RULE 6: Job-level counts (t_jobs, j_breach) are derived from build_top_jobs_df
    which uses the composite [Sub_Application, Job_Name] key.  Time-series
    (daily/window) keep Job_Name-only grouping for chart compatibility.

    SLA MATH RULE: Every SLA ceiling is resolved per-job from the uploaded
    SLA matrix (via build_sla_index → resolve_sla).  No hardcoded customer-
    specific values.  Falls back to schedule-type ceiling, then pe_config
    default.  Source is tagged on every resolved value.
    """
    # ── Stage 2A: User-specified job exclusions (config_store["exclude_jobs"]) ─
    # Jobs the analyst has explicitly excluded from SLA/buffer analysis.
    # Applied to the ANALYSIS copy only — raw df is preserved for fail_count,
    # heatmap, and time-series charts so excluded jobs remain visible there.
    _user_excl_jobs: set = set()
    try:
        from services import config_store as _cfg_excl
        _excl_list = _cfg_excl.get("exclude_jobs") or []
        if isinstance(_excl_list, list):
            _user_excl_jobs = {str(j).strip() for j in _excl_list if j and str(j).strip()}
    except Exception:
        pass

    df_analysis = df
    if _user_excl_jobs and "Job_Name" in df.columns:
        df_analysis = df[~df["Job_Name"].isin(_user_excl_jobs)].copy()
        logger.debug(
            "compute_metrics: %d user-excluded jobs removed from SLA analysis",
            len(_user_excl_jobs),
        )

    # ── Build SLA index once (per-job ceiling from uploaded contracts) ───────
    sla_index     = build_sla_index(df_analysis)
    job_sla_map   = sla_index.get("job_sla", {})
    global_ceil   = sla_index.get("global_ceiling", _detect_sla_ceiling(df_analysis))
    sla_src_type  = sla_index.get("source", "default")

    # ── Cyclic sub-app detection (Fix 1) ─────────────────────────────────────
    # Sub-apps averaging >20 runs/day are cyclic (hourly CDC, polling, monitoring).
    # They are EXCLUDED from elapsed-window measurement because including them
    # inflates min(Start_Time)→max(End_Time) to ~24h — causing false 0% batch
    # window compliance even when the real batch finishes well within 6h.
    #
    # Job-level stats (peak_hrs, buffer, SLA breach per individual job) are NOT
    # affected — we still count cyclic jobs for compliance/anomaly purposes.
    # Only the wall-clock elapsed window measurement excludes them.
    cyclic_subs = detect_cyclic_subs(df_analysis) if "Sub_Application" in df_analysis.columns else set()
    # Surface retry storm warnings — spike dates with automated retry cascades
    _retry_storms: list = getattr(detect_cyclic_subs, "retry_storms", [])
    if _retry_storms:
        for _rs in _retry_storms:
            logger.warning(
                "compute_metrics: RETRY STORM detected — sub_app=%s job=%s date=%s runs=%d. "
                "This is NOT a cyclic job. Investigate failure root cause.",
                _rs["sub_app"], _rs["job_name"], _rs["date"], _rs["run_count"],
            )
    # df_window: dataframe used for wall-clock elapsed window computation only.
    df_window = (
        df_analysis[~df_analysis["Sub_Application"].isin(cyclic_subs)].copy()
        if cyclic_subs and "Sub_Application" in df_analysis.columns
        else df_analysis
    )

    # ── Stage 2B: Compliance scope — exclude out-of-scope schedule types ─────
    # Sub_Apps classified as MONTHLY, QUARTERLY, ADHOC, or CYCLIC have no
    # SLA window target → including them in compliance % inflates the denominator
    # with "never breach" rows that were never tracked.
    # UNKNOWN is intentionally KEPT in scope (Level 4 fallback → 6h default SLA).
    _out_of_scope_subs: set = set(_user_excl_jobs)   # start with manually excluded
    _out_of_scope_subs.update(cyclic_subs)           # cyclic = never had batch SLA

    # Track exclusion reasons per sub_app for frontend display
    _excl_sub_reasons: dict = {}   # sub_app → {"reason": "CYCLIC|...", "job_count": N, "peak_hrs": X}
    for _sa in cyclic_subs:
        _excl_sub_reasons[str(_sa)] = {"reason": "CYCLIC", "auto": True}

    try:
        from services.sla_engine import classify_schedule as _classify_sched
        _NO_SCOPE = {
            "MONTHLY", "BIMONTHLY", "DATE_SPECIFIC_MONTHLY", "QUARTERLY",
            "PIPELINE_STAGE", "ADHOC", "CYCLIC", "CYCLIC_INTERVAL", "CALENDAR_BASED",
            "OUTBOUND",
        }
        if "Sub_Application" in df_analysis.columns:
            for _sa in df_analysis["Sub_Application"].dropna().unique():
                _sched = _classify_sched(str(_sa))
                if _sched in _NO_SCOPE:
                    _out_of_scope_subs.add(str(_sa))
                    if str(_sa) not in _excl_sub_reasons:
                        _excl_sub_reasons[str(_sa)] = {"reason": _sched, "auto": True}
    except Exception:
        pass  # classification unavailable — keep all sub_apps in scope

    # Enrich exclusion records with job counts and peak hours
    if "Sub_Application" in df_analysis.columns and _excl_sub_reasons:
        for _sa, _info in _excl_sub_reasons.items():
            _sub_df = df_analysis[df_analysis["Sub_Application"].astype(str) == _sa]
            _info["job_count"] = int(_sub_df["Job_Name"].nunique()) if "Job_Name" in _sub_df.columns else 0
            _info["peak_hrs"]  = round(float(_sub_df["run_time_hrs"].max()), 2) if "run_time_hrs" in _sub_df.columns and not _sub_df.empty else 0.0


    # Per-job SLA breach counting ──────────────────────────────────────────────
    # Breach = Sub_Application window (wall-clock: last End_Time − first Start_Time
    # per Sub_App per run_date) > per-job SLA ceiling resolved from SLA matrix.
    #
    # Falls back to summed run_time_hrs per Job_Name per run_date when
    # End_Time is not available (wall-clock can't be computed).
    # G20a: require ≥30% End_Time coverage to avoid misleading elapsed windows
    # when only a handful of rows have End_Time populated.
    _end_coverage = 0.0
    if "End_Time" in df_analysis.columns and len(df_analysis) > 0:
        _end_coverage = df_analysis["End_Time"].notna().sum() / len(df_analysis)
    has_end_time = "End_Time" in df_analysis.columns and _end_coverage >= 0.30

    # Canonical per-(sub_app, run_date) window records for the shared compliance
    # engine. Populated only when wall-clock windows can be measured.
    _win_records: list = []
    _win_ceiling_map: dict = {}

    if has_end_time and "Sub_Application" in df_analysis.columns:
        # Primary: wall-clock window per Sub_Application per run_date.
        # Use df_window (cyclic + excluded sub-apps removed) to avoid 24h elapsed inflation.
        window_agg = (
            df_window.groupby(["Sub_Application", "run_date"], as_index=False)
              .agg(
                  first_start=("Start_Time", "min"),
                  last_end=("End_Time", "max"),
                  job_name=("Job_Name", "first"),  # representative job name
              )
        )
        # Filter out-of-scope sub_apps from compliance denominator
        if _out_of_scope_subs:
            window_agg = window_agg[
                ~window_agg["Sub_Application"].isin(_out_of_scope_subs)
            ].copy()

        window_agg["elapsed_hrs"] = (
            (window_agg["last_end"] - window_agg["first_start"])
            .dt.total_seconds() / 3600.0
        ).clip(lower=0).fillna(0.0)

        def _sub_sla(row) -> float:
            sub_app  = str(row["Sub_Application"])
            job_name = str(row["job_name"])
            key = f"{sub_app}|{job_name}"
            entry = job_sla_map.get(key) or job_sla_map.get(job_name)
            if entry and entry.get("sla_hrs", 0) > 0:
                return float(entry["sla_hrs"])
            return global_ceil

        window_agg["sla_hrs"] = window_agg.apply(_sub_sla, axis=1)
        window_agg["breach"]  = window_agg["elapsed_hrs"] > window_agg["sla_hrs"]

        # ── Canonical window records (one per (sub_app, run_date)) ───────────
        # Feeds the SHARED compliance_engine so the Batch Review and SLA Matrix
        # tabs compute window compliance with one identical algorithm. The
        # per-sub_app resolved sla_hrs IS the ceiling (Area 2 — no global float).
        try:
            from services.sla_engine import classify_schedule as _cs_win
        except Exception:
            _cs_win = lambda _n: "DAILY"   # noqa: E731
        for _sa in window_agg["Sub_Application"].dropna().unique():
            _win_ceiling_map[str(_sa)] = float(
                window_agg.loc[window_agg["Sub_Application"] == _sa, "sla_hrs"].min()
            )
        for _, _r in window_agg.iterrows():
            _win_records.append({
                "sub_app":       str(_r["Sub_Application"]),
                "run_date":      str(_r["run_date"]),
                "elapsed_hrs":   float(_r["elapsed_hrs"]),
                "schedule_type": _cs_win(str(_r["Sub_Application"])),
                "sla_ceil":      float(_r["sla_hrs"]),
            })

        # Merge back to daily (Job_Name level) for compatibility with heatmap
        daily = (df_analysis.groupby(["Job_Name", "run_date"], as_index=False)
                   .agg(total_hrs=("run_time_hrs", "sum"), runs=("run_time_hrs", "count")))
        # Carry the per-Sub_App breach flags into the daily Job_Name frame
        sub_breach = (window_agg.groupby("run_date")["breach"].any().reset_index()
                                .rename(columns={"breach": "_day_breach"}))
        daily = daily.merge(sub_breach, on="run_date", how="left")
        daily["breach"] = daily["_day_breach"].fillna(False)
        daily.drop(columns=["_day_breach"], inplace=True)

        # Per-job breach column for heatmap (per-job wall-clock vs per-job SLA)
        job_breach_map = {}
        for _, r in window_agg.iterrows():
            key = (str(r["Sub_Application"]), str(r["run_date"]))
            job_breach_map[key] = bool(r["breach"])

    else:
        # Fallback: per-Job_Name per-day sum vs per-job SLA
        daily = (df_analysis.groupby(["Job_Name", "run_date"], as_index=False)
                   .agg(total_hrs=("run_time_hrs", "sum"), runs=("run_time_hrs", "count")))

        def _job_sla_for_row(row) -> float:
            job_name = str(row["Job_Name"])
            entry = job_sla_map.get(job_name)
            if entry and entry.get("sla_hrs", 0) > 0:
                return float(entry["sla_hrs"])
            return global_ceil

        daily["sla_hrs"] = daily.apply(_job_sla_for_row, axis=1)
        daily["breach"]  = daily["total_hrs"] > daily["sla_hrs"]
        job_breach_map   = {}

    monthly_lim = pe_config.SLA_MONTHLY_HRS
    monthly = (df_analysis.groupby(["Job_Name", "month"], as_index=False)
                 .agg(total_hrs=("run_time_hrs", "sum")))
    monthly["breach"] = monthly["total_hrs"] > monthly_lim

    total_d = len(daily)
    # F4 — Job SLA Compliance is computed AFTER top_jobs is built (see below),
    # derived from the scoped per-job buffer_status so it ALWAYS agrees with the
    # breach / at-risk KPI tiles. Placeholder kept for any early reference.
    job_sla_comp = 0.0

    # ── Daily batch window time-series ───────────────────────────
    window = (df_analysis.groupby("run_date", as_index=False)
                .agg(total_hrs=("run_time_hrs", "sum"),
                     job_count=("Job_Name", "nunique"))
                .sort_values("run_date"))

    elapsed_available  = False
    window_breach_days = 0
    worst_elapsed_kpi  = 0.0
    avg_elapsed_kpi    = 0.0
    worst_elapsed_date = ""

    if has_end_time:
        try:
            # Elapsed window per (Sub_Application, run_date). Grouping by run_date
            # ALONE collapses every sub_app into one window (earliest start of ANY
            # sub_app → latest end of ANY sub_app), which massively inflates the
            # window for multi-sub_app customers. Group per sub_app first, then
            # take the worst sub_app window per date.
            if "window_agg" in dir() and not window_agg.empty:
                # Reuse per-sub_app window already computed (out-of-scope filtered)
                elap_sub = window_agg[["Sub_Application", "run_date", "elapsed_hrs"]].copy()
            elif "Sub_Application" in df_window.columns:
                elap_sub = (df_window.groupby(["Sub_Application", "run_date"], as_index=False)
                              .agg(first_start=("Start_Time", "min"),
                                   last_end=("End_Time", "max")))
                elap_sub["elapsed_hrs"] = (
                    (elap_sub["last_end"] - elap_sub["first_start"]).dt.total_seconds() / 3600.0
                ).clip(lower=0).fillna(0.0)
                if _out_of_scope_subs:
                    elap_sub = elap_sub[
                        ~elap_sub["Sub_Application"].astype(str).isin(_out_of_scope_subs)
                    ]
            else:
                # No sub_app dimension — single window per date
                elap_sub = (df_window.groupby("run_date", as_index=False)
                              .agg(first_start=("Start_Time", "min"),
                                   last_end=("End_Time", "max")))
                elap_sub["Sub_Application"] = "ALL"
                elap_sub["elapsed_hrs"] = (
                    (elap_sub["last_end"] - elap_sub["first_start"]).dt.total_seconds() / 3600.0
                ).clip(lower=0).fillna(0.0)

            elap_sub = elap_sub.dropna(subset=["run_date"])
            if not elap_sub.empty:
                # Bar chart: worst sub_app window per date (max across in-scope sub_apps)
                elap_daily = (elap_sub.groupby("run_date")["elapsed_hrs"]
                                .max().reset_index())
                window = window.merge(elap_daily, on="run_date", how="left")
                window["elapsed_hrs"] = window["elapsed_hrs"].fillna(0.0).round(3)
                elapsed_available = True

                # Headline KPI: DAILY/UNKNOWN sub_apps only — WEEKLY sub_apps would
                # inflate the "daily elapsed window" headline tile.
                try:
                    from services.sla_engine import classify_schedule as _cs_kpi
                    _daily_sas = {
                        sa for sa in elap_sub["Sub_Application"].dropna().unique()
                        if str(sa) == "ALL" or _cs_kpi(str(sa)) in {"DAILY", "UNKNOWN"}
                    }
                except Exception:
                    _daily_sas = set(elap_sub["Sub_Application"].dropna().unique())
                _kpi_src = (elap_sub[elap_sub["Sub_Application"].isin(_daily_sas)]
                            if _daily_sas else elap_sub)
                _kpi_daily = _kpi_src.groupby("run_date")["elapsed_hrs"].max()
                if not _kpi_daily.empty:
                    worst_elapsed_kpi  = float(_kpi_daily.max())
                    avg_elapsed_kpi    = float(_kpi_daily.mean())
                    worst_elapsed_date = str(_kpi_daily.idxmax())
        except Exception:
            pass
    if not elapsed_available:
        window["elapsed_hrs"] = 0.0

    # ── Batch Window Compliance (wall-clock daily window vs per-sub-app SLA) ──
    # Fix #14: window_agg["breach"] already uses per-Sub_Application resolved SLA
    # (via _sub_sla()). Use that aggregated breach count instead of re-computing
    # with global_ceil, which caused false BREACHes for WEEKLY jobs when DAILY
    # dominated the global ceiling (e.g. global_ceil=8.833h, WEEKLY runs 9h → false BREACH).
    n_window_days = len(window)
    if n_window_days > 0:
        if has_end_time and "window_agg" in dir() and not window_agg.empty:
            # Per-sub-app breach already computed; aggregate to run_date level
            _sub_breach_daily = window_agg.groupby("run_date")["breach"].any()
            # Align to window's run_date index
            window_breach_days = int(_sub_breach_daily.reindex(window["run_date"]).fillna(False).sum())
        elif elapsed_available:
            window_breach_days = int((window["elapsed_hrs"] > global_ceil).sum())
        else:
            window_breach_days = int((window["total_hrs"] > global_ceil).sum())
        batch_window_comp = round((1 - window_breach_days / n_window_days) * 100, 1)
        # G14: carry the per-sub_app breach flag into the window series
        # so build_batch_payload uses the correct per-SLA breach, not global ceil.
        if has_end_time and "window_agg" in dir() and not window_agg.empty:
            _breach_by_date = (window_agg.groupby("run_date")["breach"]
                               .any().reset_index()
                               .rename(columns={"breach": "_sub_breach"}))
            window = window.merge(_breach_by_date, on="run_date", how="left")
            window["breach"] = window["_sub_breach"].fillna(False)
            window.drop(columns=["_sub_breach"], inplace=True, errors="ignore")
        elif "breach" not in window.columns:
            if elapsed_available:
                window["breach"] = window["elapsed_hrs"] > global_ceil
            else:
                window["breach"] = window["total_hrs"] > global_ceil
    else:
        batch_window_comp = 0.0

    # ── Canonical window compliance via SHARED engine (Areas 4 + 5) ──────────
    # The (sub_app, date)-granular compliance that BOTH the Batch Review and the
    # SLA Matrix tabs report. compliance_engine is the single definition so the
    # two screens can never diverge for the same data.
    window_compliance = {
        "compliance_pct": float(round(batch_window_comp, 1)),
        "breach_count": int(window_breach_days),
        "ok_count": 0, "at_risk_count": 0,
        "total_windows": int(n_window_days), "excluded_windows": 0,
        "granularity": "day",
    }
    if _win_records:
        try:
            from services import compliance_engine as _ce
            _wc = _ce.compute_window_compliance(_win_records, _win_ceiling_map)
            _wc["granularity"] = "sub_app_date"
            window_compliance = _wc
        except Exception:
            pass

    # Sub-application rollup
    sub = (df_analysis.groupby("Sub_Application", as_index=False)
             .agg(total_hrs=("run_time_hrs", "sum"),
                  jobs=("Job_Name", "nunique")))

    # ── Per-job frame + compliance scope ─────────────────────────
    top_jobs = build_top_jobs_df(df_analysis, sla_index=sla_index)

    # Compliance scope: drop utility jobs and out-of-scope schedule sub_apps
    # (MONTHLY / CYCLIC / OUTBOUND / etc already collected in _out_of_scope_subs).
    # This is the SAME scope used for the window compliance, so the breach tiles,
    # the compliance %, and the window numbers all agree by construction.
    _scope_jobs = top_jobs
    if "is_utility" in top_jobs.columns:
        _scope_jobs = _scope_jobs[~_scope_jobs["is_utility"].fillna(False)]
    if _out_of_scope_subs and "Sub_Application" in _scope_jobs.columns:
        _scope_jobs = _scope_jobs[
            ~_scope_jobs["Sub_Application"].astype(str).isin(_out_of_scope_subs)
        ]
    if _scope_jobs.empty:
        _scope_jobs = top_jobs   # fall back if scoping removed everything

    t_jobs    = int(len(_scope_jobs))
    j_breach  = int((_scope_jobs["buffer_status"] == "BREACH").sum())
    j_at_risk = int((_scope_jobs["buffer_status"] == "AT_RISK").sum())
    j_ok      = max(0, t_jobs - j_breach - j_at_risk)

    # F4 — Job SLA Compliance derived from the SAME scoped frame as the tiles.
    # Guarantees compliance% and breach / at-risk counts can never contradict.
    job_sla_comp = round(j_ok / t_jobs * 100, 1) if t_jobs > 0 else 0.0

    # Worst job = highest peak within the in-scope set (drives the SLA gauge)
    worst_job_name = ""
    worst_job_peak = 0.0
    worst_job_sla  = global_ceil
    if not _scope_jobs.empty:
        worst_idx = _scope_jobs["peak_hrs"].idxmax()
        worst_row = _scope_jobs.loc[worst_idx]
        worst_job_peak = float(worst_row["peak_hrs"])
        worst_job_name = str(worst_row.get("Job_Name", "?"))
        _wsla = worst_row.get("sla_hrs", global_ceil)
        worst_job_sla = float(_wsla) if pd.notna(_wsla) and float(_wsla) > 0 else global_ceil

    # F6 — Anomaly detection (uses raw df — anomalies in excluded jobs still matter)
    anomalies = detect_job_anomalies(df)

    # F3 — Fleet-level SLA buffer uses the worst job's OWN resolved SLA, not the
    # global ceiling. A WEEKLY job must be measured against its 8h ceiling, not
    # the 6h daily majority-vote global value.
    fleet_sla_buffer = (
        calculate_sla_buffer(worst_job_sla, worst_job_peak)
        if worst_job_peak > 0 else None
    )

    # ── Data coverage / confidence (raw df — full picture) ──────────────────
    unique_dates = sorted(df["run_date"].unique())
    date_span = (max(unique_dates) - min(unique_dates)).days + 1 if len(unique_dates) >= 2 else 1
    has_start   = "Start_Time" in df.columns and df["Start_Time"].notna().sum() > 0
    has_status  = "Status" in df.columns
    has_sub_app = "Sub_Application" in df.columns and (df["Sub_Application"] != "UNKNOWN").any()
    ok_count    = int((df["Status"] == "OK").sum()) if has_status else 0
    fail_count  = int((df["Status"] == "FAILED").sum()) if has_status else 0

    # Confidence score (0-100): penalise missing columns, short date range, etc.
    conf = 100.0
    if not has_end_time:
        conf -= 15   # can't compute elapsed window
    if not has_status:
        conf -= 10   # can't distinguish OK vs FAILED
    if not has_sub_app:
        conf -= 5    # no sub-application grouping
    if date_span < 7:
        conf -= 20   # less than 7 days of data
    elif date_span < 14:
        conf -= 10
    elif date_span < 30:
        conf -= 5
    if len(df) < 50:
        conf -= 10
    conf = max(0.0, round(conf, 1))

    return {
        "daily":            daily,
        "monthly":          monthly,
        "window":           window,
        "compliance":       float(round(job_sla_comp, 1)),
        "job_sla_compliance":      float(round(job_sla_comp, 1)),
        "batch_window_compliance": float(round(batch_window_comp, 1)),
        "window_breach_days":      window_breach_days,
        "window_total_days":       n_window_days,
        # Canonical (sub_app, date)-granular window compliance from the shared engine
        "window_compliance":       window_compliance,
        "total_jobs":       t_jobs,
        "jobs_ok":          int(j_ok),
        "jobs_breach":      int(j_breach),
        "jobs_at_risk":     int(j_at_risk),
        "total_runs":       int(len(df)),
        "total_hrs":        float(round(df["run_time_hrs"].sum(), 2)),
        "sub_stats":        sub,
        "top_jobs":         top_jobs,
        "anomalies":        anomalies,
        "fleet_sla_buffer": fleet_sla_buffer,
        # ── Intelligence fields ──────────────────────────────────
        "worst_job_name":   worst_job_name,
        "worst_job_peak":   round(worst_job_peak, 3),
        "elapsed_available": elapsed_available,
        # Headline elapsed-window KPI (DAILY/UNKNOWN sub_apps only — separate from
        # the bar chart which shows max across ALL in-scope sub_apps per date).
        "elapsed_window_kpi": {
            "worst_hrs":  round(worst_elapsed_kpi, 3),
            "avg_hrs":    round(avg_elapsed_kpi, 3),
            "worst_date": worst_elapsed_date,
        },
        "date_span_days":   date_span,
        "date_range":       [str(unique_dates[0]), str(unique_dates[-1])] if unique_dates else [],
        "ok_runs":          ok_count,
        "fail_runs":        fail_count,
        "sla_source":       sla_src_type,
        "sla_daily_hrs":    global_ceil,
        "sla_ceiling":      global_ceil,   # canonical name for downstream callers
        "confidence":       conf,
        # ── Auto-detected SLA schedule mode (exposed for smart defaults) ────
        "sla_detected_mode": _detect_sla_mode(df),
        # ── SLA index for downstream (e.g. _build_sla_source_payload) ───────
        "_sla_index":       sla_index,
        # ── Retry storm warnings (job failure→cascade retries, NOT cyclic) ──
        "_retry_storms":    _retry_storms,
        # Sub_Apps excluded from compliance scope (CYCLIC/OUTBOUND/CALENDAR_BASED/etc.)
        "excluded_sub_apps": [
            {"sub_app": sa, "reason": info.get("reason","?"),
             "auto": info.get("auto", True),
             "job_count": info.get("job_count", 0),
             "peak_hrs": info.get("peak_hrs", 0.0)}
            for sa, info in _excl_sub_reasons.items()
        ],
    }


# ─────────────────────────────────────────────────────────────────
# Heatmap builders
# ─────────────────────────────────────────────────────────────────

def _build_sla_heatmap(daily_df: pd.DataFrame, ceiling: float | None = None) -> Dict[str, Any]:
    """Job × Date SLA compliance matrix — mirrors app_v2::heatmap_fig.

    Returns:
        { jobs: [...], dates: [...], cells: [{job, date, hrs, breach}, ...] }
    """
    if daily_df is None or daily_df.empty:
        return {"jobs": [], "dates": [], "cells": []}

    lim = ceiling if ceiling and ceiling > 0 else pe_config.SLA_DAILY_HRS
    dates = sorted(daily_df["run_date"].unique())[-21:]   # last 21 days
    all_jobs = sorted(daily_df["Job_Name"].unique())
    if len(all_jobs) > 40:
        all_jobs = (
            daily_df.groupby("Job_Name")["total_hrs"].sum()
            .nlargest(40).index.tolist()
        )

    cells = []
    for job in all_jobs:
        for d in dates:
            sub = daily_df[(daily_df["Job_Name"] == job) & (daily_df["run_date"] == d)]
            if sub.empty:
                cells.append({"job": job, "date": str(d), "hrs": None, "breach": False})
            else:
                h = float(sub.iloc[0]["total_hrs"])
                cells.append({"job": job, "date": str(d), "hrs": round(h, 2),
                               "breach": h > lim})

    return {
        "jobs":  all_jobs,
        "dates": [str(d) for d in dates],
        "cells": cells,
        "limit": lim,
    }


def _build_hour_heatmap(df: pd.DataFrame) -> Dict[str, Any]:
    """Hour-of-day × Sub-Application execution density heatmap.

    X axis: Hour 0-23 | Y axis: top sub-apps | value: job count

    Returns:
        { sub_apps: [...], hours: [0..23], cells: [{sub_app, hour, count, total_hrs}, ...] }
    """
    if df is None or df.empty or "Hour_Bucket" not in df.columns:
        return {"sub_apps": [], "hours": list(range(24)), "cells": []}

    group_col = "Sub_Application" if "Sub_Application" in df.columns else (
                "Folder" if "Folder" in df.columns else None)
    if group_col is None:
        return {"sub_apps": [], "hours": list(range(24)), "cells": []}

    # Top 10 sub-apps by job count
    top_apps = (df.groupby(group_col)["Job_Name"].count()
                  .nlargest(10).index.tolist())
    df_f = df[df[group_col].isin(top_apps)].copy()

    pivot = (df_f.groupby([group_col, "Hour_Bucket"])
               .agg(count=("Job_Name", "count"),
                    total_hrs=("run_time_hrs", "sum"))
               .reset_index()
               .rename(columns={group_col: "sub_app", "Hour_Bucket": "hour"}))

    pivot["total_hrs"] = pivot["total_hrs"].round(2)
    cells = pivot.to_dict(orient="records")

    return {
        "sub_apps": top_apps,
        "hours":    list(range(24)),
        "cells":    cells,
    }


# ─────────────────────────────────────────────────────────────────
# Hourly counts — temporal data for executive JRTOS
# ─────────────────────────────────────────────────────────────────
def _build_hourly_counts(df: pd.DataFrame) -> Dict[str, Dict[int, int]]:
    """Aggregate job count and fail count per hour bucket (0-23) for temporal."""
    if df is None or df.empty or "Hour_Bucket" not in df.columns:
        return {"hourly_jobs": {}, "hourly_fails": {}}

    hourly_jobs = df.groupby("Hour_Bucket").size().to_dict()

    if "Status" in df.columns:
        fail_df = df[df["Status"] == "FAILED"]
        hourly_fails = fail_df.groupby("Hour_Bucket").size().to_dict() if not fail_df.empty else {}
    else:
        hourly_fails = {}

    # Ensure all hours 0-23 present
    for h in range(24):
        hourly_jobs.setdefault(h, 0)
        hourly_fails.setdefault(h, 0)

    return {"hourly_jobs": hourly_jobs, "hourly_fails": hourly_fails}


# ─────────────────────────────────────────────────────────────────
# build_batch_payload — JSON-ready envelope for the frontend
# ─────────────────────────────────────────────────────────────────
def _build_worst_job(m: dict, top_jobs_df: "pd.DataFrame") -> dict:
    """Return worst-job section using per-job SLA from top_jobs_df.

    Falls back to global sla_ceiling when top_jobs_df is empty or sla_hrs
    column is absent (ensures backward compatibility).
    """
    job_name  = m.get("worst_job_name", "")
    peak_hrs  = m.get("worst_job_peak", 0.0)
    # Try to pull per-job SLA from the top_jobs dataframe
    per_job_sla = m.get("sla_ceiling")   # default: global ceiling
    if not top_jobs_df.empty and "sla_hrs" in top_jobs_df.columns and job_name:
        match = top_jobs_df[top_jobs_df["Job_Name"] == job_name]
        if not match.empty:
            val = float(match.iloc[0]["sla_hrs"])
            if val > 0:
                per_job_sla = val

    buffer_pct = (
        round(((per_job_sla - peak_hrs) / per_job_sla * 100), 1)
        if per_job_sla and per_job_sla > 0 else 0.0
    )
    return {
        "job_name":   job_name,
        "peak_hrs":   peak_hrs,
        "sla_hrs":    per_job_sla,
        "buffer_pct": buffer_pct,
        "note": "Worst-job peak = single highest OK-run duration across all jobs.",
    }


def build_batch_payload(df: pd.DataFrame) -> Dict[str, Any]:
    """Run compute_metrics and flatten all DataFrames → JSON-ready dicts.

    The Phase 3 contract requires:
        - KPIs  (compliance %, total runs, SLA buffers)
        - Top 10 breaching jobs
        - Time-series data for the charts
    """
    if df is None or df.empty:
        return {
            "kpis": {
                "compliance_pct": 0.0,
                "total_runs":     0,
                "total_jobs":     0,
                "total_hrs":      0.0,
                "jobs_breach":    0,
                "jobs_at_risk":   0,
                "jobs_ok":        0,
                "daily_limit_hrs":   pe_config.SLA_DAILY_HRS,
                "monthly_limit_hrs": pe_config.SLA_MONTHLY_HRS,
                "fleet_sla_buffer":  None,
            },
            "top_jobs":      [],
            "top_breaches":  [],
            "window":        [],
            "sub_stats":     [],
            "anomalies":     [],
            "hourly_counts": {"hourly_jobs": {}, "hourly_fails": {}},
            "sla_heatmap":   {"jobs": [], "dates": [], "cells": [], "limit": pe_config.SLA_DAILY_HRS},
            "hour_heatmap":  {"sub_apps": [], "hours": list(range(24)), "cells": []},
        }

    m = compute_metrics(df)
    top_jobs_df: pd.DataFrame = m["top_jobs"]
    window_df:   pd.DataFrame = m["window"]
    sub_df:      pd.DataFrame = m["sub_stats"]

    # Addition 4 — Multi-application-per-folder detection
    # When a single Folder contains 2+ distinct Application values, each
    # Application should be analyzed independently for window compliance.
    # Surface this as a warning in the payload so the analyst can split if needed.
    _multi_app_folders: list = []
    if "Folder" in df.columns and "Application" in df.columns:
        _folder_apps = (
            df.dropna(subset=["Folder", "Application"])
              .groupby("Folder")["Application"]
              .nunique()
        )
        _split_folders = _folder_apps[_folder_apps >= 2].index.tolist()
        for _fld in _split_folders:
            _apps = sorted(df[df["Folder"] == _fld]["Application"].dropna().unique().tolist())
            _multi_app_folders.append({
                "folder":       _fld,
                "applications": _apps,
                "app_count":    len(_apps),
                "note": (
                    f"Folder '{_fld}' contains {len(_apps)} distinct Applications "
                    f"({', '.join(str(a) for a in _apps[:5])}). "
                    "Window compliance should be measured per Application, not per Folder. "
                    "Consider splitting the Ctrl-M export by Application for accurate analysis."
                ),
            })
        if _multi_app_folders:
            logger.info(
                "build_batch_payload: %d multi-app folder(s) detected: %s",
                len(_multi_app_folders),
                [f["folder"] for f in _multi_app_folders],
            )
    # Inject into metrics dict so _build_data_warnings can surface them
    m["_multi_app_folders"] = _multi_app_folders

    # Top 10 breaching jobs (buffer < 0); fall back to worst 10 by peak if none breaching
    breaches_df = top_jobs_df[top_jobs_df["buffer_pct"] < 0].head(10)
    if breaches_df.empty:
        breaches_df = top_jobs_df.head(10)

    # Top 15 jobs by peak (used by the horizontal bar chart)
    top15_df = top_jobs_df.head(15).copy()

    # Identify top contributing job for each day (for chart annotation)
    daily_df = m["daily"]
    top_job_per_day = {}
    if daily_df is not None and not daily_df.empty:
        try:
            idx = daily_df.groupby("run_date")["total_hrs"].idxmax()
            for d, i in idx.items():
                row = daily_df.loc[i]
                top_job_per_day[str(d)] = str(row.get("Job_Name", "?"))
        except Exception:
            pass

    # Serialize the daily time-series window
    window_records = []
    elapsed_avail_local = bool(m.get("elapsed_available"))
    # Per-day failure count for chart overlay (failed ✕ marker)
    fail_by_date: dict = {}
    if "Status" in df.columns:
        fail_series = df[df["Status"] == "FAILED"].groupby("run_date").size()
        fail_by_date = {str(d): int(n) for d, n in fail_series.items()}
    for _, r in window_df.iterrows():
        date_str = str(r["run_date"])
        elapsed_hrs = round(float(r.get("elapsed_hrs", 0)), 3)
        total_hrs   = round(float(r["total_hrs"]), 3)
        # G14: use the pre-computed per-sub_app breach flag from compute_metrics()
        # (which uses window_agg's per-SLA ceiling) rather than recomputing with
        # the global ceiling here (which caused false breaches for WEEKLY sub-apps
        # when the global ceiling was dominated by DAILY thresholds).
        if "breach" in r.index:
            is_breach = bool(r["breach"])
        elif elapsed_avail_local:
            is_breach = elapsed_hrs > m["sla_ceiling"]
        else:
            is_breach = total_hrs   > m["sla_ceiling"]
        window_records.append({
            "run_date":     date_str,
            "total_hrs":    total_hrs,
            "elapsed_hrs":  elapsed_hrs,
            "job_count":    int(r["job_count"]),
            "breach":       bool(is_breach),
            "top_job":      top_job_per_day.get(date_str, ""),
            "has_failures": fail_by_date.get(date_str, 0) > 0,
            "fail_count":   fail_by_date.get(date_str, 0),
        })

    # RULE 6 — include Sub_Application so findings engine can use composite key
    # sla_hrs / sla_source must be included so the frontend can show per-job ceiling
    # is_utility must be included so the frontend utility-exclusion toggle works
    _job_cols = [c for c in ["Sub_Application", "Job_Name", "peak_hrs", "avg_hrs",
                              "total_hrs", "sla_hrs", "sla_source",
                              "buffer_pct", "sla_used_pct", "buffer_status",
                              "fail_count", "is_utility", "utility_reason"]
                 if c in top_jobs_df.columns]

    return {
        "kpis": {
            "compliance_pct":             m["compliance"],
            "job_sla_compliance_pct":      m["job_sla_compliance"],
            # Headline window compliance = shared-engine (sub_app, date) value so it
            # matches the SLA Matrix tab exactly. Falls back to the day-level number
            # (carried inside window_compliance) when no elapsed window is available.
            "window_compliance_pct":       (m.get("window_compliance") or {}).get(
                                               "compliance_pct", m["batch_window_compliance"]),
            "job_sla_compliance":          m["job_sla_compliance"],
            "batch_window_compliance":     m["batch_window_compliance"],
            "window_breach_days":      m["window_breach_days"],
            "window_total_days":       m["window_total_days"],
            # Canonical (sub_app, date)-granular window compliance (shared engine).
            # Both Batch Review and SLA Matrix read the same definition.
            "window_compliance":           m.get("window_compliance"),
            "total_runs":         m["total_runs"],
            "total_jobs":         m["total_jobs"],
            "total_hrs":          m["total_hrs"],
            "jobs_breach":        m["jobs_breach"],
            "jobs_at_risk":       m["jobs_at_risk"],
            "jobs_ok":            m["jobs_ok"],
            # Execution-status counters (ENDED OK vs ENDED NOT OK)
            "ok_runs":            int(m.get("ok_runs", 0)),
            "failed_runs":        int(m.get("fail_runs", 0)),
            "fail_rate_pct":      round(
                (m.get("fail_runs", 0) / m["total_runs"] * 100.0)
                if m.get("total_runs") else 0.0, 2,
            ),
            "daily_limit_hrs":    m["sla_ceiling"],
            "monthly_limit_hrs":  pe_config.SLA_MONTHLY_HRS,
            "fleet_sla_buffer":   m["fleet_sla_buffer"],
            # Window-level SLA buffer (whole nightly batch window vs SLA ceiling).
            # Preferred headline gauge metric; None when End_Time is unavailable.
            "window_sla_buffer":  _build_window_sla_buffer(m),
            # Auto-detected schedule mode — lets sla_matrix default to same mode
            "sla_detected_mode":  m.get("sla_detected_mode", "DAILY"),
        },
        # ── Separated analysis layers ────────────────────────────
        "elapsed_window": {
            "available":    m["elapsed_available"],
            # Headline = DAILY-only worst/avg (KPI). Falls back to chart-derived
            # worst day when the DAILY-only KPI is unavailable.
            "worst_day": (
                {"run_date": (m.get("elapsed_window_kpi") or {}).get("worst_date", ""),
                 "elapsed_hrs": (m.get("elapsed_window_kpi") or {}).get("worst_hrs", 0.0)}
                if m["elapsed_available"] and (m.get("elapsed_window_kpi") or {}).get("worst_hrs", 0) > 0
                else _worst_elapsed(window_records)
            ),
            "avg_elapsed_hrs": (
                round((m.get("elapsed_window_kpi") or {}).get("avg_hrs", 0.0), 3)
                if m["elapsed_available"] and (m.get("elapsed_window_kpi") or {}).get("worst_hrs", 0) > 0
                else (round(
                    sum(w["elapsed_hrs"] for w in window_records) / max(len(window_records), 1), 3
                ) if m["elapsed_available"] else None)
            ),
            "note": (
                "Elapsed window = wall-clock from first Start_Time to last End_Time "
                "per (Sub_Application, day). Headline shows DAILY sub_apps; the chart "
                "shows the worst sub_app window per day."
                if m["elapsed_available"]
                else "End_Time column missing — elapsed window cannot be computed. "
                     "Showing summed runtime only."
            ),
        },
        "summed_runtime": {
            "total_hrs":    m["total_hrs"],
            "worst_day_hrs": round(max((w["total_hrs"] for w in window_records), default=0.0), 3),
            "avg_day_hrs":  round(
                sum(w["total_hrs"] for w in window_records) / max(len(window_records), 1), 3
            ),
            "note": "Summed runtime = sum of all individual job Run_Sec values per day.",
        },
        "worst_job": _build_worst_job(m, top_jobs_df),
        # ── SLA source metadata ──────────────────────────────────
        "sla_source": _build_sla_source_payload(m),
        # ── Data coverage & confidence ───────────────────────────
        "data_coverage": {
            "date_span_days":   m["date_span_days"],
            "date_range":       m["date_range"],
            "total_runs":       m["total_runs"],
            "ok_runs":          m["ok_runs"],
            "fail_runs":        m["fail_runs"],
            "has_end_time":     m["elapsed_available"],
            "has_status":       m["ok_runs"] + m["fail_runs"] > 0,
            "has_sub_app":      any(s.get("Sub_Application", "UNKNOWN") != "UNKNOWN"
                                   for s in sub_df.to_dict(orient="records")),
            "confidence":       m["confidence"],
            "confidence_label": (
                "HIGH" if m["confidence"] >= 80 else
                "MEDIUM" if m["confidence"] >= 60 else
                "LOW" if m["confidence"] >= 40 else "INSUFFICIENT"
            ),
            "warnings":         _build_data_warnings(m),
        },
        "multi_app_folders":  _multi_app_folders,
        "excluded_sub_apps":  m.get("excluded_sub_apps", []),
        "top_jobs":     top15_df[_job_cols].to_dict(orient="records"),
        "top_breaches": breaches_df[_job_cols].to_dict(orient="records"),
        "window":       window_records,
        "sub_stats":    sub_df.round({"total_hrs": 2}).to_dict(orient="records"),
        "anomalies":    m["anomalies"],
        "hourly_counts": _build_hourly_counts(df),
        "sla_heatmap":  _build_sla_heatmap(m["daily"], ceiling=m.get("sla_ceiling")),
        "hour_heatmap": _build_hour_heatmap(df),
        "daily_jobs":   _build_daily_jobs(df),
    }


def _build_daily_jobs(df: pd.DataFrame) -> Dict[str, list]:
    """Build per-day list of {job, start_hr, end_hr} for the concurrency
    timeline (Gantt) chart. start_hr / end_hr are decimal hours-of-day
    (e.g. 21.5 = 9:30 PM). End_Time is required — when missing, we fall
    back to start_hr + Run_Sec/3600.

    Returns: { "YYYY-MM-DD": [ {job, start_hr, end_hr}, ... ] }
    """
    if df is None or df.empty or "Start_Time" not in df.columns:
        return {}
    work = df.copy()
    work["Start_Time"] = pd.to_datetime(work["Start_Time"], errors="coerce")
    work = work.dropna(subset=["Start_Time"])
    if work.empty:
        return {}
    has_end = "End_Time" in work.columns
    if has_end:
        work["End_Time"] = pd.to_datetime(work["End_Time"], errors="coerce")

    out: Dict[str, list] = {}
    for _, r in work.iterrows():
        start = r["Start_Time"]
        if pd.isna(start):
            continue
        start_hr = start.hour + start.minute / 60.0 + start.second / 3600.0
        end = r.get("End_Time") if has_end else None
        if pd.isna(end) or end is None:
            run_sec = float(r.get("Run_Sec") or 0)
            end_hr = start_hr + run_sec / 3600.0
        else:
            # If job spans midnight, project end to start_hr + duration so the
            # Gantt bar stays on a single timeline.
            duration = (end - start).total_seconds() / 3600.0
            end_hr = start_hr + max(duration, 0)
        date_key = str(start.date())
        out.setdefault(date_key, []).append({
            "job":      str(r.get("Job_Name") or "?"),
            "start_hr": round(start_hr, 2),
            "end_hr":   round(end_hr, 2),
        })
    # Cap each day at 60 jobs (the chart only displays top 15 anyway, server
    # already sorts; this keeps payload size bounded).
    for k in list(out.keys()):
        if len(out[k]) > 60:
            out[k] = sorted(out[k], key=lambda j: j["end_hr"], reverse=True)[:60]
    return out


def _worst_elapsed(window_records: list) -> dict | None:
    """Find the day with the highest elapsed window."""
    elapsed_days = [w for w in window_records if w.get("elapsed_hrs", 0) > 0]
    if not elapsed_days:
        return None
    worst = max(elapsed_days, key=lambda w: w["elapsed_hrs"])
    return {"run_date": worst["run_date"], "elapsed_hrs": worst["elapsed_hrs"]}


def _build_window_sla_buffer(m: dict) -> dict | None:
    """Compute the batch-WINDOW SLA buffer from the sentinel-measured elapsed
    window (not the worst single job).

        buffer_pct = (SLA_ceiling − worst_day_elapsed) / SLA_ceiling × 100

    This is the headline gauge metric: it answers "how much head-room does the
    whole nightly batch window have against its SLA ceiling?" — which is the
    real customer-facing SLA, not a single job's peak.

    Returns None when no elapsed window is available (End_Time missing); callers
    then fall back to the worst-job ``fleet_sla_buffer``.
    """
    if not m.get("elapsed_available"):
        return None
    ewk = m.get("elapsed_window_kpi") or {}
    worst = float(ewk.get("worst_hrs", 0.0) or 0.0)
    avg   = float(ewk.get("avg_hrs",   0.0) or 0.0)
    if worst <= 0:
        return None
    ceil = float(m.get("sla_ceiling") or pe_config.SLA_DAILY_HRS)
    if ceil <= 0:
        return None

    buffer_hrs = round(ceil - worst, 3)
    buffer_pct = round((ceil - worst) / ceil * 100, 1)
    avg_buffer_pct = round((ceil - avg) / ceil * 100, 1) if avg > 0 else None

    if buffer_pct < 0:
        status = "BREACH"
    elif buffer_pct <= pe_config.SLA_ATRISK_PCT:
        status = "AT_RISK"
    elif buffer_pct <= pe_config.SLA_LONGJOB_PCT:
        status = "LONG_JOB"
    else:
        status = "HEALTHY"

    return {
        "buffer_hrs":        buffer_hrs,
        "buffer_pct":        buffer_pct,
        "avg_buffer_pct":    avg_buffer_pct,
        "worst_elapsed_hrs": round(worst, 3),
        "avg_elapsed_hrs":   round(avg, 3),
        "sla_ceiling_hrs":   round(ceil, 3),
        "worst_day":         ewk.get("worst_date", ""),
        "status":            status,
        "source":            "window_elapsed",
    }


def _build_data_warnings(m: dict) -> list:
    """Generate human-readable warnings about data quality issues."""
    warnings = []
    if not m["elapsed_available"]:
        warnings.append({
            "code": "NO_END_TIME",
            "text": "End_Time column missing — cannot compute real elapsed batch window. "
                    "Only summed job runtime is available.",
            "severity": "warning",
        })
    if m["date_span_days"] < 7:
        warnings.append({
            "code": "SHORT_HISTORY",
            "text": f"Only {m['date_span_days']} day(s) of batch data. "
                    "30+ days recommended for PE audit evidence.",
            "severity": "warning",
        })
    elif m["date_span_days"] < 30:
        warnings.append({
            "code": "PARTIAL_HISTORY",
            "text": f"{m['date_span_days']} days of data loaded. "
                    "30-day history recommended for full PE audit coverage.",
            "severity": "info",
        })
    if m["confidence"] < 60:
        warnings.append({
            "code": "LOW_CONFIDENCE",
            "text": f"Data confidence is {m['confidence']:.0f}% — "
                    "some metrics may not be fully reliable. Check source file completeness.",
            "severity": "warning",
        })
    if m["sla_source"] == "default":
        warnings.append({
            "code": "DEFAULT_SLA",
            "text": f"Using default SLA of {pe_config.SLA_DAILY_HRS:.1f}h. "
                    "Upload customer SLA matrix for accurate compliance measurement.",
            "severity": "info",
        })
    # Retry storm warnings — distinct from cyclic jobs
    for rs in m.get("_retry_storms", []):
        warnings.append({
            "code": "RETRY_STORM",
            "text": (
                f"Retry storm detected: {rs['sub_app']} had {rs['run_count']} runs on "
                f"{rs['date']}"
                + (f" (top job: {rs['job_name']})" if rs.get("job_name") else "")
                + " — job failure triggered automated retry cascade. "
                "Investigate root cause; this is not normal cyclic behaviour."
            ),
            "severity": "warning",
            "sub_app":  rs["sub_app"],
            "date":     rs["date"],
            "run_count": rs["run_count"],
        })
    # Multi-application-per-folder detection warnings
    for maf in m.get("_multi_app_folders", []):
        warnings.append({
            "code":       "MULTI_APP_FOLDER",
            "text":       maf["note"],
            "severity":   "info",
            "folder":     maf["folder"],
            "app_count":  maf["app_count"],
        })
    return warnings


def _build_sla_source_payload(m: dict) -> dict:
    """Build the SLA source metadata payload.

    Reads SLA intelligence from config_store AND the live sla_index produced
    by compute_metrics → build_sla_index → resolve_sla.  Emits match-quality
    statistics (how many jobs matched by exact / env-prefix / token-overlap /
    schedule fallback / assumed default) so the UI can show audit quality.
    """
    sla_index = m.get("_sla_index") or {}
    sla_type  = sla_index.get("source") or m.get("sla_source", "default")
    job_sla   = sla_index.get("job_sla", {})

    # Try to read rich SLA intelligence from config_store
    try:
        from services import config_store as _cs
        intel = _cs.get("_sla_intelligence")
    except Exception:
        intel = None

    base: dict = {
        "type":        sla_type,
        "daily_hrs":   m.get("sla_daily_hrs") or pe_config.SLA_DAILY_HRS,
        "weekly_hrs":  pe_config.SLA_WEEKLY_HRS,
        "monthly_hrs": pe_config.SLA_MONTHLY_HRS,
        "custom_hrs":  pe_config.SLA_CUSTOM_HRS,
    }

    # ── Match quality statistics ──────────────────────────────────────────────
    if job_sla:
        src_counts: dict = {}
        for entry in job_sla.values():
            s = entry.get("source", "default")
            src_counts[s] = src_counts.get(s, 0) + 1

        base["match_stats"] = {
            "total_jobs":      len(job_sla),
            "sla_matrix":      src_counts.get("sla_matrix", 0),
            "customer_fallback": src_counts.get("customer_fallback", 0),
            "assumed":         src_counts.get("assumed", 0),
            "default":         src_counts.get("default", 0),
        }
        # Per-job SLA resolution detail for audit trail (capped at 50 for payload size)
        base["job_sla_resolved"] = [
            {
                "job":        k,
                "sla_hrs":    v.get("sla_hrs"),
                "source":     v.get("source"),
                "confidence": v.get("confidence"),
                "detail":     v.get("detail"),
            }
            for k, v in list(job_sla.items())[:50]
        ]
    else:
        base["match_stats"] = {
            "total_jobs": 0, "sla_matrix": 0,
            "customer_fallback": 0, "assumed": 0, "default": 0,
        }
        base["job_sla_resolved"] = []

    if intel and isinstance(intel, dict):
        base["schema_type"]        = intel.get("schema_type", "unknown")
        base["detected_model"]     = intel.get("detected_model", "")
        base["valid_rows"]         = intel.get("valid_rows", 0)
        base["partial_rows"]       = intel.get("partial_rows", 0)
        base["total_rows"]         = intel.get("total_rows", 0)
        base["contracts"]          = intel.get("contracts", [])[:20]
        base["warnings"]           = intel.get("warnings", [])
        base["sections_detected"]  = intel.get("sections_detected", [])
        base["filename"]           = intel.get("filename", "")
        base["blocked"]            = intel.get("valid_rows", 0) == 0

        matched_jobs = base["match_stats"]["sla_matrix"]
        total_jobs   = base["match_stats"]["total_jobs"]
        assumed_jobs = base["match_stats"]["assumed"] + base["match_stats"]["default"]
        base["note"] = (
            f"SLA from '{intel.get('filename', '?')}' — "
            f"{intel.get('detected_model', 'unknown model')} · "
            f"{intel.get('valid_rows', 0)} contract rules · "
            f"{matched_jobs}/{total_jobs} jobs matched by name"
            + (f" ({assumed_jobs} using assumed defaults)" if assumed_jobs else "")
        )
    else:
        base["schema_type"]    = "none"
        base["detected_model"] = "Default system values"
        base["contracts"]      = []
        base["warnings"] = [{
            "code":     "NO_SLA_FILE",
            "text":     "No SLA matrix uploaded — compliance uses assumed defaults.",
            "severity": "critical" if sla_type == "default" else "info",
        }] if sla_type == "default" else []
        base["blocked"] = (sla_type == "default")
        base["note"] = (
            "Using default SLA windows from system configuration. "
            "Upload a customer SLA matrix to override."
            if sla_type == "default"
            else "Using customer-approved SLA windows from uploaded matrix."
        )

    return base
