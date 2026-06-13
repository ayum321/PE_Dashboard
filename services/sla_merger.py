"""
SLA Merger — 3-tier SLA truth engine.

Merges three independent sources into a single, unified SLA resolution table:

  SOURCE 1  SOW PDF / DOCX
            → contractual batch-type ceilings (6h Daily, 8h Weekly)
            → DFU/SKU volume per contract year
            → stored in config_store under "_sow_sla_windows"

  SOURCE 2  BatchSLA_info.xlsx  (workflow-level file)
            → workflow name, first/last job boundary markers
            → expected SLA per workflow (e.g. 1.5h for TEST_WEEKLY_WF1)
            → schedule cadence + timezone
            → stored in config_store under "_batch_sla_xlsx"

  SOURCE 3  Ctrl-M CSV (60-day runtime history)
            → actual start/end times per job run
            → stored in session_cache / sla_matrix engine runs on it

Resolution priority (most specific wins):
  Tier 1  BatchSLA_info.xlsx workflow-level SLA  ← tightest contractual SLA
  Tier 2  SOW-derived batch-type ceiling          ← contractual but coarser
  Tier 3  Global pe_config defaults               ← last resort

Public API:
    parse_batch_sla_xlsx(raw_bytes, filename)
        → {"workflows": [...], "row_count": int, "source": str}

    resolve_sla_tier(job_name, sub_app, batch_sla_rows, sow_windows) -> dict
        → {"limit_hours", "batch_type", "workflow", "source", "tier"}

    build_workflow_job_map(ctrlm_df, batch_sla_rows) -> dict
        → { batch_name: {actual_hours, first_start, last_end, status} }

    compliance_label(actual_h, sla_h) -> str
        → "BREACH" | "AT_RISK" | "LONG_JOB" | "OK" | "UNKNOWN"
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional

# ── Batch-type inference ──────────────────────────────────────────────────────

# Fallback static patterns used only when pe_config is unavailable
_STATIC_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("BIWEEKLY",   ["BIWEEKLY", "BI_WEEKLY", "BI-WEEKLY"]),
    ("QUARTERLY",  ["QUARTERLY", "QUATERLY", "QTR"]),
    ("MONTHLY",    ["MONTHLY", "MLY"]),
    ("WEEKLY",     ["WEEKLY", "WLY", "_WF", "-WF"]),
    ("OUTBOUND",   ["OUTBOUND"]),
    ("CYCLIC",     ["CYCLIC", "_CYC"]),
    ("DAILY",      ["DAILY", "DLY", "EVERY DAY", "EVERYDAY", "NIGHTLY", "OVERNIGHT"]),
]

# Resolution priority order for detect_batch_type()
_DETECT_PRIORITY = ["ADHOC", "BIWEEKLY", "QUARTERLY", "MONTHLY", "WEEKLY", "OUTBOUND", "CYCLIC", "DAILY"]


def _strip_env_prefix(name: str) -> str:
    """Strip known environment prefixes (PROD_, TEST_, UAT_, etc.) and
    leading year-number tokens (e.g. 2025_) from a job name."""
    try:
        from services import pe_config
        prefixes = pe_config.ENV_PREFIXES_TO_STRIP
    except Exception:
        prefixes = ["PROD_", "TEST_", "UAT_", "DEV_", "STG_"]
    upper = name.upper()
    for pfx in prefixes:
        if upper.startswith(pfx.upper()):
            name = name[len(pfx):]
            upper = name.upper()
            break
    # Strip leading 4-digit year token (e.g. "2025_DAILY_DMD" → "DAILY_DMD")
    name = re.sub(r'^\d{4}[_\-]', '', name)
    return name


# Tokens that are meaningful batch-type indicators — NOT customer/env prefixes.
# Used by _all_normalized_forms to avoid stripping "WEEKLY_WF1" → "WF1" etc.
_BATCH_TYPE_WORDS: frozenset = frozenset([
    "DAILY", "WEEKLY", "MONTHLY", "BIWEEKLY", "QUARTERLY",
    "NIGHTLY", "OVERNIGHT", "CYCLIC", "OUTBOUND", "BATCH",
])


def _all_normalized_forms(name: str) -> list[str]:
    """Return all candidate normalized keys for an XLSX workflow name.

    Always returns [primary_form]. If the first _-delimited token looks like a
    customer/site prefix (all-alpha, ≤12 chars, not a batch-type indicator), also
    appends the prefix-stripped form so the XLSX index matches Ctrl-M names with or
    without the customer prefix.

    Examples:
        "PETBARN_DAILY"   → ["PETBARN_DAILY", "DAILY"]
        "PROD_DAILY"      → ["DAILY"]  (PROD_ stripped by _strip_env_prefix)
        "WEEKLY_WF1"      → ["WEEKLY_WF1"]  (WEEKLY is a batch-type word — keep)
        "HAUK_WEEKLYREPL" → ["HAUK_WEEKLYREPL", "WEEKLYREPL"]
    """
    primary = _strip_env_prefix(name).upper()
    if not primary:
        return []
    forms: list[str] = [primary]
    if "_" in primary:
        first_tok = primary.split("_")[0]
        if (first_tok.isalpha()
                and 2 <= len(first_tok) <= 12
                and first_tok not in _BATCH_TYPE_WORDS):
            secondary = primary[len(first_tok) + 1:]
            if secondary and secondary not in forms:
                forms.append(secondary)
    return forms


def detect_batch_type(batch_name: str, schedule: str = "") -> str:
    """Infer DAILY/WEEKLY/MONTHLY/… from workflow name + schedule text.

    Uses customer-configurable job_type_patterns from pe_config when available,
    falling back to the static list. Environment prefixes are stripped before matching.

    Extended types (not in standard pe_config patterns):
      CYCLIC_INTERVAL  — runs every N minutes (Haleon EDI_850: "Runs every 15 minutes")
      CALENDAR_BASED   — calendar-driven schedule, no standard cadence
      ADHOC            — no schedule, runs on demand — excluded from compliance
      ANNUAL           — runs once per year
      MONTHLY_WORKDAY  — 1st/last working day of month
    """
    try:
        from services import pe_config
        patterns = pe_config.JOB_TYPE_PATTERNS
    except Exception:
        patterns = {btype: kws for btype, kws in _STATIC_TYPE_PATTERNS}

    stripped = _strip_env_prefix(batch_name)
    combined = f"{stripped} {schedule}".upper()
    _sched_up = schedule.upper() if schedule else ""
    _comb_up  = combined

    # ── Extended schedule detection (from XLSX Schedule column text) ──────────
    # These checks run FIRST, before the standard pattern loop, because they
    # match specific schedule text that the standard patterns don't cover.
    # IMPORTANT: ADHOC, CYCLIC_INTERVAL, CALENDAR_BASED must also be checked
    # against the batch_name so they override the standard DAILY/WEEKLY fallback.
    _all_text = f"{stripped} {schedule}".upper()

    # ADHOC: "ADHOC", "Adhoc batch", "on demand"
    if re.search(r'\badhoc\b|\bon[\s-]*demand\b', _all_text, re.IGNORECASE):
        return "ADHOC"
    # CYCLIC_INTERVAL: "every N minutes", "N-minute interval"
    if re.search(r'every\s+\d+\s*min|\d+\s*min(?:ute)?(?:s)?\s+interval', _all_text, re.IGNORECASE):
        return "CYCLIC_INTERVAL"
    # CYCLIC / INTRADAY (from batch name) — use plain substring, not \b,
    # because job names use underscores (_INTRADAY) which are \w chars,
    # so \b doesn't fire between _ and a letter.
    if re.search(r'CYCLIC|INTRADAY|INTRA_|DASHBOARD', stripped.upper()):
        return "CYCLIC"
    # CALENDAR_BASED: "Calendar_444", "Calendar_445" — no trailing \b needed
    if re.search(r'CALENDAR|44[45]', _all_text.upper()):
        return "CALENDAR_BASED"
    # ANNUAL: "First week of January", "once a year", "annually"
    if re.search(r'first week of (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|once a year|annual', _all_text, re.IGNORECASE):
        return "ANNUAL"
    # MONTHLY_WORKDAY: "1st working day", "last working day"
    if re.search(r'(?:1st|first|last)\s+working\s+day', _all_text, re.IGNORECASE):
        return "MONTHLY_WORKDAY"
    # DATE_SPECIFIC_MONTHLY: "Every 12th of Month"
    if re.search(r'(?:every\s+)?\d+(?:st|nd|rd|th)\s+of\s+(?:each|every|the)?\s*month', _all_text, re.IGNORECASE):
        return "DATE_SPECIFIC_MONTHLY"
    # PERIODIC: "periodically", "runs periodically"
    if re.search(r'\bperiodic(?:ally)?\b', _all_text, re.IGNORECASE):
        return "PERIODIC"

    # Fast path: batch_name or schedule IS an exact schedule word (e.g. "Weekly",
    # "Bi-Weekly", "DAILY").  The pe_config patterns use underscore-prefix/suffix
    # style ("WEEKLY_", "_WEEK") which don't match standalone words — so we check
    # for exact word matches first, before the pattern loop.
    _EXACT_SCHEDULE_MAP = {
        "DAILY": "DAILY",   "DLY": "DAILY",     "NIGHTLY": "DAILY",
        "OVERNIGHT": "DAILY", "EOD": "DAILY",    "BOD": "DAILY",
        "WEEKLY": "WEEKLY", "WLY": "WEEKLY",    "WK": "WEEKLY",
        "EOW": "WEEKLY",    "BOW": "WEEKLY",    "EOWR": "WEEKLY",
        "BIWEEKLY": "BIWEEKLY", "BIWKLY": "BIWEEKLY",
        "FORTNIGHTLY": "BIWEEKLY", "FORTNIGHT": "BIWEEKLY",
        "MONTHLY": "MONTHLY", "MLY": "MONTHLY",  "MNTH": "MONTHLY",
        "EOM": "MONTHLY",   "BOM": "MONTHLY",   "EOMR": "MONTHLY",
        "MONTHEND": "MONTHLY", "MONTHCLOSE": "MONTHLY",
        "YEAREND": "MONTHLY", "YEARCLOSE": "MONTHLY",
        "QUARTERLY": "QUARTERLY", "QTR": "QUARTERLY",
        "ADHOC": "ADHOC",   "ADHOC_": "ADHOC",
    }
    # Check full normalized name first (e.g. "BIWEEKLY")
    _normalised = stripped.upper().replace("-", "").replace("_", "").replace(" ", "")
    if _normalised in _EXACT_SCHEDULE_MAP:
        return _EXACT_SCHEDULE_MAP[_normalised]
    # Check adjacent token pairs BEFORE individual tokens so "BI_WEEKLY" compound
    # names are resolved as BIWEEKLY, not WEEKLY (single-token match would fire first).
    import re as _re
    _tokens_raw = _re.split(r'[\s_\-]+', stripped.upper())
    for _i in range(len(_tokens_raw) - 1):
        _pair = _tokens_raw[_i] + _tokens_raw[_i + 1]   # e.g. "BI"+"WEEKLY"="BIWEEKLY"
        if _pair in _EXACT_SCHEDULE_MAP:
            return _EXACT_SCHEDULE_MAP[_pair]
    # Then check individual tokens — handles "DP Weekly", "SP Weekly", "Weekly SP Batch"
    for _tok in _tokens_raw:
        _tok_norm = _tok.replace("-", "").replace("_", "")
        if _tok_norm in _EXACT_SCHEDULE_MAP:
            return _EXACT_SCHEDULE_MAP[_tok_norm]
    # Also check the schedule column value alone
    if schedule:
        _sched_norm = _sched_up.replace("-", "").replace("_", "").replace(" ", "")
        if _sched_norm in _EXACT_SCHEDULE_MAP:
            return _EXACT_SCHEDULE_MAP[_sched_norm]
        # "Runs Every Saturday/Sunday" → WEEKLY; "Mon-Fri" → DAILY
        if any(d in _sched_up for d in ("SATURDAY", "SUNDAY", "SAT", "SUN")):
            return "WEEKLY"
        if re.match(r"^MON[\s\-]*FRI", _sched_up):
            return "DAILY"
        # "Runs every Monday" / "Every Tuesday & Wednesday"
        _DAYS = {"MONDAY": "WEEKLY", "TUESDAY": "PERIODIC", "WEDNESDAY": "PERIODIC",
                 "THURSDAY": "PERIODIC", "FRIDAY": "WEEKLY"}
        for _day, _dtyp in _DAYS.items():
            if _day in _sched_up:
                return _dtyp

    for btype in _DETECT_PRIORITY:
        keywords = patterns.get(btype, [])
        if any(kw.upper() in combined for kw in keywords):
            return btype
    # No evidence — return DAILY as conservative default (most common PE pattern).
    # Callers should check sla_source to know this was inferred, not explicit.
    return "DAILY"


# ── SLA text → float hours ────────────────────────────────────────────────────

def parse_sla_hours(value: Any) -> Optional[float]:
    """
    Convert any SLA text/number to float hours.
    Handles all real-world formats found across BY SCPO customers:
      "1.5 hrs"         → 1.5        (Michelin)
      "45 min"          → 0.75       (Michelin)
      "3 hours 30 min"  → 3.5        (Dole)
      "2hr 17 min"      → 2.28       (Dole — no space between number and hr)
      "5 hours 57 minutes" → 5.95    (Dole)
      "11 hrs+4 hrs"    → 15.0       (FLATS engine+buffer — TOTAL is the SLA)
      "4-5 hrs"         → 4.5        (FLATS range — midpoint used)
      "15-20 mins"      → 0.29       (FLATS range in minutes — midpoint)
      0.25              → 6.0        (Excel day fraction)
      "9PM" / "11:30PM" → None       (clock time, not duration — caller handles)
      1.5               → 1.5        (plain hours)

    IMPORTANT: Excel stores time-of-day as a day fraction (0 < v < 1).
      0.25   → 6.0h     (quarter of 24h)
      0.27083 → 6.5h    (06:30)
      0.375  → 9.0h     (09:00)
    These must be multiplied by 24 — NOT treated as literal hours.
    """
    if value is None:
        return None
    import pandas as _pd
    try:
        if _pd.isna(value):
            return None
    except Exception:
        pass
    # Numeric value: check for Excel day fraction before string conversion
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fv = float(value)
        if 0 < fv < 1:
            # Excel time fraction: multiply by 24 to get hours
            return round(fv * 24, 3)
        if fv >= 1:
            return fv  # already in hours
    s = str(value).strip().lower()
    # Compound: "3 hours 30 min", "2hr 17 min", "5 hours 57 minutes", "4 hours 48 min"
    # Must check BEFORE the simpler single-unit patterns
    m = re.search(r'(\d+(?:\.\d+)?)\s*h[ro]?u?r?s?\s+(\d+(?:\.\d+)?)\s*min', s)
    if m:
        return round(float(m.group(1)) + float(m.group(2)) / 60, 4)
    # Engine+buffer: "11 hrs+4 hrs", "13 hrs + 4 hrs", "7 hrs+ 4 hrs"
    # The total (engine + buffer) IS the SLA ceiling — do not add buffer again
    m = re.search(
        r'(\d+(?:\.\d+)?)\s*h[ro]?u?r?s?\s*\+\s*(\d+(?:\.\d+)?)\s*h[ro]?u?r?s?', s)
    if m:
        return round(float(m.group(1)) + float(m.group(2)), 3)
    # Range in hours: "4-5 hrs", "4 - 5 hours" → midpoint
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*h[ro]?u?r?s?', s)
    if m:
        return round((float(m.group(1)) + float(m.group(2))) / 2, 3)
    # Range in minutes: "15-20 mins", "15 - 20 minutes" → midpoint converted to hours
    m = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*min', s)
    if m:
        return round(((float(m.group(1)) + float(m.group(2))) / 2) / 60, 4)
    # Clock time like "9PM", "9:30PM", "11:00 AM" → not a duration, return None
    # Caller (FLATS Expected End Time) must handle these as deadline times
    if re.match(r'^\d{1,2}(?::\d{2})?\s*(?:am|pm)$', s):
        return None
    # "X hr" / "X hrs" / "X hour(s)"
    m = re.search(r"([\d.]+)\s*h[ro]?u?r?s?", s)
    if m:
        return float(m.group(1))
    # "X min" / "X minutes"
    m = re.search(r"([\d.]+)\s*min", s)
    if m:
        return round(float(m.group(1)) / 60, 4)
    # "H:MM" or "HH:MM" — NOT HH:MM:SS (that's a clock time)
    m = re.match(r"^(\d+):(\d{2})$", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    # Plain number → treat as hours (catches "6.5", "8.0" etc.)
    try:
        fv = float(s)
        if 0 < fv < 1:
            return round(fv * 24, 3)  # string form of Excel fraction
        return fv
    except ValueError:
        return None


def parse_excel_time_to_hhmm(value: Any) -> Optional[str]:
    """Convert an Excel time fraction or HH:MM[:SS] string to "HH:MM" string.

    Excel stores time-of-day as a fractional day:
      0.125   → "03:00"
      0.27083 → "06:30"
      0.875   → "21:00"
      4.167E-2 → "01:00"  (0.04167 × 24 × 60 = 60 min)

    Also normalises clock strings:
      "06:30:00" → "06:30"
      "9:00"     → "09:00"
    """
    if value is None:
        return None
    import pandas as _pd
    try:
        if _pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fv = float(value) % 1  # strip integer date portion, keep time fraction
        total_min = round(fv * 24 * 60)
        h, m = divmod(total_min, 60)
        return f"{h:02d}:{m:02d}"
    s = str(value).strip()
    match = re.match(r'^(\d{1,2}):(\d{2})', s)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    return None


def parse_start_time(value: Any) -> Any:
    """Parse a batch start time from XLSX cells into a datetime.time or list[datetime.time].

    Handles all formats seen across BY SCPO customers:
      Excel fraction  → 0.875 → time(21, 0)
      "10.45 AM"      → time(10, 45)    (Haleon: dot separator)
      "11.18 AM"      → time(11, 18)
      "8:00PM"        → time(20, 0)     (no space before AM/PM)
      "5:00 AM PHT"   → time(5, 0)      (Dole: embedded timezone stripped)
      "6:30 AM PHT & 1 PM PHT" → [time(6, 30), time(13, 0)]  (Dole: two times)
      "1:30PM"        → time(13, 30)    (FLATS: no space)
      None / blank    → None            (ADHOC — no fixed start time)

    Returns:
      None              — no fixed start (ADHOC)
      datetime.time     — single start time
      list[datetime.time] — twice-daily batch (two times in one cell)
    """
    import datetime
    if value is None:
        return None
    import pandas as _pd
    try:
        if _pd.isna(value):
            return None
    except Exception:
        pass
    # Numeric → Excel fraction
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fv = float(value) % 1
        total_min = round(fv * 24 * 60)
        h, m = divmod(total_min, 60)
        try:
            return datetime.time(h % 24, m % 60)
        except Exception:
            return None

    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "n/a", "tbd", "-", "adhoc", "on demand"):
        return None

    # Multiple times in one cell (Dole: "6:30 AM PHT & 1 PM PHT")
    _separators = re.split(r'\s*[&,/]\s*', s)
    if len(_separators) > 1:
        parsed = [parse_start_time(part.strip()) for part in _separators]
        parsed = [t for t in parsed if t is not None]
        return parsed if len(parsed) > 1 else (parsed[0] if parsed else None)

    # Normalize dot-separator: "10.45 AM" → "10:45 AM", "11.18 AM" → "11:18 AM"
    # Apply BEFORE timezone stripping so the timezone label doesn't interfere.
    # Only applies when there's an AM/PM marker — plain "10.5" stays numeric.
    s_clean = re.sub(r'^(\d{1,2})\.(\d{2})\s*(AM|PM)', r'\1:\2 \3', s, flags=re.IGNORECASE)

    # Strip embedded timezone labels (PHT, IST, AEST, CET, EDT, etc.)
    s_clean = re.sub(r'\s+[A-Z]{2,4}(?:\s*[+-]\d+)?$', '', s_clean.strip())
    s_clean = s_clean.strip()

    # Normalize no-space AM/PM: "8:00PM" → "8:00 PM", "1:30PM" → "1:30 PM"
    s_clean = re.sub(r'(\d)(AM|PM)$', r'\1 \2', s_clean, flags=re.IGNORECASE)
    s_clean = re.sub(r'(\d{2})(AM|PM)$', r'\1 \2', s_clean, flags=re.IGNORECASE)

    # Try standard time formats
    _TIME_FMTS = [
        "%I:%M %p", "%I:%M:%S %p",  # 12-hour with space
        "%H:%M",    "%H:%M:%S",      # 24-hour
        "%I %p",                     # "1 PM" bare hour
    ]
    for fmt in _TIME_FMTS:
        try:
            return datetime.datetime.strptime(s_clean.strip(), fmt).time()
        except ValueError:
            continue
    return None



    """Return a classification dict for each unique Sub_Application in df.

    Per sub_app returns:
      schedule_type  : DAILY | WEEKLY | MONTHLY | CYCLIC | BIWEEKLY | UNKNOWN
      ctrl_m_sla_hrs : SLA ceiling in hours, or None for CYCLIC/MONTHLY
      environment    : PRODUCTION | TEST | UNKNOWN
      parallel_group : base name if sub_app ends in _1/_2/_WF1/_WF2, else None
    """
    import pandas as _pd
    result: dict = {}
    if "Sub_Application" not in df.columns:
        return result

    _SLA_MAP = {
        "DAILY": 6.0, "NIGHTLY": 6.0, "OVERNIGHT": 6.0,
        "WEEKLY": 8.0, "BIWEEKLY": 8.0, "PERIODIC": 8.0, "UNKNOWN": 6.0,
    }

    for sub in df["Sub_Application"].dropna().unique():
        sub_str = str(sub).strip()
        if not sub_str:
            continue

        batch_type = detect_batch_type(sub_str)
        sla_hrs = _SLA_MAP.get(batch_type, None)

        upper = sub_str.upper()
        if upper.startswith("PROD_"):
            env = "PRODUCTION"
        elif any(upper.startswith(p) for p in ("TEST_", "UAT_", "DEV_", "STG_")):
            env = "TEST"
        else:
            env = "UNKNOWN"

        # Parallel group: ends in _1/_2/_3 or _WF1/_WF2
        pg_m = re.search(r'_(\d+)$', sub_str)
        wf_m = re.search(r'_(WF\d+)$', sub_str.upper())
        if pg_m:
            parallel_group = sub_str[:pg_m.start()]
        elif wf_m:
            parallel_group = sub_str[:wf_m.start()]
        else:
            parallel_group = None

        result[sub_str] = {
            "schedule_type":  batch_type,
            "ctrl_m_sla_hrs": sla_hrs,
            "environment":    env,
            "parallel_group": parallel_group,
        }
    return result


# ── Parse BatchSLA_info.xlsx ──────────────────────────────────────────────────

_COL_ALIASES = {
    # canonical_name: [accepted column name variants (lower-stripped)]
    # Batch / workflow name — accept any reasonable naming customers use
    "Batch_Name":       ["batch_name", "workflow", "workflow_name", "batch name",
                         "batch", "batch type", "sub_application", "sub application",
                         "sub_app", "sub app", "process", "task", "module",
                         "job", "job name", "batch_type"],
    "Schedule":         ["schedule", "cadence", "frequency"],
    "TimeZone":         ["timezone", "time_zone", "tz"],
    # Expected_SLA: duration/SLA value column — accepts many customer naming styles.
    # "Expected Run time" (Dole) is a DURATION, same semantic as SLA duration.
    # Variants with "(2019)" or "(PHT)" suffixes are stripped before matching.
    "Expected_SLA":     ["expected end time/sla", "expected end timesla",
                         "expected end time sla", "sla", "expected sla",
                         "sla_hours", "sla hrs", "expected_sla", "sla_limit",
                         "expected run time", "expected runtime",
                         "expected run time (hrs)", "expected run time(hrs)",
                         "sla (hrs)", "sla(hrs)", "run time sla"],
    # Expected_End_Time: time-of-day DEADLINE — sla_h computed as (deadline - start_time)
    "Expected_End_Time": ["expected end time", "expected_end_time", "sla deadline",
                          "batch end time", "target end time", "end time target"],
    # First_Job / Last_Job: sentinel job names — handle underscore vs space variants
    "First_Job":        ["first job_name", "first job name", "first_job_name",
                         "first_job", "first jobname", "start job", "start_job",
                         "start job name", "first job"],
    "Last_Job":         ["last job_name", "last job name", "last_job_name",
                         "last_job", "last jobname", "end job", "end_job",
                         "end job name", "last job"],
    # Accept standalone "Start" / "End" (common in summary sheets)
    "Start_Time":       ["start time", "start_time", "start", "window start",
                         "batch start", "start date", "start time (ist)",
                         "start time (aest)", "start time (cet)"],
    # Current end time: "Current run time(PHT)" (Dole) — same column, different name
    "End_Time":         ["current end time", "end time", "end_time",
                         "current_end_time", "current run time", "end", "window end",
                         "batch end", "end date", "current run time (pht)",
                         "current end time (pht)", "current runtime"],
    # Actual observed duration column (for last_run_hours_xlsx fallback)
    "Actual_Duration":  ["duration", "total batch time", "total_batch_time",
                         "elapsed", "elapsed time", "actual duration",
                         "live gmp run time", "gmp live", "drp gmp run time",
                         "gmp drp", "pipo lp run time",
                         "concurrent batch run time", "concurrent batch"],
}


def _normalize_col_header(col: str) -> str:
    """Normalize a raw column header for alias matching.

    Strips:
      - parenthetical suffixes: "Expected End Time/SLA (2019)" → "expected end time/sla"
      - embedded timezone labels: "(IST)", "(PHT)", "(AEST)", "(CET)"
      - leading/trailing whitespace
    Lowercases the result.
    """
    # Strip parenthetical suffixes like "(2019)", "(PHT)", "(IST)"
    s = re.sub(r'\s*\([^)]*\)\s*$', '', col.strip())
    # Also strip any remaining trailing parentheticals (e.g. multiple)
    s = re.sub(r'\s*\([^)]*\)', '', s)
    return s.lower().strip()


def _map_columns(df_columns: list[str]) -> dict[str, str]:
    """Return {canonical → actual_col} for columns found in df.

    Uses normalized column names (parenthetical suffixes stripped, lowercased)
    before matching against _COL_ALIASES, enabling Haleon-style columns like
    'Expected End Time/SLA (2019)' and 'Current run time(PHT)' to match correctly.
    """
    lower = {_normalize_col_header(c): c for c in df_columns}
    mapping: dict[str, str] = {}
    for canon, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in lower:
                mapping[canon] = lower[alias]
                break
    return mapping


def _overnight_delta_hours(start_val: Any, end_val: Any) -> Optional[float]:
    """Compute elapsed hours between two time-of-day values, handling overnight crossing.

    e.g. start=21:00, end=01:00 → 4.0h  (not -20h)
         start=21:00, end=03:00 → 6.0h
         start=08:30, end=11:50 → 3.33h
    Accepts pandas Timestamp, datetime.time, or string like '21:00:00' / '01:00'.
    Returns None if either value is null or unparseable.
    Sanity cap: delta > 18h is almost certainly a data error, return None.
    """
    import pandas as _pd
    try:
        if start_val is None or end_val is None:
            return None
        st = _pd.to_datetime(str(start_val), errors="coerce")
        et = _pd.to_datetime(str(end_val), errors="coerce")
        if _pd.isna(st) or _pd.isna(et):
            return None
        st_h = st.hour + st.minute / 60 + st.second / 3600
        et_h = et.hour + et.minute / 60 + et.second / 3600
        delta = et_h - st_h
        if delta < 0:
            delta += 24.0  # overnight crossing
        # Sanity: >18h delta almost certainly means a data error (e.g. wrong
        # date paired with wrong time).  Return None so fallback SLA kicks in.
        if delta > 18.0:
            return None
        return round(delta, 3)
    except Exception:
        return None


def _parse_sheet_workflows(df: "Any", warnings: list, sheet_name: str) -> list[dict]:
    """Parse one DataFrame (one XLSX sheet or CSV) into a list of workflow dicts.

    Called by parse_batch_sla_xlsx for each sheet that has a recognizable
    Batch_Name column.  Returns an empty list if the sheet has no usable rows.
    Each returned workflow dict includes "source_sheet" for traceability.
    """
    import pandas as pd
    df.columns = df.columns.astype(str).str.strip()
    col_map = _map_columns(list(df.columns))

    if "Batch_Name" not in col_map:
        return []

    def _col(df, canon: str, default=None, optional: bool = False, _cm=col_map):
        if canon in _cm:
            return df[_cm[canon]]
        if not optional:
            warnings.append(f"[{sheet_name}] Column '{canon}' not found — skipping.")
        return default

    # Expected_SLA (numeric) and Expected_End_Time (time-of-day) are mutually optional —
    # either one is sufficient to derive sla_h; suppress warning when the other is present.
    _has_sla_col    = "Expected_SLA"      in col_map
    _has_expend_col = "Expected_End_Time" in col_map
    # All SLA/schedule/timezone columns are optional for runtime-summary files.
    # Only Batch_Name is required — warn about it later per-row.
    sla_series          = _col(df, "Expected_SLA",      optional=True)
    expected_end_series = _col(df, "Expected_End_Time", optional=True)
    schedule_series     = _col(df, "Schedule",          optional=True)
    tz_series           = _col(df, "TimeZone",          optional=True)
    first_series        = _col(df, "First_Job",         optional=True)
    last_series         = _col(df, "Last_Job",          optional=True)
    start_series        = _col(df, "Start_Time",        optional=True)
    end_series          = _col(df, "End_Time",          optional=True)
    # Actual_Duration: fallback when the file has a pre-computed duration column
    # instead of separate Start + End timestamps (e.g. summary sheets).
    actual_dur_series   = _col(df, "Actual_Duration",   optional=True)

    workflows: list[dict] = []
    _consecutive_nan_rows = 0   # track section boundary (reset per sheet)

    for idx, row in df.iterrows():
        def _v(series, fallback=""):
            if series is None:
                return fallback
            val = series.iloc[idx] if hasattr(series, "iloc") else fallback
            return "" if (val is None or (isinstance(val, float) and val != val)) else str(val).strip()

        batch_name = _v(_col(df, "Batch_Name"), f"Row_{idx}")
        if not batch_name or batch_name.startswith("Row_"):
            _consecutive_nan_rows += 1
            # 3+ consecutive empty rows = section boundary — stop parsing
            if _consecutive_nan_rows >= 3 and workflows:
                warnings.append(f"Row {idx}: 3+ consecutive empty rows — "
                                "stopping parse (end of SLA definition section).")
                break
            continue
        _consecutive_nan_rows = 0   # reset on valid row

        # ── Dual-section XLSX detection ────────────────────────────────
        # Some BatchSLA files have SLA definitions in the top section and
        # historical run data below (separated by NaN rows, with a second
        # header like "DATE", "DAY", "START JOB", etc.).  Stop processing
        # when we hit a second header row.
        _bn_upper = batch_name.upper().strip()
        _SECONDARY_HEADERS = {
            "DATE", "DAY", "START JOB", "END JOB", "RUN DATE",
            "JOB DATE", "EXECUTION DATE", "TIMESTAMP", "RUN #",
            "WEEK", "MONTH", "OBSERVATION", "HISTORY", "ACTUAL",
            "ACTUAL RUN", "RUN NUMBER", "RUN_DATE", "SEQUENCE",
            "BATCH RUN", "EXECUTION", "EXEC DATE",
        }
        if _bn_upper in _SECONDARY_HEADERS:
            warnings.append(f"Row {idx}: detected secondary header '{batch_name}' — "
                            "stopping parse (historical run data section).")
            break
        # Also catch date-like values in Batch_Name (e.g. "2025-06-11 00:00:00")
        if re.match(r'^\d{4}-\d{2}-\d{2}', _bn_upper):
            warnings.append(f"Row {idx}: Batch_Name looks like a date ('{batch_name}') — "
                            "stopping parse (historical run data section).")
            break

        schedule   = _v(schedule_series)
        timezone   = _v(tz_series, "CET")
        first_job_raw  = _v(first_series)
        last_job_raw   = _v(last_series)

        # ── Multiple sentinels per cell (Haleon EDI_852 parallel sub-workflows) ──
        # Haleon cells: "JOB_A  JOB_B  JOB_C" (2+ spaces = delimiter)
        # Single sentinel: plain job name (no double-space)
        def _split_sentinels(raw: str) -> list[str]:
            """Split a sentinel cell into a list of job names.

            Delimiters seen across customers:
              - 2+ consecutive spaces (Haleon)
              - newline / tab
            Single-space within a job name is NOT a delimiter.
            """
            if not raw:
                return []
            # Try multi-space split first
            parts = re.split(r'  +|\t|\n', raw)
            parts = [p.strip() for p in parts if p.strip()]
            return parts if parts else [raw.strip()]

        first_jobs = _split_sentinels(first_job_raw)
        last_jobs  = _split_sentinels(last_job_raw)
        is_parallel = len(first_jobs) > 1 or len(last_jobs) > 1
        # Store primary sentinel (first element) for backward compat;
        # full lists stored as first_jobs_list / last_jobs_list
        first_job = first_jobs[0] if first_jobs else ""
        last_job  = last_jobs[0]  if last_jobs  else ""

        sla_raw = sla_series.iloc[idx] if sla_series is not None else None
        sla_h   = parse_sla_hours(sla_raw)
        sla_end_time_raw: Optional[str] = None   # clock-time deadline (e.g. "07:00") when applicable

        # If Expected_SLA parse returned None AND the raw value looks like a time-of-day
        # string (HH:MM[:SS]), treat it as a deadline and compute sla_h as overnight delta
        # from Start_Time.  Handles combined "Expected End Time/SLA" columns where the cell
        # holds a clock time rather than a numeric duration (e.g. "01:00:00" = 1am deadline).
        # Also store the raw clock-time string for downstream midnight_diff comparison
        # (reference script midnight_diff logic — actual_end vs sla_end_clock).
        if sla_h is None and sla_raw and start_series is not None:
            _sla_str = str(sla_raw).strip()
            if re.match(r'^\d{1,2}:\d{2}', _sla_str):
                try:
                    sla_h = _overnight_delta_hours(start_series.iloc[idx], sla_raw)
                    sla_end_time_raw = _sla_str  # preserve for clock-time buffer check
                except Exception:
                    pass

        # Fallback: compute sla_h from Expected_End_Time − Start_Time (time-based deadline column)
        # This handles customers who provide a time-of-day deadline (e.g. "01:00:00") rather than
        # a numeric SLA duration.  Overnight batches are handled correctly (21:00 → 01:00 = 4h).
        if sla_h is None and expected_end_series is not None and start_series is not None:
            try:
                exp_val = expected_end_series.iloc[idx]
                st_val0 = start_series.iloc[idx]
                if exp_val and st_val0:
                    sla_h = _overnight_delta_hours(st_val0, exp_val)
                    if sla_h is not None and sla_end_time_raw is None:
                        # Store the Expected_End_Time value as clock-time deadline
                        _exp_str = str(exp_val).strip()
                        if re.match(r'^\d{1,2}:\d{2}', _exp_str):
                            sla_end_time_raw = _exp_str
            except Exception:
                pass

        # Actual runtime from XLSX own timestamps (optional)
        # Uses overnight-aware delta so batches crossing midnight are handled correctly.
        actual_h: Optional[float] = None
        try:
            if start_series is not None and end_series is not None:
                st_val = start_series.iloc[idx]
                en_val = end_series.iloc[idx]
                if st_val and en_val:
                    delta_h = _overnight_delta_hours(st_val, en_val)
                    if delta_h is not None and delta_h > 0:
                        actual_h = delta_h
        except Exception:
            pass

        # Fallback: use pre-computed duration column when Start/End delta gave nothing
        # (handles summary sheets with "Total batch time", "Duration" etc.)
        if actual_h is None and actual_dur_series is not None:
            try:
                dur_val = actual_dur_series.iloc[idx]
                if dur_val is not None:
                    actual_h = parse_sla_hours(dur_val)
            except Exception:
                pass

        btype = detect_batch_type(batch_name, schedule)

        # ── Cross-validation: Expected_SLA vs Expected_End_Time ──────
        # When both columns exist, check for contradictions. If the numeric SLA
        # and the time-derived SLA differ by >50%, warn the user.
        _cross_sla_h = None
        if sla_h is not None and _has_sla_col and _has_expend_col and start_series is not None:
            try:
                exp_val = expected_end_series.iloc[idx]
                st_val0 = start_series.iloc[idx]
                if exp_val and st_val0:
                    _cross_sla_h = _overnight_delta_hours(st_val0, exp_val)
            except Exception:
                pass
            if _cross_sla_h is not None and sla_h > 0:
                _divergence = abs(_cross_sla_h - sla_h) / sla_h
                if _divergence > 0.5:
                    warnings.append(
                        f"Row {idx} '{batch_name}': Expected_SLA={sla_h:.1f}h but "
                        f"Expected_End_Time implies {_cross_sla_h:.1f}h "
                        f"(divergence {_divergence*100:.0f}%). Using Expected_SLA value."
                    )

        # ── SLA sanity bounds ─────────────────────────────────────────
        # SLA < 0.1h (6 min) or > 48h is almost certainly a parse error
        # Determine sla_source early so bounds check can reference it
        sla_source = "BATCH_SLA_XLSX" if sla_h is not None else None
        if sla_h is not None and sla_source == "BATCH_SLA_XLSX":
            if sla_h < 0.1:
                warnings.append(
                    f"Row {idx} '{batch_name}': SLA={sla_h:.3f}h ({sla_h*60:.0f} min) "
                    f"seems too small — verify source. Falling back to defaults."
                )
                sla_h = GLOBAL_DEFAULTS.get(btype, 6.0)
                sla_source = "GLOBAL_DEFAULT"
            elif sla_h > 48.0:
                warnings.append(
                    f"Row {idx} '{batch_name}': SLA={sla_h:.1f}h seems too large — "
                    f"verify source. Value kept but flagged."
                )

        # ── Tier 2/3 SLA fallback when XLSX has no SLA column ──────────
        # If the XLSX provides only runtime data (no SLA/Expected End Time),
        # sla_h is None and buffer% would show "—".  Apply the 3-tier resolver
        # so every workflow gets at least a default SLA for compliance scoring.
        if sla_h is None:
            try:
                from services import config_store as _cs
                _sow_w = _cs.get("_sow_sla_windows") or {}
                if btype in _sow_w:
                    _entry = _sow_w[btype]
                    _ceil = _entry.get("limit_hours") if isinstance(_entry, dict) else float(_entry)
                    if _ceil and _ceil > 0:
                        sla_h = float(_ceil)
                        sla_source = "SOW_EXTRACTED"
            except Exception:
                pass
        if sla_h is None:
            sla_h = GLOBAL_DEFAULTS.get(btype, 6.0)
            sla_source = "GLOBAL_DEFAULT"

        workflows.append({
            "workflow":           batch_name,
            "batch_type":         btype,
            "schedule":           schedule,
            "timezone":           timezone,
            "first_job":          first_job,
            "last_job":           last_job,
            "first_jobs_list":    first_jobs if is_parallel else None,
            "last_jobs_list":     last_jobs  if is_parallel else None,
            "is_parallel":        is_parallel,
            "sla_hours":          sla_h,
            "sla_source":         sla_source,
            "sla_end_time":       sla_end_time_raw,   # clock-time deadline ("07:00") or None
            "last_run_hours_xlsx": actual_h,
            "compliance":         compliance_label(actual_h, sla_h),
            "source":             "BATCH_SLA_XLSX",
            "source_sheet":       sheet_name,
            # ADHOC/CALENDAR/CYCLIC_INTERVAL workflows excluded from SLA compliance denominator
            "exclude_from_compliance": btype in ("ADHOC", "CALENDAR_BASED", "CYCLIC_INTERVAL", "ANNUAL"),
        })

    return workflows


def parse_batch_sla_xlsx(raw_bytes: bytes, filename: str = "BatchSLA_info.xlsx") -> dict:
    """
    Parse BatchSLA_info.xlsx (or any workflow-SLA spreadsheet).

    Scans ALL sheets that have a recognizable Batch_Name column (not just
    sheet 0) and merges their workflows.  Different customers store SLA data
    on different tabs (e.g. "Sheet1", "C&A", "Batch SLA").  Primary-normalized
    workflow names are deduplicated across sheets — earlier sheets win.

    Returns:
        {
          "workflows": [
              {
                "workflow": str,
                "batch_type": str,
                "schedule": str,
                "timezone": str,
                "first_job": str,
                "last_job": str,
                "sla_hours": float | None,
                "last_run_hours_xlsx": float | None,
                "compliance": str,
                "source_sheet": str,
              },
              ...
          ],
          "row_count": int,
          "filename": str,
          "columns_found": list,
          "source_sheet": str | None,
          "warnings": list,
        }
    """
    import pandas as pd

    warnings: list[str] = []
    ext = filename.rsplit(".", 1)[-1].lower()

    # ── Collect all DataFrames to process ──────────────────────────────────────
    # For XLSX: parse every sheet that has a recognizable Batch_Name column.
    # Different customers place SLA definitions on different sheets (Sheet1, C&A,
    # "Batch SLA", etc.).  Reading all recognized sheets and merging gives maximum
    # coverage without requiring the file to follow a specific tab layout.
    _dfs_to_process: list[tuple] = []   # (df, sheet_name)
    try:
        if ext in ("xlsx", "xls"):
            xl = pd.ExcelFile(io.BytesIO(raw_bytes))
            for _sn in xl.sheet_names:
                try:
                    _df = xl.parse(_sn)
                    _df.columns = _df.columns.astype(str).str.strip()
                    _cm = _map_columns(list(_df.columns))
                    if "Batch_Name" in _cm:
                        _dfs_to_process.append((_df, _sn))
                except Exception as _se:
                    warnings.append(f"Sheet '{_sn}': read error ({_se})")
            # Fallback: if no sheet had a recognised Batch_Name, use sheet 0 anyway
            if not _dfs_to_process and xl.sheet_names:
                _df0 = xl.parse(xl.sheet_names[0])
                _df0.columns = _df0.columns.astype(str).str.strip()
                _dfs_to_process.append((_df0, xl.sheet_names[0]))
        else:
            _df = pd.read_csv(io.BytesIO(raw_bytes))
            _df.columns = _df.columns.astype(str).str.strip()
            _dfs_to_process.append((_df, filename))
    except Exception as exc:
        return {"workflows": [], "row_count": 0, "filename": filename,
                "columns_found": [], "source_sheet": None,
                "warnings": [f"Cannot read file: {exc}"]}

    # ── Parse each sheet, deduplicate across sheets ────────────────────────────
    # Primary-normalized workflow name (strip env prefix → UPPER) is the dedup key.
    # First occurrence wins — sheets are processed in workbook order.
    _all_col_found: set[str] = set()
    _sheets_used: list[str] = []
    _seen_pkeys: set[str] = set()
    workflows: list[dict] = []

    for _df, _sheet_name in _dfs_to_process:
        _col_map = _map_columns(list(_df.columns))
        _all_col_found.update(_col_map.keys())
        _sh_wfs = _parse_sheet_workflows(_df, warnings, _sheet_name)
        _added = 0
        for wf in _sh_wfs:
            _pkey = _strip_env_prefix(wf.get("workflow") or "").upper()
            if _pkey and _pkey not in _seen_pkeys:
                _seen_pkeys.add(_pkey)
                workflows.append(wf)
                _added += 1
        if _added > 0:
            _sheets_used.append(_sheet_name)

    _explicit = sum(1 for w in workflows if w.get("sla_source") == "BATCH_SLA_XLSX")
    # ── Engine-based SLA enrichment ────────────────────────────────────────────
    # When no explicit SLA column was found (time-window XLSX format), use the
    # SLA intelligence engine to extract per-workflow SLA hours from Start/End
    # time columns.  This handles customers like Harry's USA who express SLA as
    # a batch window (e.g. 00:00–08:00 = 8h) rather than a numeric "Expected SLA" column.
    if _explicit == 0 and workflows:
        try:
            from services.sla_engine import ingest_sla_file as _ise_enrich
            _intel = _ise_enrich(raw_bytes, filename)
            if _intel.valid_rows > 0 and _intel.contracts:
                _eng_map = {
                    c.batch_name.upper(): c.sla_window_hrs
                    for c in _intel.contracts
                    if c.batch_name and c.sla_window_hrs and c.sla_window_hrs > 0
                }
                if _eng_map:
                    for w in workflows:
                        wf_key = _strip_env_prefix(w.get("workflow") or "").upper()
                        if wf_key in _eng_map:
                            w["sla_hours"] = _eng_map[wf_key]
                            w["sla_source"] = "BATCH_SLA_XLSX"
                    _explicit = sum(1 for w in workflows if w.get("sla_source") == "BATCH_SLA_XLSX")
        except Exception:
            pass
    _fallback = sum(1 for w in workflows if w.get("sla_source") in ("SOW_EXTRACTED", "GLOBAL_DEFAULT"))
    if _fallback > 0 and _explicit == 0:
        warnings.append(
            f"XLSX has no SLA column — applied Tier 2 (SOW) / Tier 3 (defaults) "
            f"for {_fallback} workflow(s). Upload a file with an 'Expected SLA' "
            f"or 'SLA' column for explicit per-workflow targets."
        )

    return {
        "workflows": workflows,
        "row_count": len(workflows),
        "filename":  filename,
        "columns_found": list(_all_col_found),
        "source_sheet": ", ".join(_sheets_used) if _sheets_used else None,
        "warnings":  warnings,
    }


# ── Ctrl-M first/last job actual runtime ─────────────────────────────────────

def build_workflow_job_map(ctrlm_df, batch_sla_rows: list[dict]) -> dict:
    """
    For each workflow row in BatchSLA_info, match its first_job / last_job
    in the Ctrl-M history and compute actual workflow elapsed time.

    Returns: { batch_name → {actual_hours, first_start, last_end, status} }
    """
    import pandas as pd

    result: dict[str, dict] = {}

    # Normalise Ctrl-M column names
    col_lower = {c.lower().replace(" ", "_"): c for c in ctrlm_df.columns}
    job_col   = col_lower.get("job_name") or col_lower.get("jobname") or "Job_Name"
    st_col    = col_lower.get("start_time") or col_lower.get("starttime") or "Start_Time"
    en_col    = col_lower.get("end_time") or col_lower.get("endtime") or "End_Time"

    for row in batch_sla_rows:
        batch_name = row.get("workflow", "")
        first_job  = (row.get("first_job") or "").strip().upper()
        last_job   = (row.get("last_job")  or "").strip().upper()

        if not first_job or not last_job:
            result[batch_name] = {"actual_hours": None, "status": "NO_JOB_MARKERS"}
            continue

        ctrlm_upper = ctrlm_df[job_col].str.upper()
        first_runs  = ctrlm_df[ctrlm_upper.str.contains(first_job, na=False, regex=False)]
        last_runs   = ctrlm_df[ctrlm_upper.str.contains(last_job,  na=False, regex=False)]

        if first_runs.empty or last_runs.empty:
            result[batch_name] = {
                "actual_hours": None,
                "first_job_found": not first_runs.empty,
                "last_job_found":  not last_runs.empty,
                "status": "JOB_NOT_FOUND_IN_CTRLM",
            }
            continue

        # Window OPENS at the EARLIEST occurrence of the start sentinel and
        # CLOSES at the LATEST occurrence of the end sentinel. Using .min() for
        # first_start is correct even when the start sentinel runs multiple times
        # (parallel sub-workflows) — .max() would pick the last occurrence and
        # make the window appear artificially short.
        first_start = pd.to_datetime(first_runs[st_col], errors="coerce").min()
        last_end    = pd.to_datetime(last_runs[en_col],  errors="coerce").max()

        if pd.isna(first_start) or pd.isna(last_end):
            result[batch_name] = {"actual_hours": None, "status": "TIMESTAMP_ERROR"}
            continue

        actual_hours = (last_end - first_start).total_seconds() / 3600

        # Midnight crossover guard: a negative window means the end sentinel's
        # timestamp rolled past midnight relative to the start — add 24h.
        if actual_hours < 0:
            actual_hours += 24.0

        # Sanity bounds — flag windows that are implausibly long (data spans
        # multiple batch cycles) or implausibly short instead of trusting them.
        try:
            from services import pe_config
            _max_w = float(getattr(pe_config, "SENTINEL_MAX_WINDOW_HRS", 20.0))
            _min_w = float(getattr(pe_config, "SENTINEL_MIN_WINDOW_HRS", 0.25))
        except Exception:
            _max_w, _min_w = 20.0, 0.25

        if actual_hours > _max_w:
            result[batch_name] = {
                "actual_hours":   round(actual_hours, 3),
                "first_job_found": True,
                "last_job_found":  True,
                "first_start":    str(first_start),
                "last_end":       str(last_end),
                "status":         "SUSPECT_TOO_LONG",
            }
            continue

        result[batch_name] = {
            "actual_hours": round(actual_hours, 3),
            "first_job_found": True,
            "last_job_found":  True,
            "first_start": str(first_start),
            "last_end":    str(last_end),
            "status": "SUSPECT_TOO_SHORT" if actual_hours < _min_w else "OK",
        }

    return result


# ── 3-tier SLA resolver ───────────────────────────────────────────────────────

#  Global defaults (last resort — Tier 3)
#  Must match pe_config.SLA_DEFAULTS — single source of truth.
#  pe_config reads from config_store at runtime; these are compile-time fallbacks
#  used only when pe_config import fails.
GLOBAL_DEFAULTS: dict[str, float] = {
    "DAILY":     6.0,
    "WEEKLY":    17.0,
    "BIWEEKLY":  17.0,
    "MONTHLY":   17.0,
    "QUARTERLY": 12.0,
    "OUTBOUND":  1.0,
}


def resolve_sla_tier(
    job_name: str,
    sub_app: str,
    batch_sla_rows: list[dict],
    sow_windows: dict,
    job_override_hrs: float = 0.0,
) -> dict:
    """
    3-tier SLA resolver — returns the effective SLA limit for a single job.

    Args:
        job_name:         Ctrl-M job name
        sub_app:          Sub-application / workflow label from Ctrl-M
        batch_sla_rows:   Parsed rows from BatchSLA_info.xlsx
        sow_windows:      Dict like {"DAILY": {"limit_hours": 6.0}, "WEEKLY": ...}
        job_override_hrs: Explicit override (e.g. from the user config UI)

    Returns:
        {
          "limit_hours": float,
          "batch_type":  str,
          "workflow":    str | None,
          "source":      str,   # "OVERRIDE"|"EXCLUDED"|"BATCH_SLA_XLSX"|"SOW_EXTRACTED"|"GLOBAL_DEFAULT"
          "tier":        int,   # -1 (excluded) | 0..3
        }
    """
    # Strip env prefixes for all matching operations
    job_stripped = _strip_env_prefix(job_name).upper()
    sub_stripped = _strip_env_prefix(sub_app).upper()

    batch_type = detect_batch_type(sub_app or job_name, "")

    # Tier -1: excluded batch types (CYCLIC, OUTBOUND, etc.)
    try:
        from services import pe_config
        excluded_types = pe_config.EXCLUDE_FROM_SLA
    except Exception:
        excluded_types = ["CYCLIC", "OUTBOUND"]
    if batch_type in excluded_types:
        return {"limit_hours": 0.0, "batch_type": batch_type,
                "workflow": None, "source": "EXCLUDED", "tier": -1}

    # Tier 0: explicit job override (from config UI)
    if job_override_hrs and job_override_hrs > 0:
        return {"limit_hours": float(job_override_hrs), "batch_type": batch_type,
                "workflow": None, "source": "OVERRIDE", "tier": 0}

    # Tier 1: BatchSLA_info.xlsx — workflow-level SLA
    # Matching priority:
    #   (a) Sub_Application exactly matches Batch_Name   ← diagram: SubApp exact match
    #   (b) job_name IS the first_job or last_job        ← diagram: JobName anchor
    #   (c) workflow name appears as substring of job    ← fallback
    for row in (batch_sla_rows or []):
        first  = _strip_env_prefix(row.get("first_job") or "").upper()
        last   = _strip_env_prefix(row.get("last_job")  or "").upper()
        wf     = _strip_env_prefix(row.get("workflow")  or "").upper()
        sla_h  = row.get("sla_hours")
        wftype = row.get("batch_type", batch_type)

        matched = False
        # (a) Sub_Application exact-match to Batch_Name
        if wf and (wf == sub_stripped or sub_stripped == wf):
            matched = True
        # (b) Job anchoring: job name IS (or contains) the first or last marker
        if not matched:
            if first and (first in job_stripped or first == job_stripped):
                matched = True
            elif last and (last in job_stripped or last == job_stripped):
                matched = True
        # (c) Workflow name substring in job (fallback)
        if not matched and wf and wf in job_stripped:
            matched = True

        if matched and sla_h and sla_h > 0:
            return {
                "limit_hours": float(sla_h),
                "batch_type":  wftype or batch_type,
                "workflow":    row.get("workflow"),
                "source":      "BATCH_SLA_XLSX",
                "tier":        1,
            }

    # Tier 1 token fallback — if no exact/substring match above, try token overlap
    # Handles mismatches like "TEST_WEEKLY_WF1" (XLSX) vs "PROD_WEEKLY" (Ctrl-M)
    best_sla: float | None = None
    best_wf:  str | None   = None
    best_score = 0
    sub_tok = frozenset(t for t in re.split(r"[_\s]+", sub_stripped) if len(t) >= 2)
    job_tok = frozenset(t for t in re.split(r"[_\s]+", job_stripped) if len(t) >= 2)
    for row in (batch_sla_rows or []):
        sla_h = row.get("sla_hours")
        if not sla_h or sla_h <= 0:
            continue
        wf = _strip_env_prefix(row.get("workflow") or "").upper()
        wf_tok = frozenset(t for t in re.split(r"[_\s]+", wf) if len(t) >= 2)
        score = max(len(wf_tok & sub_tok), len(wf_tok & job_tok))
        if score > best_score:
            best_score = score
            best_sla = float(sla_h)
            best_wf  = row.get("workflow")
    if best_score >= 2 and best_sla:   # standardised to ≥2 tokens (matches _bulk_lookup_bsla)
        return {
            "limit_hours": best_sla,
            "batch_type":  batch_type,
            "workflow":    best_wf,
            "source":      "BATCH_SLA_XLSX",
            "tier":        1,
        }

    # Tier 2: SOW-extracted batch-type ceiling
    if batch_type in (sow_windows or {}):
        entry = sow_windows[batch_type]
        ceiling = entry.get("limit_hours") if isinstance(entry, dict) else float(entry)
        if ceiling and ceiling > 0:
            return {
                "limit_hours": float(ceiling),
                "batch_type":  batch_type,
                "workflow":    None,
                "source":      "SOW_EXTRACTED",
                "tier":        2,
            }

    # Tier 3: Global defaults
    return {
        "limit_hours": GLOBAL_DEFAULTS.get(batch_type, 6.0),
        "batch_type":  batch_type,
        "workflow":    None,
        "source":      "GLOBAL_DEFAULT",
        "tier":        3,
    }


# ── Compliance ────────────────────────────────────────────────────────────────

def compliance_label(actual_h: Optional[float], sla_h: Optional[float]) -> str:
    """Classify a workflow's last-known run against its SLA.

    Thresholds read from pe_config (single canonical source).
    Falls back to module-level defaults if pe_config is unavailable.
    Formula: buffer_pct = (sla_h - actual_h) / sla_h * 100
      buffer <= 0%            → BREACH
      0% < buffer <= AT_RISK  → AT_RISK
      AT_RISK < buffer <= LJ  → LONG_JOB
      buffer > LJ             → OK
    """
    if actual_h is None or sla_h is None or sla_h <= 0:
        return "UNKNOWN"
    try:
        from services import pe_config as _pc
        _at = _pc.SLA_ATRISK_PCT   # e.g. 15.0
        _lj = _pc.SLA_LONGJOB_PCT  # e.g. 40.0
    except Exception:
        _at, _lj = 15.0, 40.0     # safe fallback if circular import
    buffer_pct = (sla_h - actual_h) / sla_h * 100
    if buffer_pct <= 0:
        return "BREACH"
    if buffer_pct <= _at:
        return "AT_RISK"
    if buffer_pct <= _lj:
        return "LONG_JOB"
    return "OK"
