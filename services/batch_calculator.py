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
from typing import Any, Dict, Optional

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
        # Stage 0: Excel serial date detection — numeric values > 40000 are Excel epoch days.
        # Excel epoch is 1899-12-30; a value of 40000 ≈ 2009-07-06; 45000 ≈ 2023-03-16.
        # Some Ctrl-M exports store timestamps as numeric serial+fraction (e.g. 45001.625).
        # Detect these BEFORE string normalization so they aren't corrupted by str coercion.
        _numeric_s = pd.to_numeric(s, errors="coerce")
        _serial_mask = _numeric_s.notna() & (_numeric_s > 40000) & (_numeric_s < 60000)
        if _serial_mask.any():
            # Convert Excel serial to datetime (origin="1899-12-30", unit="D")
            try:
                _excel_dt = pd.to_datetime(_numeric_s[_serial_mask],
                                           unit="D", origin="1899-12-30", errors="coerce")
                # Only apply if conversion looks sane (year 2000–2040)
                _valid = _excel_dt.notna() & (_excel_dt.dt.year >= 2000) & (_excel_dt.dt.year <= 2040)
                if _valid.any():
                    s = s.copy().astype(object)
                    s[_serial_mask & _valid.reindex(s.index, fill_value=False)] = \
                        _excel_dt[_valid].dt.strftime("%Y-%m-%d %H:%M:%S")
                    s = s.astype(str)
            except Exception:
                pass

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
        except ImportError as e:
            # pandas raises ImportError (not caught above) when the required Excel
            # engine (openpyxl/xlrd) isn't installed in THIS python's env — happens
            # when a customer's .venv was created/copied before dependencies were
            # fully installed. Give an actionable fix instead of a raw traceback.
            raise ValueError(
                f"Cannot parse Ctrl-M file: the '{engine}' package required to read "
                f"{ext} files is missing from this Python environment. "
                f"Re-run start.bat to auto-install missing dependencies, or run: "
                f"pip install {engine}. (Original error: {e})"
            ) from e
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
            except ImportError as e:
                raise ValueError(
                    "Cannot parse Ctrl-M file: the 'openpyxl' package required to read "
                    "Excel content is missing from this Python environment. "
                    "Re-run start.bat to auto-install missing dependencies, or run: "
                    f"pip install openpyxl. (Original error: {e})"
                ) from e
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
    # Snapshot the ORIGINAL header tokens before any rename/synthesis so the
    # content-shape classifier (below) can judge the file on its real columns.
    _raw_cols_lower = [str(c).lower().strip() for c in df.columns]
    _n_raw_rows = len(df)
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

    # ── P2 #8: schema-completeness capability flags (captured pre-synthesis) ──
    # Record which analytical columns were GENUINELY present in the source, before
    # the graceful-degradation synthesis below fabricates defaults. Downstream can
    # read df.attrs["capabilities"] to disable only the affected metric instead of
    # silently presenting a default as if it were real customer data.
    _present = set(df.columns)
    _capabilities = {
        "has_job_name":          "Job_Name" in _present,
        "has_start_time":        "Start_Time" in _present,
        "has_end_time":          "End_Time" in _present,
        "has_sub_application":   "Sub_Application" in _present,
        "has_status":            "Status" in _present,
        "has_runtime":           "Run_Sec" in _present or "_duration_str" in _present,
        # Derived capability gates (what the pipeline can legitimately compute)
        "window_compliance":     "End_Time" in _present and "Start_Time" in _present,
        "per_sub_app_sla":       "Sub_Application" in _present,
        "failure_rate":          "Status" in _present,
    }
    try:
        df.attrs["capabilities"] = _capabilities
    except Exception:
        pass
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

    # ── Content-shape classifier: reject PRE-AGGREGATED summary/rollup files ──
    # A raw Ctrl-M export has one row per job *run*. Pre-aggregated rollups (daily
    # summaries, SLA-breach reports, correlation matrices) instead expose derived
    # output columns. These must NOT be analysed as job logs — judging a rollup
    # against per-job SLAs produces nonsense. Detect generically via header tokens.
    # Require ≥2 distinct aggregation signals to avoid false-positives on a raw
    # file that happens to carry a single "Total Runtime" column.
    _AGG_TOKENS = (
        "_breach", "sla_breach", "breach_count", "exceeded", "weekly_sla",
        "monthly_sla", "correlation", "avg_runtime", "average_runtime",
        "total_breach", "breaches", "pct_compliant", "compliance_pct",
        "p95", "p99", "stddev", "std_dev",
    )
    _agg_hits = sorted({
        tok for tok in _AGG_TOKENS
        for col in _raw_cols_lower
        if tok in col.replace(" ", "_").replace("-", "_")
    })
    # Only trip when the file ALSO lacks per-run job granularity: no real End_Time
    # mapped (rollups rarely carry both start AND end per row) AND no per-job
    # Run_Sec, OR the row count is implausibly small for a 30-day raw export.
    _has_real_runtime = "Run_Sec" in df.columns or "End_Time" in df.columns
    if len(_agg_hits) >= 2 and not _has_real_runtime:
        raise ValueError(
            "This looks like a pre-aggregated summary/rollup, not a raw Ctrl-M "
            f"export (aggregation columns detected: {_agg_hits[:6]}). "
            "Upload the raw per-job-run report (one row per job execution with "
            "Job_Name + Start_Time), not a derived SLA/breach summary."
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
            "status": status,
            # GAP-4: tag source so UI knows this is a single-job peak, not the
            # whole-window elapsed metric (active when End_Time is unavailable).
            "source": "fleet_peak_fallback"}


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
# PATH C — Adaptive SLA from run history (no XLSX uploaded)
# ─────────────────────────────────────────────────────────────────
# Minimum avg runtime (5 min) below which adaptive SLA is unreliable.
# Sub-minute jobs are OS scheduler noise, not batch SLA signals.
_MIN_ADAPTIVE_AVG_HRS: float = 5.0 / 60.0


def _compute_adaptive_sla(df: pd.DataFrame, global_ceil: float) -> pd.DataFrame:
    """Compute per-job adaptive SLA baseline from Ctrl-M run history (PATH C).

    Used when no SLA XLSX is uploaded so every job gets a history-derived ceiling
    rather than a one-size-fits-all global default.

    Quality tiers (by OK run count per job):
      STRONG     (≥14 runs)  → p95
      MODERATE   (7-13 runs) → max(p90, avg + 2σ)
      WEAK       (3-6 runs)  → max(peak×0.90, avg + 2σ, avg + σ), floor = avg
      INSUFFICIENT (<3 runs) → peak (excluded from per-job compliance)
      SHORT_JOB  (avg < 5 min) → excluded (OS jitter, not batch signal)

    Returns a DataFrame indexed by composite key suitable for merging into
    top_jobs_df.  Columns: sla_hrs, baseline_quality, is_high_variance.
    """
    import math as _math_ap

    has_status = "Status" in df.columns
    ok_df = df[df["Status"] == "OK"].copy() if has_status else df.copy()
    if ok_df.empty:
        ok_df = df.copy()

    group_cols = (["Sub_Application", "Job_Name"]
                  if "Sub_Application" in df.columns else ["Job_Name"])

    rows = []
    for key, grp in ok_df.groupby(group_cols)["run_time_hrs"]:
        hrs = grp.dropna()
        n = len(hrs)
        if n == 0:
            continue
        avg = float(hrs.mean())
        std = float(hrs.std(ddof=1)) if n > 1 else 0.0

        # SHORT_JOB: average < 5 min → noise, exclude from adaptive compliance
        if avg < _MIN_ADAPTIVE_AVG_HRS:
            quality = "SHORT_JOB"
            sla_hrs = None
        elif n >= 14:
            quality = "STRONG"
            sla_hrs = float(hrs.quantile(0.95))
        elif n >= 7:
            quality = "MODERATE"
            sla_hrs = max(float(hrs.quantile(0.90)), avg + 2 * std, avg)
        elif n >= 3:
            quality = "WEAK"
            peak = float(hrs.max())
            sla_hrs = max(peak * 0.90, avg + 2 * std, avg + std, avg)
        else:
            quality = "INSUFFICIENT"
            sla_hrs = float(hrs.max())  # best estimate, but excluded from compliance

        if sla_hrs is not None:
            # Cap at global_ceil — adaptive SLA must not exceed the schedule window
            sla_hrs = min(sla_hrs, global_ceil)
            sla_hrs = round(sla_hrs, 4)

        # High-variance flag: CV > 0.5 AND σ > 15 min AND n ≥ 5
        is_high_variance = (
            n >= 5 and avg > 0
            and (std / avg) > 0.5
            and std > 0.25
        )

        _key = key if isinstance(key, tuple) else (key,)
        rows.append({
            **dict(zip(group_cols, _key)),
            "sla_hrs":         sla_hrs,
            "baseline_quality": quality,
            "is_high_variance": is_high_variance,
        })

    if not rows:
        return pd.DataFrame(columns=group_cols + ["sla_hrs", "baseline_quality", "is_high_variance"])

    return pd.DataFrame(rows).set_index(group_cols)


# ─────────────────────────────────────────────────────────────────
# Utility job exclusion helper — generic, signal-based
# ─────────────────────────────────────────────────────────────────
_UTILITY_RUNTIME_MAX_BASIS: frozenset[str] = frozenset({
    "_fw", "fw_",
    "batch_start", "batch_end", "batchstart", "batchend",
    "pre_batch_node", "post_batch_node", "qwbatchstart", "qwbatchend",
    "seq_disable_login", "seq_enable_users",
    "disable_users", "enable_users",
    "disable_login", "enable_login",
    "zabbix_monitors",
})


def _normalize_job_name(job_name: str) -> str:
    return re.sub(r"[\s\-]+", "_", str(job_name)).lower().strip()


def is_utility_job(
    job_name: str,
    avg_runtime_hrs: float,
    max_runtime_hrs: float,
) -> tuple[bool, str]:
    """
    Return (is_utility, reason_string) for a single job name.

    Strong utility tokens are name-only exclusions. Runtime-gated patterns require
    both a name match and a runtime threshold check.
    """
    n = _normalize_job_name(job_name)

    _strong_tokens = sorted(
        {str(t).strip().lower() for t in getattr(pe_config, "STRONG_UTILITY_TOKENS", set()) if str(t).strip()},
        key=lambda s: (-len(s), s),
    )
    for token in _strong_tokens:
        if token and token in n:
            return True, f"strong_utility:{token}"

    matched_fail_reason = ""
    for pattern, threshold in getattr(pe_config, "RUNTIME_GATED_UTILITY", {}).items():
        if not pattern or pattern not in n:
            continue

        check_val = max_runtime_hrs if pattern in _UTILITY_RUNTIME_MAX_BASIS else avg_runtime_hrs
        try:
            check_val_f = float(check_val)
        except (TypeError, ValueError):
            check_val_f = float("nan")

        if pd.notna(check_val_f) and check_val_f < float(threshold):
            return True, f"runtime_gated:{pattern}({check_val_f:.4f}h<{float(threshold):.4f}h)"

        matched_fail_reason = (
            f"pattern_matched_not_excluded:{pattern}({check_val_f:.4f}h>={float(threshold):.4f}h)"
        )

    return False, matched_fail_reason


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

    # ── PATH C: Adaptive baseline when no XLSX contracts present ────────────
    # When every job resolved from global_ceil (no XLSX), compute per-job history
    # baselines so jobs get meaningful individual targets instead of one shared default.
    # PATH A/B (XLSX present) → skip entirely; contracted values take precedence.
    _sla_path = "A" if sla_index and sla_index.get("source") in ("sla_matrix", "batch_sla_xlsx") else "C"
    top_jobs["sla_path"] = _sla_path

    if _sla_path == "C":
        _adaptive_df = _compute_adaptive_sla(df, global_ceil)
        if not _adaptive_df.empty:
            _gc = group_cols if isinstance(group_cols, list) else [group_cols]
            _merge_cols = [c for c in _gc if c in top_jobs.columns]
            _top_reset = top_jobs.reset_index(drop=True)
            try:
                _top_reset = _top_reset.join(
                    _adaptive_df[["sla_hrs", "baseline_quality", "is_high_variance"]],
                    on=_merge_cols if len(_merge_cols) > 1 else _merge_cols[0],
                    how="left",
                    rsuffix="_adaptive",
                )
                # Apply adaptive SLA where available (not SHORT_JOB/INSUFFICIENT for compliance)
                _has_adapt = _top_reset.get("sla_hrs_adaptive", pd.Series(dtype=float)).notna()
                if _has_adapt.any():
                    _top_reset.loc[_has_adapt, "sla_hrs"] = _top_reset.loc[_has_adapt, "sla_hrs_adaptive"]
                    _top_reset.loc[_has_adapt, "sla_source"] = "adaptive"
                # Mark SHORT_JOB rows — buffer_pct will be set to None (excluded from compliance)
                if "baseline_quality" in _top_reset.columns:
                    _short_mask = _top_reset["baseline_quality"] == "SHORT_JOB"
                    _top_reset["is_short_job"] = _short_mask
                    _insuf_mask = _top_reset["baseline_quality"] == "INSUFFICIENT"
                    _top_reset.loc[_short_mask | _insuf_mask, "sla_hrs"] = global_ceil  # display only
                    # Trust fix: once sla_hrs is overwritten back to the flat
                    # global ceiling above, sla_source must say so too — leaving
                    # it as "adaptive" would label a flat-default display value
                    # as if it were a real history-derived number.
                    _top_reset.loc[_short_mask | _insuf_mask, "sla_source"] = "default"
                else:
                    _top_reset["is_short_job"] = False
                # Carry is_high_variance column
                if "is_high_variance" not in _top_reset.columns:
                    _top_reset["is_high_variance"] = False
                # Drop temp adaptive sla column
                _top_reset.drop(columns=["sla_hrs_adaptive"], inplace=True, errors="ignore")
                top_jobs = _top_reset
            except Exception as _adap_err:
                logger.warning("build_top_jobs_df: adaptive SLA merge failed — %s", _adap_err)
        else:
            top_jobs["is_short_job"] = False
            top_jobs["is_high_variance"] = False
            top_jobs["baseline_quality"] = "INSUFFICIENT"
    else:
        top_jobs["is_short_job"] = False
        top_jobs["is_high_variance"] = False
        top_jobs["baseline_quality"] = "CONTRACTED"

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
    # GAP-6: also guard against entirely-absent sla_hrs column (all NaN after replace).
    # Use np.where so the entire column is vectorised; NaN propagates correctly downstream.
    _sla_safe = top_jobs["sla_hrs"].replace(0, float("nan"))
    # Coerce to numeric in case any SlaContract reconstruction silently left strings
    _sla_safe = pd.to_numeric(_sla_safe, errors="coerce")
    _nan_ratio = float(_sla_safe.isna().mean())
    if _nan_ratio > 0.5:
        logger.warning(
            "build_top_jobs_df: %.0f%% of jobs have NaN/0 sla_hrs — "
            "SLA contracts may not have loaded correctly.  "
            "Falling back to global_ceil for NaN rows so gauge and table stay in sync.",
            _nan_ratio * 100,
        )
        _global_ceil_for_nan = float(
            sla_index.get("global_ceiling", 0) if sla_index else 0
        ) or float(getattr(pe_config, "SLA_DAILY_HRS", 6.0))
        _sla_safe = _sla_safe.fillna(_global_ceil_for_nan)
    top_jobs["buffer_pct"]   = ((_sla_safe - top_jobs["peak_hrs"]) / _sla_safe * 100).round(1)
    top_jobs["sla_used_pct"] = (top_jobs["peak_hrs"] / _sla_safe * 100).round(1)

    # PATH C: SHORT_JOB and INSUFFICIENT quality → set buffer_pct to None so they
    # are excluded from the per-job compliance denominator (avoid noise-driven false positives).
    if "is_short_job" in top_jobs.columns:
        _excl_mask = top_jobs["is_short_job"].fillna(False)
        if "baseline_quality" in top_jobs.columns:
            _excl_mask = _excl_mask | (top_jobs["baseline_quality"] == "INSUFFICIENT")
        top_jobs.loc[_excl_mask, "buffer_pct"]   = None
        top_jobs.loc[_excl_mask, "sla_used_pct"] = None
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

    # ── Tag utility jobs using the two-step generic exclusion algorithm ──────
    if "Job_Name" in top_jobs.columns:
        _util_results = top_jobs.apply(
            lambda r: is_utility_job(
                str(r.get("Job_Name", "")),
                float(r.get("avg_hrs", 0.0) or 0.0),
                float(r.get("peak_hrs", 0.0) or 0.0),
            ),
            axis=1,
        )
        top_jobs["is_utility"] = _util_results.apply(lambda t: bool(t[0]))
        top_jobs["utility_reason"] = _util_results.apply(lambda t: str(t[1]) if t[1] else "")
    else:
        top_jobs["is_utility"] = False
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
        dominant = "DAILY"

    ceiling_map: Dict[str, float] = {
        "DAILY":   pe_config.SLA_DAILY_HRS,
        "WEEKLY":  pe_config.SLA_WEEKLY_HRS,
        "MONTHLY": pe_config.SLA_MONTHLY_HRS,
    }
    base = ceiling_map.get(dominant, pe_config.SLA_DAILY_HRS)

    # CHANGE 3: prefer stored SLA-intelligence ceilings (set when matrix uploaded)
    # over pe_config defaults — this fixes the 6.0h vs 8.8h gauge discrepancy.
    try:
        from services import config_store as _cs_dc
        _intel = _cs_dc.get("_sla_intelligence") or {}
        _ceilings = _intel.get("ceilings") or {}
        _matrix_val = _ceilings.get(dominant) or _ceilings.get("DAILY")
        if _matrix_val and float(_matrix_val) > 0:
            return float(_matrix_val)
        # Fall back to SOW windows when no SLA matrix
        _sow = _cs_dc.get("_sow_sla_windows") or {}
        _sow_entry = _sow.get(dominant, {})
        _sow_val = _sow_entry.get("limit_hours") if isinstance(_sow_entry, dict) else _sow_entry
        if _sow_val and float(_sow_val) > 0:
            return float(_sow_val)
    except Exception:
        pass

    return base


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
    from services.sla_engine import resolve_sla, classify_schedule, SlaContract, ResolvedSla

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
        intel = {}

    raw_contracts = intel.get("contracts", [])
    raw_ceilings  = intel.get("ceilings", {})

    # ── Tier-2 fallback: BatchSLA_info.xlsx workflows (set by /api/batch-sla/upload) ──
    # When no full SLA-intelligence run happened, the simpler BatchSLA XLSX upload
    # stores parsed {workflow, sla_hours, schedule} rows under '_batch_sla_xlsx'.
    # Convert those to contract dicts so per-job resolution still works.
    _from_batch_sla_xlsx = False
    if not raw_contracts:
        try:
            _bsla = _cs.get("_batch_sla_xlsx") or {}
        except Exception:
            _bsla = {}
        _wfs = _bsla.get("workflows") or []
        raw_contracts = []
        for _w in _wfs:
            if not isinstance(_w, dict):
                continue
            _name = str(_w.get("workflow") or _w.get("job_name") or "").strip()
            _hrs_raw = _w.get("sla_hours", _w.get("sla_hrs"))
            try:
                _hrs = float(_hrs_raw) if _hrs_raw is not None else 0.0
            except (TypeError, ValueError):
                _hrs = 0.0
            if not _name or _hrs <= 0:
                continue
            _sched_raw = str(_w.get("schedule") or "")
            try:
                _stype = classify_schedule(_sched_raw) if _sched_raw else "UNKNOWN"
                if _stype in ("UNKNOWN", ""):
                    _stype = classify_schedule(_name)
            except Exception:
                _stype = "UNKNOWN"
            raw_contracts.append({
                "batch_name":       _name,
                "schedule_type":    _stype or "UNKNOWN",
                "schedule_raw":     _sched_raw,
                "sla_model":        "DURATION",
                "sla_duration_hrs": _hrs,
                "sla_window_hrs":   _hrs,
                "sla_source_type":  "JOB_SPECIFIC",
                "completeness":     "complete",
                "source_row":       int(_w.get("row", 0) or 0),
            })
        if raw_contracts:
            _from_batch_sla_xlsx = True
            logger.info(
                "build_sla_index: using %d workflow SLA(s) from _batch_sla_xlsx fallback",
                len(raw_contracts),
            )

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
        except Exception as _contract_err:
            # GAP-2: log skipped contracts so silent failures become visible in diagnostics
            logger.warning(
                "build_sla_index: skipped SlaContract row %d ('%s') — %s: %s",
                c.get("source_row", 0), c.get("batch_name", "?"),
                type(_contract_err).__name__, _contract_err,
            )
            continue

    # GAP-2: surface skip count in result for UI diagnostic
    _total_raw = len([c for c in raw_contracts if isinstance(c, dict)])
    _skip_count = _total_raw - len(contracts)
    if _skip_count > 0:
        logger.warning(
            "build_sla_index: %d/%d SlaContract(s) skipped due to reconstruction errors "
            "— these jobs will fall back to default SLA ceilings",
            _skip_count, _total_raw,
        )
    result["_contract_skip_count"] = _skip_count

    result["contracts"] = contracts
    result["ceilings"]  = {k: float(v) for k, v in raw_ceilings.items() if v}
    # Preserve source provenance: "batch_sla_xlsx" when from the simple XLSX upload,
    # "sla_matrix" when from the full SLA-intelligence run.
    # The frontend stale banner hides only when source == "batch_sla_xlsx" or "sla_matrix".
    result["source"]    = "batch_sla_xlsx" if _from_batch_sla_xlsx else "sla_matrix"

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

    # ── Canonical sub-app provenance (single source of truth) ────────────────
    # resolve_sla() matches individual JOB names; customer SLA workbooks usually
    # carry WORKFLOW-tier contracts ("BY WEEKLY" governs every job under
    # TEST_2025_WEEKLY). build_ceiling_map_detailed() is the SAME matcher the
    # compliance math uses, so we consult it to honestly label a job whose
    # sub-app is governed by the customer matrix as "sla_matrix" instead of
    # mislabelling it "assumed" — and to give it the exact contracted ceiling
    # (6h daily, 7.5h seq, 9h weekly …) rather than a flat global default.
    _detail_ceiling: Dict[str, Dict[str, Any]] = {}
    if "Sub_Application" in df.columns:
        try:
            from services import compliance_engine as _ce_prov
            from services import config_store as _cs_prov
            _detail_ceiling = _ce_prov.build_ceiling_map_detailed(
                sub_applications=[
                    str(s) for s in df["Sub_Application"].dropna().unique()
                ],
                xlsx_config=_cs_prov.get("_batch_sla_xlsx") or None,
                pe_config_ref=pe_config,
            )
        except Exception:
            _detail_ceiling = {}

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

        # ── Workflow-tier override ───────────────────────────────────────────
        # When per-job matching produced no customer match (assumed/default) but
        # the sub-app IS governed by a customer SLA-matrix contract, adopt that
        # contract's ceiling + provenance. This is the binding workflow-tier SLA,
        # so labelling it "assumed" would be misleading.
        _det = _detail_ceiling.get(sub_app.upper()) if sub_app else None
        if (_det and _det.get("source") == "sla_matrix"
                and resolved.source not in ("sla_matrix", "customer_fallback")):
            _mt    = _det.get("match_type", "token")
            _score = float(_det.get("score") or 0.0)
            _conf  = "high" if (_mt == "token" and _score >= 1.0) else "medium"
            _pat   = _det.get("matched_pattern") or sub_app
            resolved = ResolvedSla(
                sla_hrs=float(_det["sla_hrs"]),
                sla_model="WINDOW",
                source="sla_matrix",
                source_detail=(
                    f"Resolved from customer SLA matrix workflow '{_pat}' "
                    f"(workflow tier · {_mt} match)"
                ),
                schedule_type=resolved.schedule_type or _det.get("schedule_type", "UNKNOWN"),
                confidence=_conf,
            )

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

    # Expose the per-sub_app resolved ceilings (workflow tier) so the SLA-source
    # payload can present the customer's *actual* contracted windows honestly
    # instead of a single global daily number that may not match any one job.
    if _detail_ceiling:
        result["sub_app_ceilings"] = {
            sa: {
                "sla_hrs":         d.get("sla_hrs"),
                "source":          d.get("source"),
                "schedule_type":   d.get("schedule_type"),
                "matched_pattern": d.get("matched_pattern"),
            }
            for sa, d in _detail_ceiling.items()
        }

    return result
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
# Window decomposition — busy-time (interval union), idle gaps, and
# batch-block detection.  The elapsed span (first-start → last-end)
# overstates real workload when a day's jobs run in separated clusters
# with long idle gaps between them.  These helpers recover the ACTIVE
# compute time so buffer is measured honestly against the SLA window.
# ─────────────────────────────────────────────────────────────────
def _merge_intervals(pairs, gap_seconds: float = 0.0) -> list:
    """Merge [start, end] timestamp pairs whose separation is <= gap_seconds.

    gap_seconds=0 → strict union (overlap/touch) = real busy time.
    gap_seconds=N → cluster intervals separated by <= N seconds into one block.
    Returns a list of merged [start, end] pairs sorted by start.
    """
    cleaned = [(s, e) for s, e in pairs if pd.notna(s) and pd.notna(e) and e >= s]
    if not cleaned:
        return []
    cleaned.sort(key=lambda p: p[0])
    merged = [[cleaned[0][0], cleaned[0][1]]]
    for s, e in cleaned[1:]:
        if (s - merged[-1][1]).total_seconds() <= gap_seconds:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def _busy_and_blocks_for_day(starts, ends, block_gap_hrs: float):
    """Return (busy_hrs, blocks) for one day's runs.

    busy_hrs = total time covered by the UNION of all run intervals
               (overlapping/parallel jobs counted once) — the REAL active
               batch time, distinct from the first→last elapsed span.
    blocks   = clusters of runs separated by idle gaps > block_gap_hrs,
               each {start, end, span_hrs, runs}.  Lets morning/evening
               batch phases be reported separately instead of being
               stretched into one inflated window across the dead gap.
    """
    pairs = list(zip(starts, ends))
    union = _merge_intervals(pairs, gap_seconds=0.0)
    busy_hrs = sum((e - s).total_seconds() for s, e in union) / 3600.0
    block_iv = _merge_intervals(pairs, gap_seconds=max(block_gap_hrs, 0.0) * 3600.0)
    blocks = []
    for s, e in block_iv:
        span = (e - s).total_seconds() / 3600.0
        runs = sum(1 for ps, _pe in pairs if pd.notna(ps) and s <= ps <= e)
        blocks.append({
            "start":    pd.Timestamp(s).strftime("%H:%M"),
            "end":      pd.Timestamp(e).strftime("%H:%M"),
            "span_hrs": round(span, 3),
            "runs":     int(runs),
        })
    return round(busy_hrs, 3), blocks


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

    # ── Canonical IN-SCOPE frame for the daily picture ───────────────────────
    # The daily time-series (jobs/day, batch elapsed/day) and the heatmaps must
    # reflect the SAME post-exclusion scope used by window compliance.  Without
    # this, the headline "jobs per day" (built from df_analysis) silently counts
    # MONTHLY/OUTBOUND/cyclic sub-apps that the compliance denominator already
    # dropped — so the review tells two contradicting stories on the same screen.
    # df_scope removes every out-of-scope sub_app (user-excluded ∪ cyclic ∪
    # MONTHLY/OUTBOUND/QUARTERLY/…), exactly the set used by window_agg above.
    if _out_of_scope_subs and "Sub_Application" in df_analysis.columns:
        df_scope = df_analysis[
            ~df_analysis["Sub_Application"].astype(str).isin(_out_of_scope_subs)
        ].copy()
        if df_scope.empty:
            df_scope = df_analysis   # never scope away the entire batch
    else:
        df_scope = df_analysis


    # Per-day breach from window_agg (populated inside the has_end_time block below).
    # Used to set window["breach"] from per-sub-app ceilings instead of global_ceil
    # so the bar chart and drill-down table are consistent with compliance_engine's verdict.
    _window_day_breach = None

    # Reverse map: job_name → sub_application (from df_scope).
    # Used by SLA lookups to build composite "SubApp|JobName" keys that match
    # job_sla_map's key format, and by the has_end_time path to resolve sub-app
    # ceilings from _win_ceiling_map.
    _job_sub_rev: Dict[str, str] = {}
    if "Sub_Application" in df_scope.columns and "Job_Name" in df_scope.columns:
        for _jn, _sa in (df_scope[["Job_Name", "Sub_Application"]]
                         .drop_duplicates()
                         .itertuples(index=False, name=None)):
            _sa_str = str(_sa) if _sa and str(_sa) not in ("", "nan", "None") else ""
            if _sa_str:
                _job_sub_rev[str(_jn)] = _sa_str

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
    # Per-day binding rollup (breach overrun vs the binding sub-app's OWN ceiling
    # + the tightest in-scope buffer) so the narrative + chart breach labels use
    # the correct ceiling instead of the global daily default.  Populated from
    # window_agg below; merged into the per-day `window` frame for serialization.
    _perday_bind_df = None
    # Representative daily ceiling used for single-number LABELS only (headline
    # window phrase, gauge legend, daily-window dashed line) — NEVER for the
    # per-(sub_app, date) compliance math, which always uses each sub-app's own
    # resolved ceiling. Defaults to the global ceiling; refined below to the
    # volume-dominant resolved ceiling once the per-sub-app window map is built.
    window_dominant_ceiling = float(global_ceil)
    # Distinct in-scope resolved ceilings (count + range). When the matrix binds
    # MORE THAN ONE ceiling, a single "inside the Nh window" headline is dishonest
    # (each day was judged against its own ceiling, not the dominant one), so the
    # UI must phrase the headline as "each within its own ceiling (min–max)".
    window_inscope_ceiling_count = 1
    window_inscope_ceiling_min = float(global_ceil)
    window_inscope_ceiling_max = float(global_ceil)

    if has_end_time and "Sub_Application" in df_analysis.columns:
        # ── Gap 1: Sentinel job map from BatchSLA XLSX ───────────────────────
        # When the customer has uploaded a BatchSLA_info.xlsx with First_Job /
        # Last_Job sentinels, restrict the window to those sentinel timestamps
        # instead of the global min/max of all jobs.  This prevents file-prep
        # jobs running before the official batch start from inflating the window.
        _sentinel_map: dict = {}  # normalised_sub_app → {"start_jobs": set, "end_jobs": set}
        try:
            from services import config_store as _cs_store
            _batch_sla_raw = _cs_store.get("_batch_sla_xlsx") or {}
            for _wf in _batch_sla_raw.get("workflows", []):
                _sa_key = str(_wf.get("workflow") or _wf.get("sub_app_pattern") or "").upper().strip()
                _fj     = str(_wf.get("first_job") or "").upper().strip()
                _lj     = str(_wf.get("last_job")  or "").upper().strip()
                if _sa_key and (_fj or _lj):
                    _sentinel_map[_sa_key] = {
                        "start_jobs": {j.strip() for j in _fj.split() if j.strip()} if _fj else set(),
                        "end_jobs":   {j.strip() for j in _lj.split() if j.strip()} if _lj else set(),
                    }
        except Exception:
            pass

        # G20b/G20c: Synthesise End_Time from Start_Time + Run_Sec for rows where
        # End_Time is NaT.  Without this, sub-apps where ALL rows in a date group
        # have NaT End_Time would produce elapsed_hrs = 0.0 (via fillna) and
        # silently appear compliant even though batch ran for hours.
        # Run_Sec is always populated by load_ctrlm_bytes; this is a safe fallback.
        _df_win_for_agg = df_window
        if (
            "End_Time" in df_window.columns
            and "Start_Time" in df_window.columns
            and "Run_Sec" in df_window.columns
            and df_window["End_Time"].isna().any()
        ):
            _df_win_for_agg = df_window.copy()
            _nat_end = _df_win_for_agg["End_Time"].isna()
            _valid_start = _df_win_for_agg["Start_Time"].notna()
            _valid_sec   = pd.to_numeric(_df_win_for_agg["Run_Sec"], errors="coerce").notna()
            _synth_mask  = _nat_end & _valid_start & _valid_sec
            if _synth_mask.any():
                _run_secs = pd.to_numeric(_df_win_for_agg.loc[_synth_mask, "Run_Sec"], errors="coerce")
                _df_win_for_agg.loc[_synth_mask, "End_Time"] = (
                    _df_win_for_agg.loc[_synth_mask, "Start_Time"]
                    + pd.to_timedelta(_run_secs, unit="s", errors="coerce")
                )

        # Primary: wall-clock window per Sub_Application per run_date.
        # Use df_window (cyclic + excluded sub-apps removed) to avoid 24h elapsed inflation.
        window_agg = (
            _df_win_for_agg.groupby(["Sub_Application", "run_date"], as_index=False)
              .agg(
                  first_start=("Start_Time", "min"),
                  last_end=("End_Time", "max"),
              )
        )
        # Filter out-of-scope sub_apps from compliance denominator
        if _out_of_scope_subs:
            window_agg = window_agg[
                ~window_agg["Sub_Application"].isin(_out_of_scope_subs)
            ].copy()

        # ── Gap 1 (continued): Patch sentinel-scoped first_start / last_end ──
        # For sub_apps that have a First_Job/Last_Job contract, recompute the
        # window boundaries using only the sentinel job timestamps per run_date.
        if _sentinel_map and "Job_Name" in df_window.columns:
            for _sa_norm, _sent in _sentinel_map.items():
                # Match normalised sub_app pattern against actual sub_app names
                _sa_matches = [
                    s for s in df_window["Sub_Application"].dropna().unique()
                    if str(s).upper() == _sa_norm or _sa_norm in str(s).upper()
                ]
                for _sa_actual in _sa_matches:
                    _sub_df = df_window[df_window["Sub_Application"] == _sa_actual]
                    for _rd, _rd_grp in _sub_df.groupby("run_date"):
                        _mask = window_agg[
                            (window_agg["Sub_Application"] == _sa_actual) &
                            (window_agg["run_date"] == _rd)
                        ].index
                        if _mask.empty:
                            continue
                        _jnames = _rd_grp["Job_Name"].str.upper()
                        # Start sentinel: min Start_Time of start sentinel jobs
                        if _sent["start_jobs"]:
                            _start_rows = _rd_grp[_jnames.isin(_sent["start_jobs"])]
                            if not _start_rows.empty:
                                window_agg.loc[_mask, "first_start"] = _start_rows["Start_Time"].min()
                        # End sentinel: max End_Time of end sentinel jobs
                        if _sent["end_jobs"]:
                            _end_rows = _rd_grp[_jnames.isin(_sent["end_jobs"])]
                            if not _end_rows.empty and "End_Time" in _end_rows.columns:
                                window_agg.loc[_mask, "last_end"] = _end_rows["End_Time"].max()

        window_agg["elapsed_hrs"] = (
            (window_agg["last_end"] - window_agg["first_start"])
            .dt.total_seconds() / 3600.0
        ).clip(lower=0).fillna(0.0)

        # ── Point-2 fix: per-(sub_app, date) busy-time + contiguous-block ─────
        # elapsed_hrs above is the first→last SPAN.  For spread / sequenced
        # batches that span is mostly IDLE GAP (e.g. a 2-phase daily that runs a
        # morning block, sits idle 13h, then an evening block reads as one ~20h
        # window) — so breaching on the span is structurally guaranteed to fail
        # almost every day even when no batch actually ran long.  The SLA-binding
        # measure is largest_block_hrs: the longest CONTIGUOUS run (clusters
        # split by idle gaps > BATCH_BLOCK_GAP_HRS), i.e. the real wall-clock the
        # batch occupied in one go.  active_busy_hrs (interval union) + idle_pct
        # are carried as context.  Breach (below) is judged on the block, not the
        # span.
        _blk_gap = float(getattr(pe_config, "BATCH_BLOCK_GAP_HRS", 1.0))
        _sa_busy_rows = []
        try:
            for (_sa_b, _rd_b), _g_b in (
                _df_win_for_agg.dropna(subset=["run_date"])
                .groupby(["Sub_Application", "run_date"])
            ):
                _bz, _blks = _busy_and_blocks_for_day(
                    list(_g_b["Start_Time"]), list(_g_b["End_Time"]), _blk_gap
                )
                _sa_busy_rows.append({
                    "Sub_Application":   _sa_b,
                    "run_date":          _rd_b,
                    "active_busy_hrs":   _bz,
                    "largest_block_hrs": round(max((b["span_hrs"] for b in _blks), default=0.0), 3),
                    "block_count_sa":    len(_blks),
                })
        except Exception:
            _sa_busy_rows = []
        if _sa_busy_rows:
            window_agg = window_agg.merge(
                pd.DataFrame(_sa_busy_rows), on=["Sub_Application", "run_date"], how="left"
            )
        for _c, _d in (("active_busy_hrs", 0.0), ("largest_block_hrs", 0.0), ("block_count_sa", 0)):
            if _c not in window_agg.columns:
                window_agg[_c] = _d
            window_agg[_c] = window_agg[_c].fillna(_d)
        # effective_hrs = SLA-binding batch-window duration (longest contiguous
        # run); fall back to the elapsed span only when block decomposition
        # produced nothing (single-timestamp / degenerate rows).
        window_agg["effective_hrs"] = window_agg["largest_block_hrs"].where(
            window_agg["largest_block_hrs"] > 0, window_agg["elapsed_hrs"]
        ).round(3)
        window_agg["idle_gap_hrs"] = (
            (window_agg["elapsed_hrs"] - window_agg["active_busy_hrs"]).clip(lower=0).round(3)
        )
        window_agg["idle_pct"] = np.where(
            window_agg["elapsed_hrs"] > 0,
            window_agg["idle_gap_hrs"] / window_agg["elapsed_hrs"] * 100.0,
            0.0,
        ).round(1)

        # ── Gap 5: Flag suspect windows using pe_config sentinel thresholds ──
        _max_sentinel_win = float(getattr(pe_config, "SENTINEL_MAX_WINDOW_HRS", 20.0))
        _min_sentinel_win = float(getattr(pe_config, "SENTINEL_MIN_WINDOW_HRS", 0.25))
        window_agg["suspect_flag"] = "OK"
        window_agg.loc[window_agg["elapsed_hrs"] > _max_sentinel_win, "suspect_flag"] = "SUSPECT_TOO_LONG"
        window_agg.loc[
            (window_agg["elapsed_hrs"] > 0) & (window_agg["elapsed_hrs"] < _min_sentinel_win),
            "suspect_flag"
        ] = "SUSPECT_TOO_SHORT"

        # ── PROMPT 1: Build direct XLSX workflow→SLA map (Priority 0 in _sub_sla) ────
        # When BatchSLA XLSX is present, build a direct sub_app-pattern→sla_hrs map
        # from the workflow rows. This is Priority 0 in _sub_sla() — it overrides
        # job_sla_map scanning for sub_apps where the workflow name substring-matches,
        # so a workflow gets its XLSX ceiling (e.g. 8.83h), not the 6.0h global_ceil
        # default. Sub-string match: "<WORKFLOW>" in "<ENV>_<WORKFLOW>" → True.
        _xlsx_sla_map: dict = {}
        try:
            from services import config_store as _cs_p1
            _bsla_p1 = _cs_p1.get("_batch_sla_xlsx") or {}
            for _wf_p1 in _bsla_p1.get("workflows", []):
                _wk = str(
                    _wf_p1.get("workflow") or _wf_p1.get("sub_app_pattern") or ""
                ).upper().strip()
                _wv = float(
                    _wf_p1.get("sla_hours") or _wf_p1.get("window_sla_hrs")
                    or _wf_p1.get("sla_hrs") or 0
                )
                if _wk and _wv > 0:
                    _xlsx_sla_map[_wk] = _wv
        except Exception:
            pass

        # ── Gap 2: _sub_sla — look up SLA by sub_app scope, not random job ──
        # Priority 0: direct XLSX workflow map (substring match: env prefix stripped)
        # Priority 1: scan all job_sla_map entries for this sub_app
        # Priority 2: schedule-type ceiling from uploaded XLSX ceilings map
        # Priority 3: global_ceil
        try:
            from services.sla_engine import classify_schedule as _cs_win
        except Exception:
            _cs_win = lambda _n: "DAILY"   # noqa: E731

        _sla_ceilings = sla_index.get("ceilings", {})

        def _sub_sla(row) -> float:
            sub_app  = str(row["Sub_Application"])
            sa_upper = sub_app.upper()
            # Priority 0: direct XLSX workflow SLA (fuzzy substring match)
            # "<WORKFLOW>" matches "<ENV>_<WORKFLOW>" via substring
            for _wk, _wv in _xlsx_sla_map.items():
                if _wk in sa_upper or sa_upper in _wk:
                    return _wv
            # Priority 1: scan all job_sla_map entries for this sub_app
            _sa_sla_vals = [
                float(v["sla_hrs"]) for k, v in job_sla_map.items()
                if (k.upper().startswith(f"{sa_upper}|") or k.upper() == sa_upper)
                and v.get("sla_hrs") and float(v["sla_hrs"]) > 0
            ]
            if _sa_sla_vals:
                # Return the most frequent value; tie-break by maximum (favour tighter XLSX value)
                try:
                    from statistics import mode as _stat_mode
                    return _stat_mode(_sa_sla_vals)
                except Exception:
                    return max(set(_sa_sla_vals), key=_sa_sla_vals.count)
            # Priority 2: schedule-type ceiling from uploaded XLSX ceilings map
            try:
                _stype = _cs_win(sub_app)
                if _stype in _sla_ceilings and float(_sla_ceilings[_stype]) > 0:
                    return float(_sla_ceilings[_stype])
            except Exception:
                pass
            return global_ceil

        window_agg["sla_hrs"] = window_agg.apply(_sub_sla, axis=1)
        # Point-2 fix: breach on the SLA-binding effective window (longest
        # contiguous batch run), NOT the first→last elapsed span.  The span is
        # mostly idle gap for spread / sequenced batches and over-breaches ~every
        # day; effective_hrs reflects the real wall-clock the batch occupied in
        # one contiguous run.  breach_basis is stamped for provenance / audit.
        window_agg["breach"]       = window_agg["effective_hrs"] > window_agg["sla_hrs"]
        window_agg["breach_basis"] = "largest_block"

        # Per-day binding rollup: a calendar day breaches because SOME sub-app ran
        # past ITS OWN ceiling (e.g. DAILY at 6h), not the global daily default.
        # Roll window_agg up per run_date → the worst overrun among breaching
        # sub-apps (vs that sub-app's ceiling) + the tightest buffer among ALL
        # in-scope sub-apps, so the day-level narrative/chart cite the real ceiling.
        try:
            _wa = window_agg.copy()
            _wa["_over"]   = (_wa["effective_hrs"] - _wa["sla_hrs"]).round(3)
            _wa["_bufpct"] = np.where(
                _wa["sla_hrs"] > 0,
                (_wa["sla_hrs"] - _wa["effective_hrs"]) / _wa["sla_hrs"] * 100.0,
                100.0,
            ).round(1)
            _bind_rows = []
            for _rd, _grp in _wa.groupby("run_date"):
                _row = {"run_date": _rd}
                _brc = _grp[_grp["breach"]]
                if not _brc.empty:
                    _w = _brc.loc[_brc["_over"].idxmax()]
                    _row.update({
                        "breach_sub_app":       str(_w["Sub_Application"]),
                        "breach_sub_effective": float(_w["effective_hrs"]),
                        "breach_sub_ceil":      float(_w["sla_hrs"]),
                        "breach_overrun_hrs":   float(max(_w["_over"], 0.0)),
                    })
                _t = _grp.loc[_grp["_bufpct"].idxmin()]
                _row.update({
                    "tight_sub_app":   str(_t["Sub_Application"]),
                    "min_buffer_pct":  float(_t["_bufpct"]),
                    "tight_effective": float(_t["effective_hrs"]),
                    "tight_ceil":      float(_t["sla_hrs"]),
                })
                _bind_rows.append(_row)
            if _bind_rows:
                _perday_bind_df = pd.DataFrame(_bind_rows)
        except Exception:
            _perday_bind_df = None

        # ── Gap 4: Wall-clock SLA deadline breach (parallel to duration breach) ──
        # Duration breach (above) answers "did the batch run LONGER than its
        # window?".  Deadline breach answers a distinct, contractually-binding
        # question: "did the batch FINISH past its absolute clock-time ceiling
        # (e.g. 06:00 EST)?" — a failure mode duration math cannot see.  A daily
        # batch that starts at 00:00 and ends 09:13 only used 9.2h of elapsed
        # time, but if its contracted ceiling is 06:00 it breached by 3.2h.
        #
        # The absolute deadline (sla_end) is sourced from the SAME resolved
        # contracts the rest of the pipeline uses: job_sla_map carries
        # 'sla_end_time' per 'Sub_App|Job'.  A window is only deadline-assessable
        # when its sub_app has a contracted sla_end — otherwise it is excluded
        # from the deadline denominator (never silently passed or failed).
        from datetime import time as _dl_time
        _deadline_map: Dict[str, "_dl_time"] = {}
        for _dk_raw, _dv_raw in job_sla_map.items():
            _end_iso = (_dv_raw or {}).get("sla_end_time")
            if not _end_iso:
                continue
            _sa_part = str(_dk_raw).split("|", 1)[0].upper().strip()
            if not _sa_part or _sa_part in _deadline_map:
                continue
            try:
                _deadline_map[_sa_part] = _dl_time.fromisoformat(str(_end_iso))
            except Exception:
                continue

        def _resolve_deadline(sub_app: str) -> Optional["_dl_time"]:
            sa = str(sub_app).upper().strip()
            if sa in _deadline_map:
                return _deadline_map[sa]
            # Workflow-tier deadline keyed by env-stripped substring (PROD_/TEST_…)
            for _dk, _dv in _deadline_map.items():
                if _dk and (_dk in sa or sa in _dk):
                    return _dv
            return None

        def _deadline_eval(r) -> "pd.Series":
            dl = _resolve_deadline(r["Sub_Application"])
            le = r.get("last_end")
            fs = r.get("first_start")
            if dl is None or pd.isna(le) or pd.isna(fs):
                return pd.Series({
                    "deadline_dt":          pd.NaT,
                    "deadline_breach":      False,
                    "deadline_overrun_hrs": 0.0,
                    "deadline_known":       dl is not None,
                })
            # Anchor the deadline to the batch START date, rolling to the next
            # calendar day when the deadline hour had already elapsed before the
            # batch began (overnight batch whose 06:00 ceiling lands the
            # following morning).  This mirrors the midnight-wrap intent of
            # sla_engine.compare_actual but uses the REAL last_end timestamp.
            anchor = (pd.Timestamp(fs).normalize()
                      + pd.Timedelta(hours=dl.hour, minutes=dl.minute, seconds=dl.second))
            if anchor < fs:
                anchor = anchor + pd.Timedelta(days=1)
            overrun = (pd.Timestamp(le) - anchor).total_seconds() / 3600.0
            return pd.Series({
                "deadline_dt":          anchor,
                "deadline_breach":      bool(overrun > 0),
                "deadline_overrun_hrs": round(max(overrun, 0.0), 3),
                "deadline_known":       True,
            })

        if not window_agg.empty:
            _dl_eval_df = window_agg.apply(_deadline_eval, axis=1)
            for _dc in ("deadline_dt", "deadline_breach", "deadline_overrun_hrs", "deadline_known"):
                window_agg[_dc] = _dl_eval_df[_dc]
        else:
            window_agg["deadline_dt"]          = pd.NaT
            window_agg["deadline_breach"]      = False
            window_agg["deadline_overrun_hrs"] = 0.0
            window_agg["deadline_known"]       = False
        # build_ceiling_map() is the SINGLE source of truth used by both
        # Batch Review and SLA Matrix — guarantees identical numbers on both tabs.
        # Per-row sla_hrs (from _sub_sla above) already encodes XLSX Priority 0;
        # build_ceiling_map() re-derives the per-sub_app ceiling from the same
        # XLSX source so the compliance engine's fallback path is also aligned.
        try:
            from services import compliance_engine as _ce_cm
            from services import config_store as _cs_cm
            _win_ceiling_map = _ce_cm.build_ceiling_map(
                sub_applications=list(window_agg["Sub_Application"].dropna().unique()),
                xlsx_config=_cs_cm.get("_batch_sla_xlsx") or None,
                pe_config_ref=pe_config,
            )
        except Exception:
            # Fallback: derive from window_agg sla_hrs column (mode per sub_app)
            for _sa in window_agg["Sub_Application"].dropna().unique():
                _sa_sla_series = window_agg.loc[window_agg["Sub_Application"] == _sa, "sla_hrs"]
                _sa_vc = _sa_sla_series.value_counts()
                _win_ceiling_map[str(_sa)] = float(_sa_vc.index[0]) if not _sa_vc.empty else global_ceil

        # ── Representative (volume-dominant) binding ceiling for LABELS ──────────
        # A customer matrix carries several daily-class windows (e.g. 6h BY_DAILY
        # vs 7.5h BY SEQ DAILY); the schedule-type vote behind global_ceil can
        # surface the looser one, so a lone "Daily Xh" label contradicts the
        # per-(sub_app, date) compliance and the per-job tables. Weight each
        # resolved ceiling by the in-scope RUN VOLUME it governs and pick the
        # dominant — the ceiling of the batch type the headline mainly describes.
        try:
            _ceil_vol: Dict[float, int] = {}
            for _sa_name, _sa_runs in (
                df_scope["Sub_Application"].astype(str).value_counts().items()
            ):
                _sa_ceil = _win_ceiling_map.get(_sa_name)
                if _sa_ceil is None:
                    _sa_ceil = _win_ceiling_map.get(_sa_name.upper())
                if _sa_ceil is None or float(_sa_ceil) <= 0:
                    continue
                _key = round(float(_sa_ceil), 2)
                _ceil_vol[_key] = _ceil_vol.get(_key, 0) + int(_sa_runs)
            if _ceil_vol:
                # Highest governed run-volume wins; tie-break to the TIGHTER ceiling
                # so a tie never loosens the displayed audit target.
                window_dominant_ceiling = float(
                    sorted(_ceil_vol.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                )
                # Distinct in-scope ceilings drive the honest multi-ceiling headline.
                _distinct_ceils = sorted(_ceil_vol.keys())
                window_inscope_ceiling_count = len(_distinct_ceils)
                window_inscope_ceiling_min = float(_distinct_ceils[0])
                window_inscope_ceiling_max = float(_distinct_ceils[-1])
        except Exception:
            pass

        for _, _r in window_agg.iterrows():
            _win_records.append({
                "sub_app":       str(_r["Sub_Application"]),
                "run_date":      str(_r["run_date"]),
                "elapsed_hrs":   float(_r["elapsed_hrs"]),
                # Point-2: effective (longest contiguous block) is the SLA-binding
                # duration the compliance engine judges; elapsed span + busy/idle
                # ride along as context.  breach is passed EXPLICITLY (block-based)
                # so the engine does not re-derive it from the elapsed span.
                "effective_hrs":     float(_r.get("effective_hrs", _r["elapsed_hrs"]) or 0.0),
                "active_busy_hrs":   float(_r.get("active_busy_hrs", 0.0) or 0.0),
                "largest_block_hrs": float(_r.get("largest_block_hrs", 0.0) or 0.0),
                "idle_pct":          float(_r.get("idle_pct", 0.0) or 0.0),
                "breach":            bool(_r.get("breach", False)),
                "breach_basis":      str(_r.get("breach_basis", "largest_block")),
                "schedule_type": _cs_win(str(_r["Sub_Application"])),
                "sla_ceil":      float(_r["sla_hrs"]),
                "suspect_flag":  str(_r.get("suspect_flag", "OK")),
                # Gap 4: wall-clock deadline dimension (parallel to duration breach)
                "last_end_clock":       (pd.Timestamp(_r["last_end"]).isoformat()
                                         if pd.notna(_r.get("last_end")) else None),
                "sla_deadline_clock":   (pd.Timestamp(_r["deadline_dt"]).isoformat()
                                         if pd.notna(_r.get("deadline_dt")) else None),
                "deadline_breach":      bool(_r.get("deadline_breach", False)),
                "deadline_overrun_hrs": float(_r.get("deadline_overrun_hrs", 0.0) or 0.0),
                "deadline_known":       bool(_r.get("deadline_known", False)),
            })

        # Legacy compatibility payload only. The daily buffer now uses the
        # canonical daily worst day, so this stays empty on purpose.
        _worst_window_info: dict = {}

        # Merge back to daily (Job_Name level) for compatibility with heatmap
        daily = (df_scope.groupby(["Job_Name", "run_date"], as_index=False)
                   .agg(total_hrs=("run_time_hrs", "sum"), runs=("run_time_hrs", "count")))
        # Carry the per-Sub_App breach flags into the daily Job_Name frame
        sub_breach = (window_agg.groupby("run_date")["breach"].any().reset_index()
                                .rename(columns={"breach": "_day_breach"}))
        daily = daily.merge(sub_breach, on="run_date", how="left")
        daily["breach"] = daily["_day_breach"].fillna(False)
        daily.drop(columns=["_day_breach"], inplace=True)

        # Save per-day breach so window["breach"] (the bar chart) uses per-sub-app
        # ceilings — not global_ceil — making the chart consistent with compliance %.
        _window_day_breach = sub_breach.rename(columns={"_day_breach": "_wb"})

        # Add per-job SLA ceiling to daily for correct heatmap cell coloring.
        # Without this the heatmap compares every job against the global ceiling and
        # can show a job as red when it is actually within its own contracted SLA.
        # Build per-job ceiling using _win_ceiling_map (sub_app → resolved ceiling).
        # This avoids the "SubApp|JobName" composite key mismatch that prevents
        # job_sla_map.get(job_name) from finding any entries.
        _job_ceil_ht: Dict[str, float] = {
            str(_jn): _win_ceiling_map.get(str(_sa), global_ceil)
            for _jn, _sa in (df_scope[["Job_Name", "Sub_Application"]]
                             .drop_duplicates()
                             .itertuples(index=False, name=None))
        } if "Sub_Application" in df_scope.columns and "Job_Name" in df_scope.columns else {}

        def _job_sla_for_daily_ht(row) -> float:
            return _job_ceil_ht.get(str(row.get("Job_Name", "")), global_ceil)
        if "sla_hrs" not in daily.columns:
            daily["sla_hrs"] = daily.apply(_job_sla_for_daily_ht, axis=1)

        # Per-job breach column for heatmap (per-job wall-clock vs per-job SLA)
        job_breach_map = {}
        for _, r in window_agg.iterrows():
            key = (str(r["Sub_Application"]), str(r["run_date"]))
            job_breach_map[key] = bool(r["breach"])

    else:
        # Fallback: per-Job_Name per-day sum vs per-job SLA
        daily = (df_scope.groupby(["Job_Name", "run_date"], as_index=False)
                   .agg(total_hrs=("run_time_hrs", "sum"), runs=("run_time_hrs", "count")))

        def _job_sla_for_row(row) -> float:
            job_name = str(row["Job_Name"])
            sub_app = _job_sub_rev.get(job_name, "")
            # Try composite key first (standard format in job_sla_map), fallback to bare name
            key = f"{sub_app}|{job_name}" if sub_app else job_name
            entry = job_sla_map.get(key) or job_sla_map.get(job_name)
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
    _raw_window_counts = (
        df.groupby("run_date", as_index=False)
          .agg(raw_job_count=("Job_Name", "nunique"),
               raw_run_count=("Job_Name", "size"),
               raw_total_hrs=("run_time_hrs", "sum"))
        if "Job_Name" in df.columns and "run_date" in df.columns
        else pd.DataFrame(columns=["run_date", "raw_job_count", "raw_run_count", "raw_total_hrs"])
    )
    _raw_window_names = (
        df.groupby("run_date")["Job_Name"]
          .apply(lambda s: [str(v) for v in pd.unique(s.dropna().astype(str))])
          .reset_index(name="raw_job_names")
        if "Job_Name" in df.columns and "run_date" in df.columns
        else pd.DataFrame(columns=["run_date", "raw_job_names"])
    )
    # job_count / total_hrs are IN-SCOPE (post-exclusion). raw_job_count is the
    # full per-day count (every sub_app), so excluded_job_count = raw − in-scope
    # now reflects ALL removed jobs (user-excluded + cyclic + MONTHLY/OUTBOUND),
    # not just the user-excluded ones.  job_count counts UNIQUE job names; the
    # *_run_count columns count EXECUTIONS (rows) so the UI can show "207 unique
    # jobs / 218 runs" and the analyst is never surprised by a repeat-run total.
    window = (df_scope.groupby("run_date", as_index=False)
                .agg(total_hrs=("run_time_hrs", "sum"),
                     job_count=("Job_Name", "nunique"),
                     scope_run_count=("Job_Name", "size"))
                .sort_values("run_date"))
    if not _raw_window_counts.empty:
        window = window.merge(_raw_window_counts, on="run_date", how="left")
    else:
        window["raw_job_count"] = window["job_count"]
        window["raw_run_count"] = window["scope_run_count"]
    if not _raw_window_names.empty:
        window = window.merge(_raw_window_names, on="run_date", how="left")
    else:
        window["raw_job_names"] = [[] for _ in range(len(window))]
    window["raw_job_count"] = window["raw_job_count"].fillna(window["job_count"]).astype(int)
    if "raw_run_count" not in window.columns:
        window["raw_run_count"] = window["scope_run_count"]
    window["raw_run_count"]   = window["raw_run_count"].fillna(window["scope_run_count"]).astype(int)
    window["scope_run_count"] = window["scope_run_count"].fillna(window["job_count"]).astype(int)
    window["excluded_job_count"] = (window["raw_job_count"] - window["job_count"]).clip(lower=0).astype(int)
    # Per-day runtime carried by the excluded jobs (raw − in-scope). Surfaced so
    # the tooltip can reconcile the in-scope "Summed runtime" against a raw Excel
    # SUM of every row for the day — the difference IS this excluded runtime.
    if "raw_total_hrs" in window.columns:
        window["raw_total_hrs"] = window["raw_total_hrs"].fillna(window["total_hrs"])
    else:
        window["raw_total_hrs"] = window["total_hrs"]
    window["excluded_hrs"] = (window["raw_total_hrs"] - window["total_hrs"]).clip(lower=0).round(3)
    window["raw_job_names"] = window["raw_job_names"].apply(
        lambda v: [str(x) for x in v] if isinstance(v, list) else []
    )

    elapsed_available  = False
    window_breach_days = 0
    worst_elapsed_kpi  = 0.0
    avg_elapsed_kpi    = 0.0
    worst_elapsed_date = ""

    if has_end_time:
        try:
            # Canonical daily elapsed window: first job start → last job end for
            # the whole batch on each day. This is the number used everywhere.
            # Scope to in-scope sub_apps only — a MONTHLY job running before/after
            # the daily window would otherwise inflate the wall-clock elapsed and
            # contradict the per-sub-app compliance denominator.
            _df_win_scoped = _df_win_for_agg
            if _out_of_scope_subs and "Sub_Application" in _df_win_for_agg.columns:
                _df_win_scoped = _df_win_for_agg[
                    ~_df_win_for_agg["Sub_Application"].astype(str).isin(_out_of_scope_subs)
                ]
                if _df_win_scoped.empty:
                    _df_win_scoped = _df_win_for_agg
            _daily_elapsed = (
                _df_win_scoped.groupby("run_date", as_index=False)
                .agg(first_start=("Start_Time", "min"),
                     last_end=("End_Time", "max"),
                     job_count=("Job_Name", "nunique"))
                .dropna(subset=["run_date"])
            )
            if not _daily_elapsed.empty:
                _daily_elapsed["elapsed_hrs"] = (
                    (_daily_elapsed["last_end"] - _daily_elapsed["first_start"])
                    .dt.total_seconds() / 3600.0
                ).clip(lower=0).fillna(0.0)
                window = window.merge(
                    _daily_elapsed[["run_date", "elapsed_hrs"]],
                    on="run_date",
                    how="left",
                )
                window["elapsed_hrs"] = window["elapsed_hrs"].fillna(0.0).round(3)
                elapsed_available = True
                worst_elapsed_kpi  = float(_daily_elapsed["elapsed_hrs"].max())
                avg_elapsed_kpi    = float(_daily_elapsed["elapsed_hrs"].mean())
                worst_elapsed_date = str(
                    _daily_elapsed.loc[_daily_elapsed["elapsed_hrs"].idxmax(), "run_date"]
                )
                # ── Busy-time (interval union) + batch-block decomposition ──
                # elapsed_hrs is the first→last SPAN; active_busy_hrs is the union
                # of all run intervals (parallel jobs counted once) = real compute
                # time.  idle_gap_hrs is the dead time inside the span.  blocks are
                # the run clusters separated by gaps > BATCH_BLOCK_GAP_HRS so a
                # morning + evening batch reads as two phases, not one 20h window.
                try:
                    _block_gap = float(getattr(pe_config, "BATCH_BLOCK_GAP_HRS", 1.0))
                    _busy_rows = []
                    for _rd, _g in _df_win_scoped.dropna(subset=["run_date"]).groupby("run_date"):
                        _busy_h, _blocks = _busy_and_blocks_for_day(
                            list(_g["Start_Time"]), list(_g["End_Time"]), _block_gap
                        )
                        _busy_rows.append({
                            "run_date":          _rd,
                            "active_busy_hrs":   _busy_h,
                            "batch_blocks":      _blocks,
                            "block_count":       len(_blocks),
                            "largest_block_hrs": round(max((b["span_hrs"] for b in _blocks), default=0.0), 3),
                        })
                    if _busy_rows:
                        _busy_df = pd.DataFrame(_busy_rows)
                        window = window.merge(_busy_df, on="run_date", how="left")
                except Exception:
                    pass
        except Exception:
            pass
    if not elapsed_available:
        window["elapsed_hrs"] = 0.0
    # Busy/idle/blocks fallbacks so downstream serialization is always safe.
    if "active_busy_hrs" not in window.columns:
        window["active_busy_hrs"] = 0.0
    if "block_count" not in window.columns:
        window["block_count"] = 0
    if "largest_block_hrs" not in window.columns:
        window["largest_block_hrs"] = 0.0
    if "batch_blocks" not in window.columns:
        window["batch_blocks"] = [[] for _ in range(len(window))]
    window["active_busy_hrs"] = pd.to_numeric(window["active_busy_hrs"], errors="coerce").fillna(0.0).round(3)
    window["largest_block_hrs"] = pd.to_numeric(window["largest_block_hrs"], errors="coerce").fillna(0.0).round(3)
    window["block_count"]     = pd.to_numeric(window["block_count"], errors="coerce").fillna(0).astype(int)
    window["batch_blocks"]    = window["batch_blocks"].apply(
        lambda v: v if isinstance(v, list) else []
    )
    window["idle_gap_hrs"] = (window["elapsed_hrs"] - window["active_busy_hrs"]).clip(lower=0).round(3)
    window["idle_pct"] = np.where(
        window["elapsed_hrs"] > 0,
        window["idle_gap_hrs"] / window["elapsed_hrs"] * 100.0,
        0.0,
    ).round(1)

    # Attach the per-day binding rollup so the narrative + chart breach labels use
    # the binding sub-app's OWN ceiling (DAILY=6h) rather than the global daily.
    if _perday_bind_df is not None and not _perday_bind_df.empty:
        window = window.merge(_perday_bind_df, on="run_date", how="left")
    for _bc in ("breach_overrun_hrs", "breach_sub_effective", "breach_sub_ceil",
                "min_buffer_pct", "tight_effective", "tight_ceil"):
        if _bc not in window.columns:
            window[_bc] = np.nan
    for _sc in ("breach_sub_app", "tight_sub_app"):
        if _sc not in window.columns:
            window[_sc] = ""
        window[_sc] = window[_sc].fillna("")

    # ── Batch Window Compliance (canonical daily window vs resolved SLA) ──
    # The daily window records already carry the authoritative per-day breach
    # flag. Feed those directly into the shared compliance engine so Batch and
    # SLA Matrix always agree on the same denominator.
    if "breach" not in window.columns:
        if _window_day_breach is not None:
            # Per-sub-app breach: a day is "breached" if ANY in-scope sub_app exceeded
            # its own resolved SLA ceiling (XLSX → schedule-type → global default).
            # Keeps the bar chart, drill-down table, and spike detector consistent
            # with the compliance % KPI — all derived from the same per-sub-app verdict.
            window = window.merge(_window_day_breach, on="run_date", how="left")
            window["breach"] = window["_wb"].fillna(False)
            window.drop(columns=["_wb"], inplace=True, errors="ignore")
        elif elapsed_available:
            window["breach"] = window["elapsed_hrs"] > global_ceil
        else:
            window["breach"] = window["total_hrs"] > global_ceil

    if "sla_hrs" not in window.columns:
        window["sla_hrs"] = global_ceil

    window_records_daily = window.to_dict(orient="records")
    n_window_days = len(window_records_daily)
    batch_window_comp = 0.0
    window_breach_days = 0
    window_compliance = {
        "compliance_pct": 0.0,
        "breach_count": 0,
        "ok_count": 0,
        "at_risk_count": 0,
        "total_windows": 0,
        "excluded_windows": 0,
        "warnings": [],
        "granularity": "day",
    }
    if window_records_daily:
        try:
            from services import compliance_engine as _ce
            # Prefer _win_records (per-sub-app, sla_ceil from XLSX) over window_records_daily
            # (per-day totals, breach precomputed against global_ceil).  _win_records is
            # populated whenever wall-clock windows + Sub_Application are both available.
            _ce_records = _win_records if _win_records else window_records_daily
            _ce_cmap    = _win_ceiling_map if _win_records else {}
            _wc = _ce.compute_window_compliance(_ce_records, _ce_cmap)
            _wc["granularity"] = "day"
            window_compliance = _wc
            batch_window_comp = float(_wc.get("compliance_pct", 0.0))
            # compliance_pct is measured over per-(sub_app, date) windows, but the
            # "days breached" UI label must stay in honest CALENDAR-day units —
            # use the engine's distinct-day rollups, not the window counts.
            window_breach_days = int(_wc.get("breach_days", _wc.get("breach_count", 0)))
            n_window_days = int(_wc.get("total_days", _wc.get("total_windows", n_window_days)))
            # Re-stamp the per-day window breach flag from the engine's canonical
            # distinct breach-day set so the bar chart, breach calendar, and
            # decision gate all count the SAME days the compliance % is based on.
            # The raw per-sub-app rollup (window["breach"]) includes excluded
            # MONTHLY/CYCLIC/OUTBOUND types and can report one more breach day than
            # the compliance engine — re-stamping keeps every surface on 26, not 27.
            _bd_list = _wc.get("breach_day_list")
            if _bd_list is not None and "run_date" in window.columns:
                _bd_set = set(str(d) for d in _bd_list)
                window["breach"] = window["run_date"].astype(str).isin(_bd_set)
                window_records_daily = window.to_dict(orient="records")
        except Exception:
            batch_window_comp = round(
                ((n_window_days - int(window["breach"].sum())) / n_window_days * 100)
                if n_window_days else 0.0,
                1,
            )
            window_breach_days = int(window["breach"].sum()) if n_window_days else 0

    # Sub-application rollup — ALL sub_apps from df_analysis (not scoped) so the
    # analyst can see what MONTHLY/OUTBOUND contributed.  in_scope=False marks
    # sub_apps excluded from the compliance denominator.
    sub = (df_analysis.groupby("Sub_Application", as_index=False)
             .agg(total_hrs=("run_time_hrs", "sum"),
                  jobs=("Job_Name", "nunique")))
    sub["in_scope"] = ~sub["Sub_Application"].astype(str).isin(_out_of_scope_subs)

    # ── Per-job frame + compliance scope ─────────────────────────
    top_jobs = build_top_jobs_df(df_analysis, sla_index=sla_index)

    # Pattern-matched jobs that were NOT excluded because runtime exceeded the threshold.
    # These stay in scope, but we surface them as data-quality warnings so the user can
    # see that the rule was evaluated and intentionally not applied.
    _utility_warnings: list = []
    if "utility_reason" in top_jobs.columns and "Job_Name" in top_jobs.columns:
        _warn_mask = top_jobs["utility_reason"].astype(str).str.startswith("pattern_matched_not_excluded:")
        _warn_rows = top_jobs[_warn_mask]
        for _, _row in _warn_rows.iterrows():
            _reason = str(_row.get("utility_reason", ""))
            _m = re.match(
                r"^pattern_matched_not_excluded:(?P<pat>[^()]+)\((?P<val>[-0-9.]+)h>=(?P<thr>[-0-9.]+)h\)$",
                _reason,
            )
            if _m:
                _utility_warnings.append({
                    "code": "UTILITY_PATTERN_NOT_EXCLUDED",
                    "text": (
                        f"Job '{_row['Job_Name']}' matches utility pattern '{_m.group('pat')}' "
                        f"but runtime {_m.group('val')}h exceeds {_m.group('thr')}h — "
                        "treated as a real batch job."
                    ),
                    "severity": "info",
                    "job_name": str(_row["Job_Name"]),
                    "pattern": _m.group("pat"),
                })

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
    # Trust fix: when no customer matrix is uploaded (PATH C), worst_job_sla can
    # be an ADAPTIVE per-job baseline (history p95/variance) rather than the flat
    # pe_config default — a tight, low-variance job can legitimately resolve to
    # e.g. 0.26h instead of 6.0h, which then reads as a huge negative buffer even
    # though the UI's "System Defaults (Daily 6h)" banner implies one generous
    # shared ceiling. Carry the worst job's own sla_source/baseline_quality so
    # the gauge + worst-job card can say WHICH ceiling actually drove the number
    # instead of silently contradicting the banner.
    if fleet_sla_buffer is not None and not _scope_jobs.empty:
        fleet_sla_buffer["sla_source"] = str(worst_row.get("sla_source", "default"))
        fleet_sla_buffer["baseline_quality"] = str(worst_row.get("baseline_quality", "")) or None

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

    # ── Gap 4: Wall-clock deadline compliance summary (single source of truth) ──
    # Roll the per-(sub_app, date) deadline flags on _win_records up into one
    # canonical summary every screen reads — Batch Review, SLA Matrix and
    # Findings must cite the SAME breach-day count and worst overrun, never
    # recompute their own.  "Assessable" = windows that carry a contracted
    # wall-clock deadline (deadline_known); windows without one are excluded
    # from the denominator so we neither pass nor fail them silently.
    _dl_assessable = [w for w in _win_records if w.get("deadline_known")]
    _dl_breaches   = [w for w in _dl_assessable if w.get("deadline_breach")]
    _dl_breach_days = sorted({str(w["run_date"]) for w in _dl_breaches})
    _dl_breach_rows = sorted(
        _dl_breaches, key=lambda w: w.get("deadline_overrun_hrs", 0.0), reverse=True
    )
    deadline_compliance = {
        "has_deadlines":       len(_dl_assessable) > 0,
        "assessable_windows":  len(_dl_assessable),
        "breach_windows":      len(_dl_breaches),
        "breach_days":         len(_dl_breach_days),
        "breach_day_list":     _dl_breach_days,
        "compliance_pct": (
            round((1.0 - len(_dl_breaches) / len(_dl_assessable)) * 100.0, 1)
            if _dl_assessable else None
        ),
        "worst_overrun_hrs": round(
            max((w.get("deadline_overrun_hrs", 0.0) for w in _dl_assessable), default=0.0), 3
        ),
        "breaches": [
            {
                "sub_app":          w.get("sub_app"),
                "run_date":         w.get("run_date"),
                "last_end_clock":   w.get("last_end_clock"),
                "sla_deadline_clock": w.get("sla_deadline_clock"),
                "overrun_hrs":      round(float(w.get("deadline_overrun_hrs", 0.0) or 0.0), 3),
                "schedule_type":    w.get("schedule_type"),
            }
            for w in _dl_breach_rows[:15]
        ],
    }

    # Days present in the file but absent from the in-scope daily-window set (e.g.
    # a day that ran ONLY out-of-scope batch types such as MONTHLY).  Surfaced
    # explicitly so a "missing" date on the daily chart / long-pole strip is
    # EXPLAINED rather than silently dropped (the 2026-05-31 case).
    _window_excluded_days = []
    try:
        _wdates = set(str(d) for d in window["run_date"].tolist()) if "run_date" in window.columns else set()
        for _d in (str(x) for x in unique_dates):
            if _d in _wdates:
                continue
            _day_df = df[df["run_date"].astype(str) == _d]
            _subs = sorted(_day_df["Sub_Application"].astype(str).unique()) if "Sub_Application" in _day_df.columns else []
            _oos = [s for s in _subs if s in _out_of_scope_subs]
            _window_excluded_days.append({
                "date":              _d,
                "sub_apps":          _subs,
                "run_count":         int(len(_day_df)),
                "out_of_scope_subs": _oos,
                "all_out_of_scope":  bool(_subs) and all(s in _out_of_scope_subs for s in _subs),
            })
    except Exception:
        _window_excluded_days = []

    return {
        "daily":            daily,
        "monthly":          monthly,
        "window":           window,
        "compliance":       float(round(job_sla_comp, 1)),
        "job_sla_compliance":      float(round(job_sla_comp, 1)),
        "batch_window_compliance": float(round(batch_window_comp, 1)),
        "window_breach_days":      window_breach_days,
        "window_total_days":       n_window_days,
        # Canonical DAY-LEVEL window compliance (the PE sign-off headline): the
        # share of calendar days the batch finished within its window. Reconciles
        # exactly with the "{breach}/{total} day(s)" fraction shown beside it.
        # Distinct from batch_window_compliance, which is per-(sub_app × day) PAIR.
        "window_day_compliance_pct": (
            float(round((n_window_days - window_breach_days) / n_window_days * 100, 1))
            if n_window_days else None
        ),
        # Pair-level counts (actual denominator for batch_window_compliance %)
        "window_total_pairs":      int(window_compliance.get("total_windows") or 0) or None,
        "window_breach_pairs":     int(window_compliance.get("breach_count") or 0) if int(window_compliance.get("total_windows") or 0) else None,
        # Breach traceability: per-breach-day attribution (which sub-app drove each
        # breach day) + per-sub-app breach pattern (structural vs intermittent). Lets
        # the headline name the cause instead of an unattributed "N breaches". The
        # excluded-from-denominator list is sourced from the CANONICAL upstream
        # _excl_sub_reasons (see "excluded_sub_apps" below) where exclusion actually
        # happens — NOT re-derived here — so there is one source for that fact.
        "window_breach_attribution": window_compliance.get("breach_days_detail") or [],
        "window_sub_app_rollup":     window_compliance.get("per_sub_app") or [],
        # Config-driven cut-off for structural vs intermittent, surfaced so the
        # classification is self-documenting (e.g. "structural · threshold ≥60%").
        "window_structural_ratio": window_compliance.get("structural_ratio"),
        "window_date_count":       len(unique_dates),
        "window_excluded_days":    _window_excluded_days,
        # Canonical (sub_app, date)-granular window compliance from the shared engine
        "window_compliance":       window_compliance,
        # Gap 4: parallel wall-clock deadline compliance (absolute clock ceilings)
        "deadline_compliance":     deadline_compliance,
        "total_jobs":       t_jobs,
        "jobs_ok":          int(j_ok),
        "jobs_breach":      int(j_breach),
        "jobs_at_risk":     int(j_at_risk),
        "total_runs":       int(len(df)),
        # PROMPT 4: summed_runtime uses df_scope (all out-of-scope sub_apps
        # excluded) so the summed-runtime KPI agrees with the in-scope daily
        # picture and the window-compliance denominator.
        "total_hrs":        float(round(df_scope["run_time_hrs"].sum() if has_end_time else df_analysis["run_time_hrs"].sum(), 2)),
        # Out-of-scope sub_apps (user-excluded ∪ cyclic ∪ MONTHLY/OUTBOUND/…) —
        # exposed so build_batch_payload scopes the heatmaps identically.
        "out_of_scope_subs": sorted(str(s) for s in _out_of_scope_subs),
        # Sub-application rollup
        "sub_stats":        sub,
        "top_jobs":         top_jobs,
        "anomalies":        anomalies,
        "fleet_sla_buffer": fleet_sla_buffer,
        # ── Intelligence fields ──────────────────────────────────
        "worst_job_name":   worst_job_name,
        "worst_job_peak":   round(worst_job_peak, 3),
        "elapsed_available": elapsed_available,
        # PROMPT 2: worst window info (sub_app-resolved SLA as gauge denominator)
        "_worst_window_info": _worst_window_info if "window_agg" in dir() else {},
        # Headline elapsed-window KPI (DAILY/UNKNOWN sub_apps only — separate from
        # the bar chart which shows max across ALL in-scope sub_apps per date).
        "elapsed_window_kpi": {
            "worst_hrs":  round(worst_elapsed_kpi, 3),
            "avg_hrs":    round(avg_elapsed_kpi, 3),
            "worst_date": worst_elapsed_date,
        },
        "_worst_day_warning": (
            f"Worst-day date '{worst_elapsed_date}' was not found in the uploaded file date range."
            if worst_elapsed_date and worst_elapsed_date not in {str(d) for d in unique_dates}
            else ""
        ),
        "date_span_days":   date_span,
        "date_range":       [str(unique_dates[0]), str(unique_dates[-1])] if unique_dates else [],
        "ok_runs":          ok_count,
        "fail_runs":        fail_count,
        "sla_source":       sla_src_type,
        "sla_daily_hrs":    global_ceil,
        "sla_ceiling":      global_ceil,   # canonical name for downstream callers
        # Volume-dominant resolved ceiling for single-number LABELS (headline
        # window phrase, gauge legend, daily-window dashed line). Reconciles the
        # lone "Daily Xh" labels with the per-sub-app compliance + per-job tables;
        # never feeds the compliance math (which uses per-sub-app ceilings).
        "window_dominant_ceiling_hrs": round(float(window_dominant_ceiling), 3),
        # Distinct in-scope ceiling spread — when count > 1 the headline must say
        # "each within its OWN ceiling (min–max)" rather than pin the dominant one,
        # because the per-day breach flags used many ceilings, not just dominant.
        "window_inscope_ceiling_count": int(window_inscope_ceiling_count),
        "window_inscope_ceiling_min": round(float(window_inscope_ceiling_min), 3),
        "window_inscope_ceiling_max": round(float(window_inscope_ceiling_max), 3),
        "confidence":       conf,
        # ── Auto-detected SLA schedule mode (exposed for smart defaults) ────
        "sla_detected_mode": _detect_sla_mode(df),
        # ── SLA index for downstream (e.g. _build_sla_source_payload) ───────
        "_sla_index":       sla_index,
        # ── Retry storm warnings (job failure→cascade retries, NOT cyclic) ──
        "_retry_storms":    _retry_storms,
        # ── Utility pattern warnings (matched but not excluded due to runtime)
        "_utility_warnings": _utility_warnings,
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
    if "Job_Name" not in daily_df.columns or "run_date" not in daily_df.columns:
        return {"jobs": [], "dates": [], "cells": [], "limit": lim}

    work = daily_df.copy()
    hrs_col = "total_hrs" if "total_hrs" in work.columns else (
        "run_time_hrs" if "run_time_hrs" in work.columns else None
    )
    if hrs_col is None:
        return {"jobs": [], "dates": [], "cells": [], "limit": lim}

    work[hrs_col] = pd.to_numeric(work[hrs_col], errors="coerce").fillna(0.0)
    work = work.dropna(subset=["Job_Name", "run_date"])
    if work.empty:
        return {"jobs": [], "dates": [], "cells": [], "limit": lim}

    # Per-job SLA ceiling map: use the sla_hrs column from daily_df when available
    # (set from job_sla_map in compute_metrics).  This means a job with an XLSX
    # ceiling of 8h is judged against 8h, not the global 6h default, so heatmap
    # cell colors agree with the per-sub-app compliance %.
    job_sla_lim: Dict[str, float] = {}
    if "sla_hrs" in work.columns:
        for _jn, _grp in work.groupby("Job_Name"):
            _vals = pd.to_numeric(_grp["sla_hrs"], errors="coerce").dropna()
            if not _vals.empty:
                try:
                    job_sla_lim[str(_jn)] = float(_vals.mode().iloc[0])
                except Exception:
                    job_sla_lim[str(_jn)] = float(_vals.max())

    dates = sorted(work["run_date"].unique())[-21:]   # last 21 days

    # Rank jobs by operational attention first:
    # breaches > near-SLA days > peak runtime > total runtime.
    job_priority: Dict[str, Dict[str, Any]] = {}
    ranked_jobs: list[tuple[float, str]] = []
    for job_name, grp in work.groupby("Job_Name", dropna=True):
        job = str(job_name)
        vals = pd.to_numeric(grp[hrs_col], errors="coerce").fillna(0.0)
        job_ceil = job_sla_lim.get(job, lim)   # per-job SLA ceiling, falls back to global
        breach_days = int((vals > job_ceil).sum())
        near_days   = int(((vals > job_ceil * 0.85) & (vals <= job_ceil)).sum())
        peak_hrs    = float(vals.max()) if not vals.empty else 0.0
        total_hrs    = float(vals.sum()) if not vals.empty else 0.0
        avg_hrs      = float(vals.mean()) if not vals.empty else 0.0
        priority     = "critical" if breach_days > 0 else "warning" if near_days > 0 else "normal"
        reasons: list[str] = []
        if breach_days:
            reasons.append(f"{breach_days} breach day(s)")
        if near_days:
            reasons.append(f"{near_days} near-SLA day(s)")
        if peak_hrs > 0:
            reasons.append(f"peak {peak_hrs:.2f}h")
        score = (breach_days * 100000.0) + (near_days * 1000.0) + (peak_hrs * 100.0) + total_hrs
        job_priority[job] = {
            "priority": priority,
            "score": round(score, 2),
            "breach_days": breach_days,
            "near_days": near_days,
            "peak_hrs": round(peak_hrs, 2),
            "avg_hrs": round(avg_hrs, 2),
            "total_hrs": round(total_hrs, 2),
            "sla_limit": round(job_ceil, 2),
            "reason": "; ".join(reasons) if reasons else "Total runtime only",
        }
        ranked_jobs.append((score, job))

    ranked_jobs.sort(key=lambda x: (-x[0], x[1]))
    all_jobs = [job for _, job in ranked_jobs[:40]]
    priority_jobs = [
        {"job": job, **job_priority.get(job, {})}
        for _, job in ranked_jobs
        if job_priority.get(job, {}).get("priority") in ("critical", "warning")
    ][:15]

    cells = []
    for job in all_jobs:
        for d in dates:
            sub = work[(work["Job_Name"] == job) & (work["run_date"] == d)]
            if sub.empty:
                cells.append({"job": job, "date": str(d), "hrs": None, "breach": False})
            else:
                h = float(pd.to_numeric(sub[hrs_col], errors="coerce").fillna(0.0).sum())
                job_ceil = job_sla_lim.get(str(job), lim)
                cells.append({"job": job, "date": str(d), "hrs": round(h, 2),
                               "breach": h > job_ceil,
                               "sla_limit": round(job_ceil, 2)})

    return {
        "jobs":  all_jobs,
        "dates": [str(d) for d in dates],
        "cells": cells,
        "limit": lim,
        "job_priority": job_priority,
        "priority_jobs": priority_jobs,
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
# Failure-density grid — Sub_Application × run_date (Gap A)
# ─────────────────────────────────────────────────────────────────
def _build_failure_grid(df: pd.DataFrame, max_days: int = 30) -> Dict[str, Any]:
    """Sub_Application × run_date execution-failure pivot for the PE Findings heatmap.

    Rows   = Sub_Application (only those with ≥1 failure, worst-first, capped)
    Cols   = run_date (most recent `max_days` calendar days in the in-scope frame)
    Value  = count of FAILED job executions that (sub_app, day)

    Reuses the FAILED classification already applied upstream (ENDED NOT OK /
    ABENDED / TERMINATED → "FAILED"), so this is a plain groupby — no new
    computation. A PE reviewer can read straight down a column to see whether a
    sub-application failed on multiple consecutive days (the Bug-2 / repeat-offender
    question) rather than inferring it from a single last-status flag.

    Returns:
        {
          sub_apps: [...], dates: [...],
          cells: [{sub_app, date, fail_count, severity}, ...],   # failures only
          row_totals: {sub_app: total_failed_jobs},
          max_fail: int, total_failed_jobs: int, has_data: bool,
        }
    """
    empty = {
        "sub_apps": [], "dates": [], "cells": [], "row_totals": {},
        "max_fail": 0, "total_failed_jobs": 0, "has_data": False,
    }
    if df is None or df.empty:
        return empty
    if "Status" not in df.columns or "run_date" not in df.columns:
        return empty

    sub_col = ("Sub_Application" if "Sub_Application" in df.columns else
               "Folder" if "Folder" in df.columns else None)
    if sub_col is None:
        return empty

    # Date axis = most recent `max_days` calendar days across the WHOLE frame, so
    # clean days still render (an all-green column is meaningful evidence too).
    recent_dates = sorted(df["run_date"].dropna().astype(str).unique())[-max_days:]
    if not recent_dates:
        return empty

    fails = df[df["Status"] == "FAILED"].copy()
    if fails.empty:
        # No failures at all — still report the date axis so the card can show an
        # explicit all-clear strip instead of silently disappearing.
        return {**empty, "dates": recent_dates, "has_data": True}

    fails = fails.dropna(subset=["run_date"])
    fails[sub_col] = fails[sub_col].fillna("UNKNOWN").astype(str)
    fails["run_date"] = fails["run_date"].astype(str)
    fails = fails[fails["run_date"].isin(recent_dates)]
    if fails.empty:
        return {**empty, "dates": recent_dates, "has_data": True}

    pivot = (fails.groupby([sub_col, "run_date"]).size()
                  .reset_index(name="fail_count"))

    # Row ordering: worst offenders first; cap rows to keep the card compact.
    row_totals_series = (pivot.groupby(sub_col)["fail_count"].sum()
                              .sort_values(ascending=False))
    sub_apps = [str(s) for s in row_totals_series.index.tolist()][:25]
    _sub_set = set(sub_apps)
    row_totals = {str(k): int(v) for k, v in row_totals_series.items()
                  if str(k) in _sub_set}

    def _sev(n: int) -> str:
        if n <= 0:
            return "ok"
        if n == 1:
            return "warn"
        return "crit"   # 2+ failed jobs in one day for one sub-app = systemic

    cells = []
    for _, r in pivot[pivot[sub_col].astype(str).isin(_sub_set)].iterrows():
        n = int(r["fail_count"])
        cells.append({
            "sub_app":    str(r[sub_col]),
            "date":       str(r["run_date"]),
            "fail_count": n,
            "severity":   _sev(n),
        })

    return {
        "sub_apps":          sub_apps,
        "dates":             recent_dates,
        "cells":             cells,
        "row_totals":        row_totals,
        "max_fail":          int(pivot["fail_count"].max()),
        "total_failed_jobs": int(pivot["fail_count"].sum()),
        "has_data":          True,
    }


# ─────────────────────────────────────────────────────────────────
# Long-pole consistency heatmap — top-N longest jobs × run_date
# ─────────────────────────────────────────────────────────────────
def _build_longpole_matrix(df: pd.DataFrame, busy_ref_hrs: float = 0.0,
                           top_n: int = 8, share_pct_flag: float = 25.0,
                           max_days: int = 30) -> Dict[str, Any]:
    """Top-N longest jobs × run_date runtime matrix (the long-pole heatmap).

    Rows  = the ``top_n`` jobs with the highest MEAN single-run runtime (the
            consistent long-runners), worst-first.
    Cols  = the most recent ``max_days`` calendar days.
    Cell  = the LONGEST single execution (minutes) of that job on that day —
            max, because one slow run is the long-pole risk even when the
            other runs that day were quick.  Absent cell = job didn't run.

    Per row we also report avg/max/min minutes, run count, days present, a
    stability read (max ÷ avg → steady vs spiky), the share of the typical
    daily busy window the job consumes, and a long-pole flag when that share
    crosses ``share_pct_flag``.  Answers: which specific jobs eat the window,
    are they consistent, and on which days do they spike?
    """
    empty = {"jobs": [], "dates": [], "cells": [], "rows": [],
             "busy_ref_hrs": round(float(busy_ref_hrs or 0), 2),
             "max_minutes": 0.0, "share_pct_flag": share_pct_flag, "has_data": False}
    if df is None or df.empty or "Job_Name" not in df.columns or "run_date" not in df.columns:
        return empty
    if "run_time_hrs" not in df.columns:
        return empty

    work = df.dropna(subset=["run_date"]).copy()
    work["run_date"] = work["run_date"].astype(str)
    work["Job_Name"] = work["Job_Name"].astype(str)
    work["_min"] = pd.to_numeric(work["run_time_hrs"], errors="coerce") * 60.0
    work = work.dropna(subset=["_min"])
    if work.empty:
        return empty

    recent_dates = sorted(work["run_date"].unique())[-max_days:]
    work = work[work["run_date"].isin(recent_dates)]
    if work.empty:
        return empty

    # Rank jobs by mean single-run minutes → the consistent long-runners lead.
    job_mean = work.groupby("Job_Name")["_min"].mean().sort_values(ascending=False)
    top_jobs = [str(j) for j in job_mean.index.tolist()[:max(top_n, 1)]]
    _set = set(top_jobs)
    sub = work[work["Job_Name"].isin(_set)]
    if sub.empty:
        return empty

    # Cell = max single-run minutes per (job, day).
    cell_pivot = sub.groupby(["Job_Name", "run_date"])["_min"].max().reset_index(name="minutes")
    cells = [{"job": str(r["Job_Name"]), "date": str(r["run_date"]),
              "minutes": round(float(r["minutes"]), 1)}
             for _, r in cell_pivot.iterrows()]

    busy_ref_min = float(busy_ref_hrs or 0) * 60.0
    rows = []
    for j in top_jobs:
        jr = sub[sub["Job_Name"] == j]["_min"]
        avg_m = float(jr.mean()); max_m = float(jr.max()); min_m = float(jr.min())
        runs = int(jr.count())
        days_present = int(sub[sub["Job_Name"] == j]["run_date"].nunique())
        spike = round(max_m / avg_m, 2) if avg_m > 0 else 1.0
        share = round(avg_m / busy_ref_min * 100.0, 1) if busy_ref_min > 0 else 0.0
        rows.append({
            "job":              j,
            "avg_min":          round(avg_m, 1),
            "max_min":          round(max_m, 1),
            "min_min":          round(min_m, 1),
            "runs":             runs,
            "days_present":     days_present,
            "days_total":       len(recent_dates),
            "spike_ratio":      spike,
            "window_share_pct": share,
            "is_longpole":      bool(share >= share_pct_flag) if busy_ref_min > 0 else bool(avg_m >= 20.0),
            "stability":        "steady" if spike <= 1.5 else ("variable" if spike <= 2.5 else "spiky"),
        })

    return {
        "jobs":           top_jobs,
        "dates":          recent_dates,
        "cells":          cells,
        "rows":           rows,
        "busy_ref_hrs":   round(float(busy_ref_hrs or 0), 2),
        "max_minutes":    round(float(cell_pivot["minutes"].max()), 1),
        "share_pct_flag": share_pct_flag,
        "has_data":       True,
    }


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
    sla_source  = "default"
    baseline_quality = None
    if not top_jobs_df.empty and "sla_hrs" in top_jobs_df.columns and job_name:
        match = top_jobs_df[top_jobs_df["Job_Name"] == job_name]
        if not match.empty:
            val = float(match.iloc[0]["sla_hrs"])
            if val > 0:
                per_job_sla = val
            # Trust fix: surface WHICH ceiling actually drove this number —
            # "adaptive" (history-derived, PATH C) vs "sla_matrix"/"default" —
            # so the card can't silently contradict the SLA source banner.
            if "sla_source" in match.columns:
                sla_source = str(match.iloc[0].get("sla_source") or "default")
            if "baseline_quality" in match.columns:
                baseline_quality = str(match.iloc[0].get("baseline_quality") or "") or None

    buffer_pct = (
        round(((per_job_sla - peak_hrs) / per_job_sla * 100), 1)
        if per_job_sla and per_job_sla > 0 else 0.0
    )
    return {
        "job_name":   job_name,
        "peak_hrs":   peak_hrs,
        "sla_hrs":    per_job_sla,
        "buffer_pct": buffer_pct,
        "sla_source": sla_source,
        "baseline_quality": baseline_quality,
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
            "failure_grid":  {"sub_apps": [], "dates": [], "cells": [], "row_totals": {},
                              "max_fail": 0, "total_failed_jobs": 0, "has_data": False},
        }

    m = compute_metrics(df)
    top_jobs_df: pd.DataFrame = m["top_jobs"]
    window_df:   pd.DataFrame = m["window"]
    sub_df:      pd.DataFrame = m["sub_stats"]

    # In-scope frame for the heatmaps / gantt / hourly density — same exclusion
    # set used by the daily picture and window compliance, so every temporal
    # surface in the Ctrl-M review correlates against one coherent scope.
    _oos_subs = set(m.get("out_of_scope_subs", []))
    if _oos_subs and "Sub_Application" in df.columns:
        _df_payload_scope = df[~df["Sub_Application"].astype(str).isin(_oos_subs)].copy()
        if _df_payload_scope.empty:
            _df_payload_scope = df
    else:
        _df_payload_scope = df

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

    # Top 10 breaching jobs (buffer < 0); fall back to worst 10 by peak if none breaching.
    # Top 15 jobs by peak (used by the horizontal bar chart).
    #
    # Utility jobs (is_utility=True) must NOT crowd out real batch jobs from these
    # top-N views — a DB backup running 4h would otherwise take a top slot and push
    # real batch jobs beyond position 15, making them invisible after frontend filtering.
    # Fix: build top-N from non-utility jobs only, then append all utility jobs at the
    # end so the frontend utility detection panel still has access to them.
    if "is_utility" in top_jobs_df.columns:
        _real_jobs_df = top_jobs_df[~top_jobs_df["is_utility"].fillna(False)]
        _util_jobs_df = top_jobs_df[top_jobs_df["is_utility"].fillna(False)]
        breaches_df = _real_jobs_df[_real_jobs_df["buffer_pct"] < 0].head(10)
        if breaches_df.empty:
            breaches_df = _real_jobs_df.head(10)
        # Top 15 real batch jobs + all utility jobs (for detection panel)
        top15_df = pd.concat([_real_jobs_df.head(15), _util_jobs_df]).copy()
    else:
        breaches_df = top_jobs_df[top_jobs_df["buffer_pct"] < 0].head(10)
        if breaches_df.empty:
            breaches_df = top_jobs_df.head(10)
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
    _n2 = lambda v: (round(float(v), 3) if pd.notna(v) else None)
    # Per-day failure count for chart overlay (failed ✕ marker)
    fail_by_date: dict = {}
    if "Status" in df.columns:
        fail_series = df[df["Status"] == "FAILED"].groupby("run_date").size()
        fail_by_date = {str(d): int(n) for d, n in fail_series.items()}
    for _, r in window_df.iterrows():
        date_str = str(r["run_date"])
        elapsed_hrs = round(float(r.get("elapsed_hrs", 0)), 3)
        total_hrs   = round(float(r["total_hrs"]), 3)
        # Point-2: largest_block_hrs = longest contiguous batch run that day;
        # effective_hrs is the SLA-binding duration (block, falling back to the
        # elapsed span only when no block was decomposed).  The daily bar should
        # plot effective_hrs, with the elapsed span shown as faint idle context.
        largest_block_hrs = round(float(r.get("largest_block_hrs", 0) or 0), 3)
        effective_hrs = largest_block_hrs if largest_block_hrs > 0 else elapsed_hrs
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
            "raw_total_hrs": round(float(r.get("raw_total_hrs", total_hrs) or total_hrs), 3),
            "excluded_hrs":  round(float(r.get("excluded_hrs", 0) or 0), 3),
            "elapsed_hrs":  elapsed_hrs,
            "active_busy_hrs": round(float(r.get("active_busy_hrs", 0) or 0), 3),
            "idle_gap_hrs":    round(float(r.get("idle_gap_hrs", 0) or 0), 3),
            "idle_pct":        round(float(r.get("idle_pct", 0) or 0), 1),
            "largest_block_hrs": largest_block_hrs,
            "effective_hrs":     effective_hrs,
            "breach_overrun_hrs":   _n2(r.get("breach_overrun_hrs")),
            "breach_sub_app":       str(r.get("breach_sub_app") or ""),
            "breach_sub_effective": _n2(r.get("breach_sub_effective")),
            "breach_sub_ceil":      _n2(r.get("breach_sub_ceil")),
            "min_buffer_pct":       _n2(r.get("min_buffer_pct")),
            "tight_sub_app":        str(r.get("tight_sub_app") or ""),
            "tight_effective":      _n2(r.get("tight_effective")),
            "tight_ceil":           _n2(r.get("tight_ceil")),
            "batch_blocks":    list(r.get("batch_blocks", [])) if isinstance(r.get("batch_blocks", []), list) else [],
            "block_count":     int(r.get("block_count", 0) or 0),
            "job_count":    int(r["job_count"]),
            "scope_run_count": int(r.get("scope_run_count", r["job_count"])),
            "raw_job_count": int(r.get("raw_job_count", r["job_count"])),
            "raw_run_count": int(r.get("raw_run_count", r.get("scope_run_count", r["job_count"]))),
            "excluded_job_count": int(r.get("excluded_job_count", max(int(r.get("raw_job_count", r["job_count"])) - int(r["job_count"]), 0))),
            "raw_job_names": list(r.get("raw_job_names", [])) if isinstance(r.get("raw_job_names", []), list) else [],
            "breach":       bool(is_breach),
            "top_job":      top_job_per_day.get(date_str, ""),
            "has_failures": fail_by_date.get(date_str, 0) > 0,
            "fail_count":   fail_by_date.get(date_str, 0),
        })

    # RULE 6 — include Sub_Application so findings engine can use composite key
    # sla_hrs / sla_source must be included so the frontend can show per-job ceiling
    # is_utility must be included so the frontend utility-exclusion toggle works
    # baseline_quality + sla_path needed so frontend can show STRONG/MODERATE/WEAK
    # confidence tier and correctly label the "BASELINE" column in adaptive mode
    _job_cols = [c for c in ["Sub_Application", "Job_Name", "peak_hrs", "avg_hrs",
                              "total_hrs", "sla_hrs", "sla_source", "sla_path",
                              "buffer_pct", "sla_used_pct", "buffer_status",
                              "baseline_quality", "is_high_variance",
                              "fail_count", "is_utility", "utility_reason"]
                 if c in top_jobs_df.columns]

    # CHANGE 1: cache the full df so SLA-matrix upload can recompute without
    # the user re-uploading the Ctrl-M CSV.
    try:
        from services import session_cache as _sc_bbp
        _sc_bbp.set("_last_ctrlm_df_records", df.to_dict("records"))
        _sc_bbp.set("_last_ctrlm_df_columns", list(df.columns))
    except Exception:
        pass

    # Long-pole heatmap: reference the TYPICAL daily busy window (median of the
    # per-day interval-union busy time) so each long job's window-share is honest.
    _busy_ref_hrs = 0.0
    try:
        if "active_busy_hrs" in window_df.columns:
            _busy_pos = pd.to_numeric(window_df["active_busy_hrs"], errors="coerce")
            _busy_pos = _busy_pos[_busy_pos > 0]
            if not _busy_pos.empty:
                _busy_ref_hrs = float(_busy_pos.median())
    except Exception:
        _busy_ref_hrs = 0.0
    _longpole_matrix = _build_longpole_matrix(
        _df_payload_scope,
        busy_ref_hrs=_busy_ref_hrs,
        top_n=int(getattr(pe_config, "LONGPOLE_TOP_N", 8)),
        share_pct_flag=float(getattr(pe_config, "LONGPOLE_WINDOW_SHARE_PCT", 25.0)),
    )

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
            # Day-level headline (canonical for PE sign-off): % of calendar days the
            # batch made its window. Pair-level window_compliance_pct stays available
            # as a labeled secondary detail — never the headline.
            "window_day_compliance_pct":   m.get("window_day_compliance_pct"),
            "window_total_pairs":      m.get("window_total_pairs"),
            "window_breach_pairs":     m.get("window_breach_pairs"),
            # Breach traceability surfaced to the frontend headline.
            "window_breach_attribution": m.get("window_breach_attribution") or [],
            # Excluded sub-apps sourced from the CANONICAL upstream exclusion list
            # (_excl_sub_reasons, where MONTHLY/OUTBOUND/CYCLIC are actually removed
            # from scope) — mapped to the {sub_app, schedule_type, worst_hrs} shape the
            # headline reads. Single source; the engine never re-derives this.
            "window_excluded_sub_apps":  [
                {"sub_app": e.get("sub_app"),
                 "schedule_type": e.get("reason"),
                 "worst_hrs": e.get("peak_hrs")}
                for e in (m.get("excluded_sub_apps") or [])
            ],
            "window_sub_app_rollup":     m.get("window_sub_app_rollup") or [],
            # Structural-pattern cut-off, passed through so the headline can publish
            # the rule alongside the classification ("structural · ≥60% of run-days").
            "window_structural_ratio":   m.get("window_structural_ratio"),
            # Canonical (sub_app, date)-granular window compliance (shared engine).
            # Both Batch Review and SLA Matrix read the same definition.
            "window_compliance":           m.get("window_compliance"),
            # Gap 4: wall-clock deadline compliance — distinct from duration above.
            # Flattened headline fields plus the full breakdown object so every
            # screen cites the SAME breach-day count and worst overrun.
            "deadline_compliance":         m.get("deadline_compliance"),
            "deadline_compliance_pct":     (m.get("deadline_compliance") or {}).get("compliance_pct"),
            "deadline_breach_days":        (m.get("deadline_compliance") or {}).get("breach_days", 0),
            "deadline_has_data":           bool((m.get("deadline_compliance") or {}).get("has_deadlines")),
            "worst_deadline_overrun_hrs":  (m.get("deadline_compliance") or {}).get("worst_overrun_hrs", 0.0),
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
            "window_dominant_ceiling_hrs": m.get("window_dominant_ceiling_hrs"),
            "window_inscope_ceiling_count": m.get("window_inscope_ceiling_count"),
            "window_inscope_ceiling_min": m.get("window_inscope_ceiling_min"),
            "window_inscope_ceiling_max": m.get("window_inscope_ceiling_max"),
            "monthly_limit_hrs":  pe_config.SLA_MONTHLY_HRS,
            "fleet_sla_buffer":   m["fleet_sla_buffer"],
            # Window-level SLA buffer (whole nightly batch window vs SLA ceiling).
            # Preferred headline gauge metric; None when End_Time is unavailable.
            "window_sla_buffer":  _build_window_sla_buffer(m),
            # GAP-4: explicit flag so the UI knows which of the two buffer paths is active.
            # "window_elapsed"     — End_Time available; gauge measures whole batch window.
            # "fleet_peak_fallback" — End_Time absent; gauge falls back to worst-job peak.
            "gauge_buffer_source": "window_elapsed" if m.get("elapsed_available") else "fleet_peak_fallback",
            # Auto-detected schedule mode — lets sla_matrix default to same mode
            "sla_detected_mode":  m.get("sla_detected_mode", "DAILY"),
            # SLA resolution path: "A" = contracted XLSX, "C" = adaptive per-job baseline
            # Frontend uses this to correctly label the compliance view
            "sla_path": (top_jobs_df["sla_path"].iloc[0]
                         if "sla_path" in top_jobs_df.columns and not top_jobs_df.empty
                         else "C"),
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
                else _worst_elapsed(window_records, valid_dates=set(unique_dates))
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
                "per day across the whole batch."
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
            "excluded_jobs":    _build_excluded_jobs_list(top15_df),
        },
        "multi_app_folders":  _multi_app_folders,
        "excluded_sub_apps":  m.get("excluded_sub_apps", []),
        # Exact sub-applications dropped from the window denominator (cyclic,
        # OUTBOUND, MONTHLY, user-excluded …). Persisted so the SLA Matrix page
        # can reuse the IDENTICAL window scope and publish one shared denominator.
        "out_of_scope_subs":  sorted(str(s) for s in m.get("out_of_scope_subs", [])),
        "top_jobs":     top15_df[_job_cols].to_dict(orient="records"),
        "top_breaches": breaches_df[_job_cols].to_dict(orient="records"),
        "window":       window_records,
        "window_sub_app": (m.get("window_compliance") or {}).get("per_sub_app", []),
        "sub_stats":    sub_df.round({"total_hrs": 2}).to_dict(orient="records"),
        "anomalies":    m["anomalies"],
        "hourly_counts": _build_hourly_counts(_df_payload_scope),
        "sla_heatmap":  _build_sla_heatmap(m["daily"], ceiling=m.get("sla_ceiling")),
        "hour_heatmap": _build_hour_heatmap(_df_payload_scope),
        "failure_grid": _build_failure_grid(_df_payload_scope),
        "longpole_matrix": _longpole_matrix,
        "daily_jobs":   _build_daily_jobs(_df_payload_scope),
    }


# CHANGE 2: convenience function for SLA-upload routes to trigger a full
# recompute without the user re-uploading the Ctrl-M file.
def recompute_with_new_sla() -> "dict | None":
    """Rebuild the full batch payload from the cached Ctrl-M DataFrame.

    Called automatically after any SLA file is ingested so the gauge and
    compliance KPIs update immediately without a re-upload.  Returns the
    new payload dict, or None if no cached data exists.
    """
    try:
        from services import session_cache as _sc_rw
        records = _sc_rw.get("_last_ctrlm_df_records")
        columns = _sc_rw.get("_last_ctrlm_df_columns")
        if not records or not columns:
            return None
        df = pd.DataFrame(records, columns=columns)
        for col in ("Start_Time", "End_Time"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        if "Run_Sec" in df.columns:
            df["Run_Sec"] = pd.to_numeric(df["Run_Sec"], errors="coerce").fillna(0)
        if "run_time_hrs" not in df.columns and "Run_Sec" in df.columns:
            df["run_time_hrs"] = df["Run_Sec"] / 3600.0
        return build_batch_payload(df)
    except Exception as _e:
        logger.error("recompute_with_new_sla failed: %s", _e)
        return None


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


def _worst_elapsed(window_records: list, valid_dates: set[str] | None = None) -> dict | None:
    """Find the day with the highest elapsed window.

    When valid_dates is provided, the chosen date must exist in that set.
    """
    elapsed_days = [w for w in window_records if w.get("elapsed_hrs", 0) > 0]
    if not elapsed_days:
        return None
    worst = max(elapsed_days, key=lambda w: w["elapsed_hrs"])
    worst_date = str(worst.get("run_date") or "")
    if valid_dates and worst_date not in valid_dates:
        return None
    return {"run_date": worst_date, "elapsed_hrs": worst["elapsed_hrs"]}


def _build_window_sla_buffer(m: dict) -> dict | None:
    """Compute the batch-WINDOW SLA buffer from the daily elapsed window."""
    if not m.get("elapsed_available"):
        return None

    ewk = m.get("elapsed_window_kpi") or {}
    worst = float(ewk.get("worst_hrs", 0.0) or 0.0)
    ceil  = float(m.get("sla_ceiling") or pe_config.SLA_DAILY_HRS)
    worst_date = ewk.get("worst_date", "")
    worst_sub  = ""

    if worst <= 0 or ceil <= 0:
        return None

    avg   = float((m.get("elapsed_window_kpi") or {}).get("avg_hrs", 0.0) or 0.0)
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
        "buffer_hrs":         buffer_hrs,
        "buffer_pct":         buffer_pct,
        "avg_buffer_pct":     avg_buffer_pct,
        "worst_elapsed_hrs":  round(worst, 3),
        "avg_elapsed_hrs":    round(avg, 3),
        "sla_ceiling_hrs":    round(ceil, 3),
        "worst_day":          worst_date,
        "worst_sub_app":      worst_sub,
        "status":             status,
        "source":             "window_elapsed",
    }


def _build_data_warnings(m: dict) -> list:
    """Generate human-readable warnings about data quality issues."""
    warnings = []
    window_days = int(m.get("window_total_days") or 0)
    file_days = int(m.get("window_date_count") or 0)
    excl_days = m.get("window_excluded_days") or []
    if window_days and file_days and window_days != file_days:
        # Name WHICH day(s) and WHY — a MONTHLY-only day legitimately has no daily
        # window, so it is EXPECTED (info), not a denominator defect (warning).
        _named = []
        for _e in excl_days:
            if _e.get("all_out_of_scope") and _e.get("out_of_scope_subs"):
                _named.append(
                    f"{_e['date']} (ran only {', '.join(_e['out_of_scope_subs'])} — "
                    f"out of daily-window scope, {_e.get('run_count', 0)} run(s))"
                )
            else:
                _named.append(f"{_e['date']} ({_e.get('run_count', 0)} run(s))")
        _detail = "; ".join(_named)
        _all_oos = bool(excl_days) and all(e.get("all_out_of_scope") for e in excl_days)
        _txt = (
            f"Window denominator: {file_days} unique date(s) in the file, "
            f"{window_days} in the daily-window compliance rollup."
        )
        if _detail:
            _txt += (
                f" Excluded day(s): {_detail}."
                + (" These are not defects — only out-of-scope batch types ran." if _all_oos else "")
            )
        warnings.append({
            "code": "WINDOW_DENOMINATOR_MISMATCH",
            "text": _txt,
            "severity": "info" if _all_oos else "warning",
            "expected_days": file_days,
            "used_days": window_days,
            "excluded_days": excl_days,
        })
    if m.get("_worst_day_warning"):
        warnings.append({
            "code": "WORST_DAY_OUT_OF_RANGE",
            "text": m["_worst_day_warning"],
            "severity": "warning",
        })
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
    for uw in m.get("_utility_warnings", []):
        warnings.append(uw)
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
    for ww in (m.get("window_compliance") or {}).get("warnings", []):
        warnings.append({
            "code": "WINDOW_COMPLIANCE_WARNING",
            "text": ww,
            "severity": "warning",
        })
    return warnings


def _build_excluded_jobs_list(top_jobs_df) -> list:
    """Return a list of {job_name, reason} dicts for jobs excluded from SLA compliance.

    Pulls SHORT_JOB / INSUFFICIENT quality rows from top_jobs_df (if adaptive path
    was taken) plus any CYCLIC sentinel-flagged jobs that were removed before
    reaching top_jobs_df (carried in the dataframe via is_short_job / baseline_quality).
    """
    result = []
    if top_jobs_df is None or top_jobs_df.empty:
        return result
    try:
        name_col = "Job_Name" if "Job_Name" in top_jobs_df.columns else (
                   "job_name" if "job_name" in top_jobs_df.columns else None)
        if not name_col:
            return result
        for _, row in top_jobs_df.iterrows():
            if bool(row.get("is_utility", False)):
                reason = str(row.get("utility_reason", "")).strip()
                if reason.startswith("strong_utility:"):
                    reason = reason.replace("strong_utility:", "", 1)
                elif reason.startswith("runtime_gated:"):
                    reason = reason.replace("runtime_gated:", "", 1)
                result.append({
                    "job_name": str(row[name_col]),
                    "reason": reason or "UTILITY",
                })
                continue
            quality = str(row.get("baseline_quality", "")).upper()
            is_short = bool(row.get("is_short_job", False))
            if quality in ("SHORT_JOB", "INSUFFICIENT") or is_short:
                result.append({
                    "job_name": str(row[name_col]),
                    "reason":   "SHORT_JOB" if quality == "SHORT_JOB" or is_short else "INSUFFICIENT",
                })
    except Exception:
        pass
    return result


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

    # ── Adaptive-baseline detection (PATH C trust fix) ──────────────────────
    # When sla_type == "default" (no customer matrix uploaded), build_top_jobs_df
    # still gives EVERY job with enough history its OWN adaptive ceiling
    # (_compute_adaptive_sla: p95 / variance-based, PATH C) instead of the flat
    # pe_config default. That's a deliberate, useful PE feature — but the old
    # banner/warning text claimed "compliance uses assumed defaults" / a single
    # "Daily Xh" ceiling while the ACTUAL gauge math for many jobs used a much
    # tighter, job-specific number. Count how many jobs are on the adaptive path
    # so the UI can describe reality instead of a stale flat-default assumption.
    #
    # Only count GENUINE adaptive tiers (STRONG/MODERATE/WEAK) — a job with
    # sla_source=="adaptive" but baseline_quality in (SHORT_JOB, INSUFFICIENT)
    # has no real history-derived signal (single/near-zero runs) and is already
    # excluded from compliance (buffer_pct=None); it must not count toward
    # "adaptive is really driving this dashboard" or the true no-data case would
    # falsely flip into "adaptive_active".
    _top_df = m.get("top_jobs")
    _adaptive_job_count = 0
    _adaptive_total_jobs = 0
    if _top_df is not None and hasattr(_top_df, "empty") and not _top_df.empty \
            and "sla_source" in _top_df.columns:
        _adaptive_total_jobs = int(len(_top_df))
        _is_adaptive = _top_df["sla_source"] == "adaptive"
        if "baseline_quality" in _top_df.columns:
            _is_adaptive = _is_adaptive & ~_top_df["baseline_quality"].isin(
                ["SHORT_JOB", "INSUFFICIENT"]
            )
        _adaptive_job_count = int(_is_adaptive.sum())
    _adaptive_active = sla_type == "default" and _adaptive_job_count > 0

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
        # Trust fix: expose adaptive-path facts so the frontend never has to
        # guess or silently claim a flat default while PATH C is actually driving
        # the compliance math for some/all jobs.
        "adaptive_active":     _adaptive_active,
        "adaptive_job_count":  _adaptive_job_count,
        "adaptive_total_jobs": _adaptive_total_jobs,
    }

    # ── Contracted-window summary (honest multi-contract view) ─────────────────
    # A customer matrix usually carries several distinct windows (Dawn Foods:
    # 6h daily, 7.5h seq, 9h weekly/monthly, 14h outbound). A single "Daily Xh"
    # banner number can't represent that and contradicts the per-job table, so
    # expose the DISTINCT resolved ceilings (customer-sourced only) for the UI.
    _sa_ceilings = sla_index.get("sub_app_ceilings") or {}
    if _sa_ceilings:
        _matrix_hrs = sorted({
            round(float(v.get("sla_hrs")), 2)
            for v in _sa_ceilings.values()
            if v.get("source") == "sla_matrix" and v.get("sla_hrs") is not None
        })
        if _matrix_hrs:
            base["resolved_ceilings"]    = _matrix_hrs
            base["resolved_ceiling_min"] = _matrix_hrs[0]
            base["resolved_ceiling_max"] = _matrix_hrs[-1]
            base["resolved_workflow_count"] = len([
                1 for v in _sa_ceilings.values() if v.get("source") == "sla_matrix"
            ])
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
        # No rich _sla_intelligence object in config_store. This happens for the
        # Tier-1 BatchSLA XLSX path (ceilings resolve through build_sla_index, which
        # doesn't persist an intelligence object) as well as true default mode.
        # Describe the source HONESTLY from the resolved ceilings instead of
        # mislabeling a loaded matrix as "none / Default system values".
        _is_default = (sla_type == "default")
        base["contracts"] = []
        if _is_default and _adaptive_active:
            # Trust fix: PATH C is engaged — most/all jobs are scored against
            # THEIR OWN history-derived ceiling (p95/variance), not a single
            # flat pe_config default. Saying "assumed defaults" here (as if one
            # generous shared ceiling applies) contradicts the gauge/worst-job
            # math and is exactly what produced the misleading -262% reading.
            # "BLOCKED / cannot produce green compliance" is also false — a
            # real (if provisional) compliance verdict IS being produced.
            base["schema_type"]    = "adaptive"
            base["detected_model"] = "Adaptive per-job baseline (history-derived)"
            base["warnings"] = [{
                "code": "ADAPTIVE_BASELINE_ACTIVE",
                "text": (
                    f"No SLA matrix uploaded — {_adaptive_job_count}/{_adaptive_total_jobs} "
                    "job(s) are scored against their OWN historical baseline "
                    "(p95/variance of past runs), not one flat default ceiling. "
                    "Buffers can look tight or negative for low-variance jobs even "
                    f"though the system default is {round(float(base['daily_hrs']), 1)}h. "
                    "Upload a customer SLA matrix for contracted ceilings."
                ),
                "severity": "warning",
            }]
            base["blocked"] = False
            base["note"] = (
                f"{_adaptive_job_count}/{_adaptive_total_jobs} job(s) using adaptive "
                "per-job baselines derived from Ctrl-M run history — no customer SLA "
                "matrix uploaded. Upload BatchSLA_info.xlsx for contracted ceilings."
            )
        elif _is_default:
            base["schema_type"]    = "none"
            base["detected_model"] = "Default system values"
            base["warnings"] = [{
                "code":     "NO_SLA_FILE",
                "text":     "No SLA matrix uploaded — compliance uses assumed defaults.",
                "severity": "critical",
            }]
            base["blocked"] = True
            base["note"] = (
                "Using default SLA windows from system configuration. "
                "Upload a customer SLA matrix to override."
            )
        else:
            # Customer SLA source (matrix / batch_sla_xlsx / customer fallback).
            # Leave schema_type empty (no parser classification to report) so the UI
            # derives the model chip from the resolved windows instead of "NONE".
            base["schema_type"] = ""
            _rc = base.get("resolved_ceilings") or []
            if len(_rc) > 1:
                base["detected_model"] = f"{len(_rc)} windows {_rc[0]:.1f}–{_rc[-1]:.1f}h"
            elif len(_rc) == 1:
                base["detected_model"] = f"1 window {_rc[0]:.1f}h"
            else:
                base["detected_model"] = "Customer SLA windows"
            base["warnings"] = []
            base["blocked"]  = False
            base["note"] = "Using customer-approved SLA windows from uploaded matrix."

    return base
