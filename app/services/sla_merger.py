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
from numbers import Number
from typing import Any, Dict, List, Optional

# A genuine SLA contract states a repeatable clock time ("9:00 PM") — it does
# not carry a specific calendar date, since the same window applies every
# day/week/month. A Start/End cell that DOES carry a calendar date
# (e.g. "2026-04-09 00:29:00") is an OBSERVED timestamp of one historical
# execution, not a contract term. Used below to let a plain "Start Time" /
# "End Time" pair (no "Expected" prefix) count as the SLA window when the
# values are pure time-of-day — mirrors the same signal in sla_engine.py.
_DATED_VALUE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}"
)
_EXCEL_EPOCH_DATES = ("1899-12-30", "1899-12-31", "1900-01-00", "1900-01-01")


def _looks_dated(raw) -> bool:
    """True when a Start/End cell carries a calendar date, not just a
    repeatable clock time — i.e. it's an observed run, not an SLA term."""
    m = _DATED_VALUE_RE.search(str(raw or ""))
    if not m:
        return False
    return not m.group(0).startswith(_EXCEL_EPOCH_DATES)


# ── Schedule day-of-week parsing ──────────────────────────────────────────────
# Some customers define TWO (or more) XLSX rows for the SAME workflow name, each
# scoped to a different subset of the week (e.g. "Sun to Thu" for the main demand
# batch, "Fri, Sat" for a maintenance window). These must NOT collapse into one
# row — each day's Ctrl-M run needs to be judged against its OWN contracted
# window/anchors. _parse_schedule_days() extracts which weekdays a Schedule cell
# actually applies to, so callers can route each run_date to the right row.
_DAY_ALIASES = {
    "SUNDAY": "SUN", "MONDAY": "MON", "TUESDAY": "TUE", "WEDNESDAY": "WED",
    "THURSDAY": "THU", "FRIDAY": "FRI", "SATURDAY": "SAT",
}
_DAY_ORDER_SUN_START = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
_DAY_TO_PY_WEEKDAY = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
# Full names first in the alternation — "(?:SUN|...)(?:DAY)?" silently failed
# on TUESDAY/WEDNESDAY/THURSDAY/SATURDAY (their full spelling isn't the 3-letter
# code + literal "DAY", e.g. "SAT" + "DAY" != "SATURDAY") while happening to
# work for SUNDAY/MONDAY/FRIDAY, which are. Real bug: a customer's "Saturday"-
# scheduled SLA row silently parsed to schedule_days=None (no weekday
# restriction) instead of {5}, breaking any weekday-based disambiguation.
_DAY_NAMES_ALT = "SUNDAY|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUN|MON|TUE|WED|THU|FRI|SAT"
_ORDINAL_DAY_RE = re.compile(
    r"\b(?:1ST|2ND|3RD|4TH|5TH|FIRST|SECOND|THIRD|FOURTH|FIFTH|LAST|OTHER)\s+"
    rf"(?:{_DAY_NAMES_ALT})\b"
)
_DAY_RANGE_RE = re.compile(
    rf"\b({_DAY_NAMES_ALT})\s*(?:TO|[\-\u2013])\s*"
    rf"({_DAY_NAMES_ALT})\b"
)
_DAY_TOKEN_RE = re.compile(rf"\b({_DAY_NAMES_ALT})\b")

# Strips a trailing human-readable annotation some customers append to a
# sentinel job cell, e.g. "SCPO_W2_001 (Disable User)" -> "SCPO_W2_001".
# Ctrl-M Job_Name values never carry this, so leaving it in breaks any
# exact-match anchor lookup against real run data.
_TRAILING_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _parse_schedule_days(schedule_text: str) -> Optional[frozenset]:
    """Return the set of Python weekday ints (Mon=0..Sun=6) a Schedule cell
    applies to, or None when the text doesn't specify a day-of-week subset
    (blank, "Daily", or an nth-weekday-of-month phrase like "Last Sunday").

    None means "applies every day" — callers must treat that as no restriction,
    which is also how a customer's single-schedule-row workflow behaves today.
    """
    if not schedule_text:
        return None
    txt = schedule_text.upper().strip()
    if _ORDINAL_DAY_RE.search(txt):
        return None   # nth-weekday-of-month, not a weekly day-of-week subset
    m = _DAY_RANGE_RE.search(txt)
    if m:
        start, end = _DAY_ALIASES.get(m.group(1), m.group(1)), _DAY_ALIASES.get(m.group(2), m.group(2))
        si, ei = _DAY_ORDER_SUN_START.index(start), _DAY_ORDER_SUN_START.index(end)
        days = (_DAY_ORDER_SUN_START[si:ei + 1] if si <= ei
                else _DAY_ORDER_SUN_START[si:] + _DAY_ORDER_SUN_START[:ei + 1])
        return frozenset(_DAY_TO_PY_WEEKDAY[d] for d in days)
    found = _DAY_TOKEN_RE.findall(txt)
    if found:
        return frozenset(_DAY_TO_PY_WEEKDAY[_DAY_ALIASES.get(d, d)] for d in found)
    return None


# ── Batch-type inference ──────────────────────────────────────────────────────

# Fallback static patterns used only when pe_config is unavailable
_STATIC_TYPE_PATTERNS: list[tuple[str, list[str]]] = [
    ("BIWEEKLY",    ["BIWEEKLY", "BI_WEEKLY", "BI-WEEKLY"]),
    ("QUARTERLY",   ["QUARTERLY", "QUATERLY", "QTR"]),
    ("MONTHLY",     ["MONTHLY", "MLY"]),
    ("WEEKLY",      ["WEEKLY", "WLY", "_WF", "-WF"]),
    ("SEQUENCING",  ["SEQUENCING", "SEQUENC", "SEQ_BATCH", "SEQ_RUN"]),
    ("OUTBOUND",    ["OUTBOUND"]),
    ("CYCLIC",      ["CYCLIC", "_CYC"]),
    ("DAILY",       ["DAILY", "DLY", "EVERY DAY", "EVERYDAY", "NIGHTLY", "OVERNIGHT"]),
]

# Resolution priority order for detect_batch_type()
_DETECT_PRIORITY = ["ADHOC", "BIWEEKLY", "QUARTERLY", "MONTHLY", "WEEKLY", "SEQUENCING", "OUTBOUND", "CYCLIC", "DAILY"]


def _strip_env_prefix(name: str) -> str:
    """Strip known environment prefixes (PROD_, TEST_, UAT_, etc.) from a job name."""
    try:
        from services import pe_config
        prefixes = pe_config.ENV_PREFIXES_TO_STRIP
    except Exception:
        prefixes = ["PROD_", "TEST_", "UAT_", "DEV_", "STG_"]
    upper = name.upper()
    for pfx in prefixes:
        if upper.startswith(pfx.upper()):
            return name[len(pfx):]
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
    # SEQUENCING: "Daily Sequencing", "PROD_SEQUENCING", "SEQ_RUN"
    # Must be checked BEFORE generic DAILY so distinct sequencing windows keep
    # their own SLA ceiling and are not merged into the main daily batch window.
    if re.search(r'SEQUENC', _all_text, re.IGNORECASE):
        return "SEQUENCING"

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
        # Monthly / nth-weekday-of-month phrasing MUST be checked BEFORE the
        # generic day-name shortcut below. Otherwise schedules like
        # "Last Sunday of Month" or "1st Sunday" / "2nd Sunday" / "Other Sunday"
        # (genuinely once-per-month cadences) get misclassified as WEEKLY just
        # because they contain a weekday name — silently applying the wrong
        # SLA default and mislabeling the Type column for any customer whose
        # BatchSLA_info.xlsx uses nth-weekday-of-month scheduling.
        if re.search(r"\bOF\s+(?:EACH\s+|THE\s+)?MONTH\b", _sched_up):
            return "MONTHLY"
        if re.search(
            r"\b(?:1ST|2ND|3RD|4TH|5TH|FIRST|SECOND|THIRD|FOURTH|FIFTH|LAST|OTHER)\s+"
            rf"(?:{_DAY_NAMES_ALT})\b",
            _sched_up,
        ):
            return "MONTHLY"
        # Multi-day range spanning >1 weekday (e.g. "Sun to Fri", "Mon to Sat")
        # is a DAILY cadence (runs most days of the week), not a once-a-week
        # WEEKLY schedule — must be checked before the single-day-name shortcut.
        if re.search(
            rf"\b(?:{_DAY_NAMES_ALT})\s*(?:TO|[\-–])\s*"
            rf"(?:{_DAY_NAMES_ALT})\b",
            _sched_up,
        ):
            return "DAILY"
        # "Runs Every Saturday/Sunday" → WEEKLY; "Mon-Fri" → DAILY
        if any(d in _sched_up for d in ("SATURDAY", "SUNDAY", "SAT", "SUN")):
            return "WEEKLY"
        if re.match(r"^MON[\s\-]*FRI", _sched_up):
            return "DAILY"
        # "Runs every Monday" / "Every Tuesday & Wednesday" — any single named
        # weekday in the schedule text is a once-a-week cadence. There is no
        # principled reason one weekday would differ from another here — a
        # batch that runs every Tuesday is exactly as WEEKLY as one that runs
        # every Monday, so all seven map to the same type.
        _DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
        for _day in _DAYS:
            if _day in _sched_up:
                return "WEEKLY"

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
    # "H:MM:SS hrs" / "HH:MM:SS hrs." / "H:MM hrs" — a clock-style duration
    # combined with a unit suffix (CCBA: "06:00:00 hrs."). MUST be checked
    # BEFORE the generic single-number "hrs" regex below — that regex is
    # unanchored and greedily matches whichever digit run sits immediately
    # before "hrs", which for "06:00:00 hrs." is the trailing seconds field
    # ("00"), silently returning 0.0 instead of 6.0.
    m = re.match(r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*h[ro]?u?r?s?\.?\s*$', s)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        return round(hh + mm / 60 + ss / 3600, 4)
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
    # Strip a leading weekday name (and connector/filler words) before matching
    # a clock time — any customer's Start Time column may state the cadence
    # inline with the time, e.g. "Sunday 9:05 PM CST", "Monday/Wednesday 9:05 PM
    # CST", "Saturday start at 2PM CST". Without this, strptime fails on the
    # leading day-name text and the whole cell is silently dropped as None.
    # Full names MUST be checked before abbreviations (TUE+DAY != TUESDAY).
    s = re.sub(
        rf'^(?:(?:{_DAY_NAMES_ALT})[\s/,&\-]*)+(?:START\s+AT\s+)?',
        "", s, flags=re.IGNORECASE,
    ).strip()
    if not s:
        return None
    # Normalise NO-BREAK / NARROW NO-BREAK spaces from Excel/Word exports
    # (e.g. CCBA "09:00\u202fPM SAST") so strptime can parse the clock time.
    s = s.replace("\xa0", " ").replace("\u202f", " ").strip()

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

_BATCH_SLA_SCHEMA_VERSION = "2"

# Strict v1 registry.  Each entry is an observed header form backed by either
# the supplied BatchSLA sheet or a direct regression fixture.  Do not add
# "reasonable" synonyms here: an unverified schema must be rejected with a
# mapping report, never guessed into a contract calculation.
_BATCH_SLA_FIELDS: dict[str, dict[str, Any]] = {
    "batch_name": {
        "required": True, "internal": "Batch_Name", "aliases": {
            "batch name": "provided BatchSLA header",
        },
    },
    "schedule": {
        "required": False, "internal": "Schedule", "aliases": {
            "schedule": "provided BatchSLA header",
            "frequency": "Batch_SLA.xlsx",
        },
    },
    "timezone": {
        "required": False, "internal": "TimeZone", "aliases": {
            "timezone": "provided BatchSLA header",
        },
    },
    "module": {
        "required": False, "internal": None, "aliases": {
            "module": "provided BatchSLA header",
        },
    },
    "start_time": {
        "required": True, "internal": "Start_Time", "aliases": {
            "start time": "provided BatchSLA header",
        },
    },
    "first_job_name": {
        "required": False, "internal": "First_Job", "aliases": {
            "first job name": "provided BatchSLA header",
        },
    },
    # This is one contract fact with two supported value shapes: an elapsed
    # duration (Expected SLA / SLA Hours) or a clock deadline (Expected End
    # Time/SLA).  The existing parser keeps that value distinction intact.
    "expected_end_sla": {
        "required": True, "internal": None, "aliases": {
            "expected end time/sla": "provided BatchSLA header",
            "expected sla (hrs)": "tests/test_sla_header_variants.py",
            "sla(in hrs)": "tests/test_sla_header_variants.py",
            "sla (in hours)": "tests/test_sla_header_variants.py",
            "sla (minutes)": "tests/test_sla_header_variants.py",
            "sla hours": "tests/test_sla_header_variants.py",
            "sla": "tests/test_sla_header_variants.py",
            "end time": "Batch_SLA.xlsx",
        },
    },
    # A declared schedule duration is contract evidence, not an observed run.
    # Keep it separately from Current End Time so the UI cannot accidentally
    # present a planned six-hour window as a six-hour measured execution.
    "contract_duration": {
        "required": False, "internal": "Contract_Duration", "aliases": {
            "duration": "Batch_SLA.xlsx",
        },
    },
    "last_job_name": {
        "required": False, "internal": "Last_Job", "aliases": {
            "last job name": "provided BatchSLA header",
        },
    },
    "current_end_time": {
        "required": False, "internal": "End_Time", "aliases": {
            "current end time": "provided BatchSLA header",
        },
    },
    "comments": {
        "required": False, "internal": None, "aliases": {
            "comments": "provided BatchSLA header",
            "comment": "Batch_SLA.xlsx",
        },
    },
}


def _normalize_col_header(col: str) -> str:
    """Normalize only case, whitespace, and underscores for schema matching."""
    return re.sub(r"[\s_]+", " ", str(col or "").strip().casefold()).strip()


def _batch_sla_mapping_report(df_columns: list[str], sheet_name: str | None = None) -> dict[str, Any]:
    """Build a deterministic BatchSLA schema report without choosing aliases."""
    raw_headers = [str(col) for col in df_columns]
    normalized: dict[str, list[str]] = {}
    for raw in raw_headers:
        normalized.setdefault(_normalize_col_header(raw), []).append(raw)

    mapped: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    used_headers: set[str] = set()
    canonical_to_raw: dict[str, str] = {}
    for canonical, definition in _BATCH_SLA_FIELDS.items():
        matches: list[str] = []
        for alias in definition["aliases"]:
            matches.extend(normalized.get(_normalize_col_header(alias), []))
        # Same raw header cannot appear twice in aliases, but an Excel sheet can
        # contain duplicated raw headers or two aliases for one canonical field.
        matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            duplicates.append({"canonical_field": canonical, "raw_headers": matches})
            continue
        if matches:
            raw = matches[0]
            canonical_to_raw[canonical] = raw
            used_headers.add(raw)
            mapped.append({
                "raw_header": raw,
                "canonical_field": canonical,
                "required": bool(definition["required"]),
                "provenance": definition["aliases"][_normalize_col_header(raw)],
            })

    missing_required = [
        canonical for canonical, definition in _BATCH_SLA_FIELDS.items()
        if definition["required"] and canonical not in canonical_to_raw
    ]
    absent_optional = [
        canonical for canonical, definition in _BATCH_SLA_FIELDS.items()
        if not definition["required"] and canonical not in canonical_to_raw
    ]
    return {
        "sheet_name": sheet_name,
        "raw_headers": raw_headers,
        "mapped": mapped,
        "missing_required": missing_required,
        "absent_optional": absent_optional,
        "duplicates": duplicates,
        "unmapped_headers": [raw for raw in raw_headers if raw not in used_headers],
        "canonical_to_raw": canonical_to_raw,
        "status": "blocked" if missing_required or duplicates else "accepted",
    }


def _with_field_population(report: dict[str, Any], df: Any) -> dict[str, Any]:
    """Attach file-wide populated/empty state without changing source values."""
    field_states: list[dict[str, Any]] = []
    for canonical, definition in _BATCH_SLA_FIELDS.items():
        raw = report["canonical_to_raw"].get(canonical)
        if not raw:
            field_states.append({"canonical_field": canonical, "state": "field_absent_in_source"})
            continue
        series = df[raw]
        populated = int((series.notna() & series.astype(str).str.strip().ne("")).sum())
        empty = int(len(series) - populated)
        field_states.append({
            "canonical_field": canonical,
            "state": "mapped_populated" if populated else "mapped_empty_for_all_rows",
            "populated_rows": populated,
            "empty_rows": empty,
        })
    report["field_states"] = field_states
    return report


def _execution_history_profile(df_columns: list[str]) -> dict[str, str] | None:
    """Recognise a dated batch-runtime history without mistaking it for SLA.

    This is deliberately a deterministic *profile*, not fuzzy matching.  A
    history workbook has a batch/workflow label, actual Start and End columns,
    and an explicitly named total runtime, but no expected/SLA/deadline field.
    Such a file belongs to Batch Review.  It cannot establish a customer SLA
    contract merely because its actual End Time looks like an SLA end time.
    """
    normalized = {_normalize_col_header(raw): str(raw) for raw in df_columns}
    batch = next((normalized[key] for key in ("batch", "workflow", "workflow name", "batch type") if key in normalized), None)
    start = next((normalized[key] for key in ("start time", "start date", "start") if key in normalized), None)
    end = next((normalized[key] for key in ("end time", "end date", "end") if key in normalized), None)
    total_runtime = next((normalized[key] for key in (
        "total batch time", "total batch runtime", "total runtime", "batch runtime", "batch elapsed",
    ) if key in normalized), None)
    has_contract_target = any(
        "sla" in header or "expected" in header or "deadline" in header or "target" in header
        for header in normalized
    )
    if batch and start and end and total_runtime and not has_contract_target:
        return {
            "batch_field": batch,
            "start_field": start,
            "end_field": end,
            "runtime_field": total_runtime,
        }
    return None


def _header_declares_sla_duration(col: str) -> bool:
    """Return True when an SLA header explicitly declares duration units.

    A bare ``SLA`` column is intentionally treated as a possible clock-time
    deadline. Headers such as ``SLA(in Hrs)`` are different: the unit makes
    the customer's intent unambiguous and the cells must be parsed as elapsed
    hours/minutes. This runs before normalization can erase that evidence.
    """
    raw = re.sub(r"[_\-]+", " ", str(col or "").strip().lower())
    if not re.search(r"\bsla\b", raw):
        return False
    return bool(re.search(r"\b(?:h(?:ou)?rs?|hours?|mins?|minutes?)\b", raw))


def _value_declares_sla_duration(value: Any) -> bool:
    """True only when a cell explicitly carries an hour/minute unit.

    This safely disambiguates a bare ``SLA`` column row by row: ``8Hrs`` is a
    duration, while ``7:00 PM`` remains a deadline. Unitless values stay on the
    existing header-driven path because their meaning cannot be proven.
    """
    try:
        import pandas as _pd
        if _pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, Number) and not isinstance(value, bool):
        # Excel clock-time serials are fractions of a day (0 < value < 1).
        # A whole/decimal number >= 1 in a bare SLA column is therefore an
        # elapsed-hour duration, not an Excel time-of-day value.
        return float(value) >= 1
    raw = str(value or "").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw) >= 1
    return bool(re.search(
        r"(?<![a-z])(?:h(?:ou)?rs?|hours?|mins?|minutes?)\b",
        raw,
    ))


def _map_columns(df_columns: list[str]) -> dict[str, str]:
    """Return legacy parser keys from the accepted strict schema mapping.

    ``_parse_sheet_workflows`` still consumes the historical internal key names
    so the SLA/Buffer/Status calculation path stays unchanged.  All header
    ambiguity is decided before this adapter is called.
    """
    report = _batch_sla_mapping_report(df_columns)
    mapping: dict[str, str] = {}
    for canonical, raw in report["canonical_to_raw"].items():
        internal = _BATCH_SLA_FIELDS[canonical]["internal"]
        if internal:
            mapping[internal] = raw

    target = report["canonical_to_raw"].get("expected_end_sla")
    if target:
        # A known duration header remains a duration.  The bare "SLA" form is
        # intentionally routed through the existing deadline/value logic so a
        # value such as "8Hrs" still works while "07:00" stays a deadline.
        target_norm = _normalize_col_header(target)
        if target_norm != "sla" and _header_declares_sla_duration(target):
            mapping["Expected_SLA"] = target
        elif target_norm.startswith("expected sla"):
            mapping["Expected_SLA"] = target
        else:
            mapping["Expected_End_Time"] = target
    return mapping


def _overnight_delta_hours(start_val: Any, end_val: Any) -> Optional[float]:
    """Compute elapsed hours between two time-of-day values, handling overnight crossing.

    e.g. start=21:00, end=01:00 → 4.0h  (not -20h)
         start=21:00, end=03:00 → 6.0h
         start=08:30, end=11:50 → 3.33h
    Accepts pandas Timestamp, datetime.time, or string like '21:00:00' / '01:00' /
    '6:30 am EST' / '8.30 pm EST' (timezone suffix stripped before parsing).
    Returns None if either value is null or unparseable.
    Sanity cap: delta > 18h is almost certainly a data error, return None.
    """
    import pandas as _pd

    def _clean(v: Any) -> str:
        """Strip day-name prefix, timezone suffix, normalise dot-as-colon, return
        clean time string.

        Real customer files (e.g. USF) put a day-of-week name/phrase directly in
        the Start_Time cell alongside the clock time: "Sunday 9:05 PM CST",
        "Saturday start at 2PM CST", "Monday/Wednesday 9:05 PM CST". Without
        stripping this, pd.to_datetime("Sunday 9:05 PM") returns NaT for every
        such row, silently discarding a real, computable per-workflow SLA window
        in favour of a generic batch-type default.
        """
        s = str(v).strip() if v is not None else ""
        if not s or s.lower() in ("nan", "none", "nat"):
            return ""
        import re as _re
        # Excel/Word exports embed NO-BREAK (\xa0) and NARROW NO-BREAK (\u202f)
        # spaces between the time and its AM/PM or timezone (e.g. CCBA:
        # "09:00\u202fPM SAST\u202f\xa0"). pandas can't parse a time containing
        # those, so normalise them to plain spaces before anything else.
        s = s.replace("\xa0", " ").replace("\u202f", " ").strip()
        # Strip a leading day-of-week name/phrase (handles "Monday/Wednesday ",
        # "Saturday start at ", "Sunday "), and repeated day tokens joined by
        # "/", "-", "," or "&". Iterative: keeps stripping while a day-name
        # (optionally followed by filler like "start at"/"at") remains at the front.
        _DAY = r'(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sun|Mon|Tue|Wed|Thu|Fri|Sat)'
        while True:
            s2 = _re.sub(
                rf'^{_DAY}(?:\s*[/\-,&]\s*{_DAY})*\s*(?:start\s+at\s+|at\s+)?',
                '', s, flags=_re.IGNORECASE,
            ).strip()
            if s2 == s:
                break
            s = s2
        # Strip trailing timezone qualifiers: "EST", "CST", "IST", "SAST",
        # "UTC+5:30", etc. Whitelisted (never a bare [A-Z]{2,4} catch-all) so a
        # real "AM"/"PM" is never mistaken for a timezone and stripped.
        s = _re.sub(
            r'\s+(?:CST|CDT|EST|EDT|PST|PDT|MST|MDT|IST|GMT|UTC[+-]?\d*'
            r'|SAST|CET|CEST|WET|WEST|EET|EEST|BST|WAT|CAT|EAT'
            r'|AEST|AEDT|ACST|ACDT|AWST|NZST|NZDT'
            r'|JST|KST|SGT|HKT|PHT|MYT|WIB|WITA|WIT|ICT|MMT|NPT|PKT|BDT|SLST'
            r'|MSK|GST|BRT|ART|CLT|COT|PET|VET)\s*$',
            '', s, flags=_re.IGNORECASE,
        ).strip()
        # Strip parenthetical notes like "(next day)"
        s = _re.sub(r'\s*\([^)]*\)', '', s).strip()
        # Normalise "8.30 pm" → "8:30 pm" (dot used as colon separator)
        s = _re.sub(r'^(\d{1,2})\.(\d{2})\s*([AaPp][Mm])', r'\1:\2 \3', s)
        # Malformed data-entry pattern: a 24-hour numeral (13-23) paired with a
        # 12-hour meridiem suffix, e.g. "17:30 AM" (the source meant 5:30 AM —
        # "17" is a stray 24-hour habit, "AM" is the intended half). pandas
        # cannot parse this combination at all (returns NaT), silently dropping
        # the row's own last-run duration. Normalize hour%12 so the stated
        # meridiem still applies, instead of discarding the value entirely.
        _mh = _re.match(r'^(\d{1,2})(:\d{2}(?::\d{2})?)\s*([AaPp][Mm])$', s)
        if _mh and int(_mh.group(1)) > 12:
            s = f"{int(_mh.group(1)) - 12}{_mh.group(2)} {_mh.group(3).upper()}"
        return s

    try:
        sc = _clean(start_val)
        ec = _clean(end_val)
        if not sc or not ec:
            return None
        st = _pd.to_datetime(sc, errors="coerce")
        et = _pd.to_datetime(ec, errors="coerce")
        if _pd.isna(st) or _pd.isna(et):
            return None
        st_h = st.hour + st.minute / 60 + st.second / 3600
        et_h = et.hour + et.minute / 60 + et.second / 3600
        delta = et_h - st_h
        if delta < 0:
            delta += 24.0  # overnight crossing
        # Sanity: >23h delta almost certainly means a data error (e.g. wrong
        # date paired with wrong time).  Return None so fallback SLA kicks in.
        if delta > 23.0:
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
    # Contract_Duration is a declared schedule window from a workbook such as
    # Batch_SLA.xlsx.  It is deliberately *not* an actual runtime: only a
    # source column explicitly mapped as Current End Time can establish an
    # observed completion for this workbook-only SLA matrix.
    contract_dur_series = _col(df, "Contract_Duration", optional=True)

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
        # Strip a trailing human-readable annotation some customers add to the
        # sentinel cell (e.g. "SCPO_W2_001 (Disable User)") — Ctrl-M Job_Name
        # values never carry this, so leaving it in means the anchor can NEVER
        # exact-match a real run and every anchor-based feature (window
        # narrowing, schedule-qualifier disambiguation) silently falls back to
        # "no anchor matched" instead of the real job.
        first_jobs = [_TRAILING_QUALIFIER_RE.sub("", j).strip() for j in first_jobs]
        last_jobs  = [_TRAILING_QUALIFIER_RE.sub("", j).strip() for j in last_jobs]
        is_parallel = len(first_jobs) > 1 or len(last_jobs) > 1
        # Store primary sentinel (first element) for backward compat;
        # full lists stored as first_jobs_list / last_jobs_list
        first_job = first_jobs[0] if first_jobs else ""
        last_job  = last_jobs[0]  if last_jobs  else ""

        sla_raw = sla_series.iloc[idx] if sla_series is not None else None
        sla_h   = parse_sla_hours(sla_raw)
        sla_end_time_raw: Optional[str] = None   # clock-time deadline (e.g. "07:00") when applicable

        # A bare "SLA" header can legitimately contain either a duration or a
        # clock deadline across customer templates. Explicit units in the cell
        # settle that ambiguity without guessing: "8Hrs"/"90 min" are durations;
        # "7:00 PM" continues through the clock-time path below.
        if sla_h is None and expected_end_series is not None:
            try:
                _candidate = expected_end_series.iloc[idx]
                if _value_declares_sla_duration(_candidate):
                    sla_h = parse_sla_hours(_candidate)
            except Exception:
                pass

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

        # ── Contracted Start Time (Gap fix: start-time compliance) ──────────
        # The SLA Matrix historically only measured DURATION (did the batch finish
        # within its window once started) and never checked whether it STARTED on
        # time. A batch that starts hours late but finishes within its allotted
        # duration looked healthy even though downstream data arrives stale. Parse
        # the contracted Start_Time here (generic, any customer's clock format via
        # parse_start_time) so routers/sla_matrix.py can compare it against the
        # actual first-job start observed in Ctrl-M.
        contract_start_raw = start_series.iloc[idx] if start_series is not None else None
        try:
            _cst = parse_start_time(contract_start_raw)
            # A cell can hold two start times (twice-daily batch) — keep the
            # first for the single-value comparison, list stays available via raw.
            if isinstance(_cst, list):
                _cst = _cst[0] if _cst else None
            contract_start_time = _cst.strftime("%H:%M:%S") if _cst else None
        except Exception:
            contract_start_time = None

        # ── Workbook timing evidence ──────────────────────────────────────────
        # `End_Time` is mapped only from a customer column explicitly named
        # "Current end time". Its difference from Start_Time is therefore a
        # workbook-reported completion window. A separately named `Duration`
        # column is a declared schedule duration, so it is kept as contract
        # evidence and must never be promoted to a measured runtime.
        window_h: Optional[float] = None
        _win_dated = False
        try:
            if start_series is not None and end_series is not None:
                st_val = start_series.iloc[idx]
                en_val = end_series.iloc[idx]
                if st_val and en_val:
                    _win_dated = _looks_dated(st_val) or _looks_dated(en_val)
                    _wd = _overnight_delta_hours(st_val, en_val)
                    if _wd is not None and _wd > 0:
                        window_h = _wd
        except Exception:
            pass

        contract_duration_h: Optional[float] = None
        if contract_dur_series is not None:
            try:
                dur_val = contract_dur_series.iloc[idx]
                if dur_val is not None:
                    _dh = parse_sla_hours(dur_val)
                    if _dh is not None and _dh > 0:
                        contract_duration_h = _dh
            except Exception:
                pass

        # A workbook can provide a clock window (Start time -> End time) and a
        # separately declared Duration. They are two statements about the same
        # contract, so silently selecting one when they disagree would invent a
        # customer SLA. One minute only absorbs source precision/rounding; it
        # never guesses which value is authoritative.
        clock_contract_h: Optional[float] = None
        contract_conflict = False
        contract_conflict_detail: Optional[str] = None
        if not _has_sla_col and expected_end_series is not None and start_series is not None:
            try:
                _expected_clock = expected_end_series.iloc[idx]
                _contract_start = start_series.iloc[idx]
                if _expected_clock and _contract_start:
                    clock_contract_h = _overnight_delta_hours(_contract_start, _expected_clock)
            except Exception:
                pass
        if (
            clock_contract_h is not None
            and contract_duration_h is not None
            and abs(clock_contract_h - contract_duration_h) > (1 / 60)
        ):
            contract_conflict = True
            contract_conflict_detail = (
                f"End Time implies {clock_contract_h:.3f}h from Start Time, but "
                f"Duration declares {contract_duration_h:.3f}h. No SLA was selected."
            )
            warnings.append(f"Row {idx} '{batch_name}': {contract_conflict_detail}")

        # A workbook explicitly names `Current end time` as a completion field.
        # It remains an input to the workbook-only calculation even when it
        # matches Expected End Time/SLA.  Equality is a source-quality caveat,
        # not a reason to discard a supplied end time: the reviewer can see the
        # zero headroom result and decide whether the source is a template copy.
        _reported_end_equals_target = False
        if (
            sla_h is not None and window_h is not None
            and end_series is not None and expected_end_series is not None
        ):
            _end_cmp = str(end_series.iloc[idx] or "").strip().lower()
            _exp_cmp = str(expected_end_series.iloc[idx] or "").strip().lower()
            if _end_cmp and _exp_cmp and _end_cmp == _exp_cmp:
                _reported_end_equals_target = True

        # `actual_h` is the only number eligible for Buffer/Headroom/Status.
        # It comes solely from workbook Start Time -> explicitly named Current
        # End Time. Contract fields are never substituted when Current end time
        # is absent; an explicitly supplied Current end time is always measured.
        actual_h: Optional[float] = window_h

        # True only when sla_h below came from a bare Start/End window (or a
        # Duration-only column) with NO explicit "Expected"/"SLA"-named column
        # anywhere in the file — the dashboard INFERRED the target rather than
        # reading a column the customer explicitly labelled as the SLA.
        # Surfaced via sla_confidence so the UI never claims machine-verified
        # certainty for something that required interpretation.
        _sla_inferred_from_bare_window = False

        # ── Do NOT promote a bare Start/End window or Duration column to the SLA ──
        # A customer's Start Time + End Time + Duration columns tell you how a
        # batch is scheduled to run or how long it took — neither one is the
        # same fact as "the contracted target the customer agreed to". Without a
        # column the customer explicitly labelled as the target ("Expected SLA",
        # "Expected End Time", "SLA", "SLA Deadline"), the dashboard does not
        # know what a compliant runtime actually is: a 6h window observed today
        # could have a 30-minute SLA or a 4-hour SLA — the file simply does not
        # say. Populating a confident "6h CONTRACT" badge from that window alone
        # was over-inference; sla_h stays None here and resolves to the PE
        # default further below, clearly tagged UNVERIFIED/DEFAULT so a reviewer
        # is told plainly this file cannot answer the SLA question, rather than
        # being shown a manufactured number. `sla_schema` records WHY — the
        # dashboard's own diagnostic of "what kind of file is this", exposed per
        # row (traceable) and aggregated file-wide below in parse_batch_sla_xlsx.
        if contract_conflict:
            _sla_schema = "CLOCK_DURATION_CONFLICT"
            sla_h = None
        elif sla_h is not None:
            _sla_schema = "EXPLICIT_COLUMN"
        elif window_h is not None and not _win_dated:
            # Bare, undated Start/End window with no "Expected" column anywhere
            # — e.g. CCBA. This is a schedule window or an observed sample, NOT
            # a stated target. Left unresolved (falls to GLOBAL_DEFAULT below).
            _sla_schema = "WINDOW_NO_EXPECTED_COLUMN"
        elif contract_duration_h is not None and window_h is None:
            # Duration-only file, no "Expected" column anywhere — Duration is
            # how long the batch runs, not what was contracted. Left unresolved.
            _sla_schema = "DURATION_NO_EXPECTED_COLUMN"
        elif window_h is not None and _win_dated:
            # Dated Start/End with no explicit SLA column anywhere — an
            # observed-execution report (Wella), not a contract. sla_h stays
            # None here (resolved to GLOBAL_DEFAULT further below).
            _sla_schema = "OBSERVED_HISTORY"
        else:
            _sla_schema = "NO_SIGNAL"

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
        sla_source = "CONTRACT_CONFLICT" if contract_conflict else ("BATCH_SLA_XLSX" if sla_h is not None else None)
        if sla_h is not None and sla_source == "BATCH_SLA_XLSX":
            if sla_h < 0.1:
                warnings.append(
                    f"Row {idx} '{batch_name}': SLA={sla_h:.3f}h ({sla_h*60:.0f} min) "
                    f"seems too small — verify source. Falling back to defaults."
                )
                sla_h = _default_sla_for(btype)
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
        if sla_h is None and not contract_conflict:
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
        if sla_h is None and not contract_conflict:
            sla_h = _default_sla_for(btype)
            sla_source = "GLOBAL_DEFAULT"

        workflows.append({
            "workflow":           batch_name,
            "batch_type":         btype,
            "schedule":           schedule,
            "schedule_days":      (sorted(_parse_schedule_days(schedule))
                                    if _parse_schedule_days(schedule) else None),
            "timezone":           timezone,
            "first_job":          first_job,
            "last_job":           last_job,
            "first_jobs_list":    first_jobs if is_parallel else None,
            "last_jobs_list":     last_jobs  if is_parallel else None,
            "is_parallel":        is_parallel,
            "sla_hours":          sla_h,
            "sla_source":         sla_source,
            # VERIFIED   = the file has an explicit "Expected SLA"/"Expected End
            #              Time"/"SLA" column the customer labelled as the target
            #              (or a SOW ceiling) — no interpretation was required.
            # UNVERIFIED = no such explicit column exists anywhere in the file —
            #              a bare Start/End window or Duration column is NEVER
            #              treated as the SLA target (see sla_schema below); this
            #              status compares an observed sample against the
            #              generic PE default, not a confirmed customer target.
            "sla_confidence": (
                "CONFLICT" if contract_conflict else
                "VERIFIED" if sla_source in ("BATCH_SLA_XLSX", "SOW_EXTRACTED") else "UNVERIFIED"
            ),
            # Which file-shape case this row was classified as — the dashboard's
            # own diagnostic of "what kind of SLA data is this", not just a
            # confidence label. One of EXPLICIT_COLUMN / WINDOW_NO_EXPECTED_COLUMN
            # / OBSERVED_HISTORY / DURATION_NO_EXPECTED_COLUMN / NO_SIGNAL.
            # Aggregated file-wide in parse_batch_sla_xlsx to decide whether to
            # raise a critical "this file has no usable SLA data" warning.
            "sla_schema":         _sla_schema,
            # Retained for compatibility with older consumers.  Current end is
            # now an explicit measured source field, never suppressed solely
            # because it equals the contract target.
            "runtime_is_placeholder": False,
            "runtime_source_caveat": "REPORTED_END_EQUALS_TARGET" if _reported_end_equals_target else None,
            "sla_end_time":       sla_end_time_raw,   # clock-time deadline ("07:00") or None
            "sla_start_time":     contract_start_time, # contracted start-of-window clock time ("14:00:00") or None
            "last_run_hours_xlsx": actual_h,
            # Workbook-only provenance contract for the React SLA Matrix.
            # Never infer an actual run from a declared schedule duration.
            "workbook_start_time": _v(start_series) or None,
            "workbook_expected_end": _v(expected_end_series) or _v(sla_series) or None,
            "workbook_reported_end": _v(end_series) or None,
            "workbook_clock_window_hours": clock_contract_h,
            "workbook_contract_duration_hours": contract_duration_h,
            "contract_conflict": contract_conflict,
            "contract_conflict_detail": contract_conflict_detail,
            "workbook_timing_source": (
                "WORKBOOK_REPORTED_CURRENT_END_EQUALS_TARGET" if actual_h is not None and _reported_end_equals_target else
                "WORKBOOK_REPORTED_TIMESTAMP_PAIR" if actual_h is not None and _win_dated else
                "WORKBOOK_SCHEDULED_START_TO_REPORTED_END" if actual_h is not None else
                "WORKBOOK_COMPLETION_NOT_REPORTED"
            ),
            "compliance":         compliance_label(actual_h, sla_h),
            # Human-readable margin of the runtime against the SLA window, in
            # plain time units — e.g. "2h 0m early" / "15m over" / "on the edge".
            # Positive = finished before the window closed; negative = ran over.
            # None when there is no runtime to compare (UNKNOWN rows).
            "sla_margin_desc":    _format_sla_margin(sla_h, actual_h),
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

    # ── Validate every candidate schema before parsing or cache commit ─────────
    # A workbook may have cover/readme sheets. Only sheets that declare a
    # BatchSLA field are candidates; every candidate must satisfy the required
    # contract and have no canonical-field ambiguity.
    _dfs_to_process: list[tuple] = []   # (df, sheet_name, mapping_report)
    _mapping_sheets: list[dict[str, Any]] = []
    _execution_history_sheets: list[dict[str, Any]] = []
    try:
        if ext in ("xlsx", "xls"):
            xl = pd.ExcelFile(io.BytesIO(raw_bytes))
            for _sn in xl.sheet_names:
                try:
                    _df = xl.parse(_sn)
                    _df.columns = _df.columns.astype(str)
                    _report = _with_field_population(
                        _batch_sla_mapping_report(list(_df.columns), _sn), _df,
                    )
                    _history_profile = _execution_history_profile(list(_df.columns))
                    if _history_profile:
                        _report["execution_history_profile"] = _history_profile
                        _execution_history_sheets.append({"sheet_name": _sn, **_history_profile})
                    # An execution-history sheet also has a batch name, so it
                    # can superficially look like a BatchSLA candidate.  Its
                    # dated Start/End and Total Runtime fields, *without* an
                    # explicit target, prove a different file role.  Keep it
                    # out of the SLA-contract candidate set so it reaches the
                    # explicit Batch Review handoff below rather than being
                    # scored against a fabricated global default.
                    _report["included_in_ingestion"] = bool(
                        _report["canonical_to_raw"].get("batch_name")
                    ) and not _history_profile
                    _report["sheet_role"] = (
                        "execution_history" if _history_profile else
                        "sla_candidate" if _report["included_in_ingestion"] else "ignored_auxiliary"
                    )
                    _mapping_sheets.append(_report)
                    if _report["included_in_ingestion"]:
                        _dfs_to_process.append((_df, _sn, _report))
                except Exception as _se:
                    warnings.append(f"Sheet '{_sn}': read error ({_se})")
        else:
            _df = pd.read_csv(io.BytesIO(raw_bytes))
            _df.columns = _df.columns.astype(str)
            _report = _with_field_population(
                _batch_sla_mapping_report(list(_df.columns), filename), _df,
            )
            _history_profile = _execution_history_profile(list(_df.columns))
            if _history_profile:
                _report["execution_history_profile"] = _history_profile
                _execution_history_sheets.append({"sheet_name": filename, **_history_profile})
            _report["included_in_ingestion"] = bool(
                _report["canonical_to_raw"].get("batch_name")
            ) and not _history_profile
            _report["sheet_role"] = (
                "execution_history" if _history_profile else
                "sla_candidate" if _report["included_in_ingestion"] else "ignored_auxiliary"
            )
            _mapping_sheets.append(_report)
            if _report["included_in_ingestion"]:
                _dfs_to_process.append((_df, filename, _report))
    except Exception as exc:
        return {"workflows": [], "row_count": 0, "filename": filename,
                "columns_found": [], "source_sheet": None,
                "warnings": [f"Cannot read file: {exc}"],
                "ingestion_status": "blocked",
                "mapping_report": {"schema_version": _BATCH_SLA_SCHEMA_VERSION, "sheets": _mapping_sheets}}

    _candidate_reports = [
        report for report in _mapping_sheets
        if report.get("included_in_ingestion")
    ]
    _blocked_reports = [report for report in _candidate_reports if report["status"] == "blocked"]
    if not _candidate_reports:
        if _execution_history_sheets:
            return {
                "workflows": [], "row_count": 0, "filename": filename,
                "columns_found": [], "source_sheet": None,
                "warnings": [
                    "Execution-history workbook detected: it contains dated batch runs and a total runtime, "
                    "but no customer SLA/expected-completion field. Routed to Batch Review; no SLA contract was inferred."
                ],
                "ingestion_status": "reroute",
                "file_role": "batch_execution_history",
                "execution_history_sheets": _execution_history_sheets,
                "mapping_report": {
                    "schema_version": _BATCH_SLA_SCHEMA_VERSION,
                    "status": "reroute",
                    "sheets": _mapping_sheets,
                },
            }
        # Do not fall back to sheet 0: it made a non-BatchSLA document appear
        # accepted while producing an empty configuration.
        _blocked_reports = _mapping_sheets or [{
            "sheet_name": filename, "missing_required": ["batch_name", "start_time", "expected_end_sla"],
            "duplicates": [], "unmapped_headers": [],
        }]
    if _blocked_reports:
        missing = sorted({field for report in _blocked_reports for field in report.get("missing_required", [])})
        duplicates = [duplicate for report in _blocked_reports for duplicate in report.get("duplicates", [])]
        messages: list[str] = []
        if missing:
            messages.append(f"Missing required BatchSLA field(s): {', '.join(missing)}.")
        for duplicate in duplicates:
            messages.append(
                f"Ambiguous mapping for {duplicate['canonical_field']}: "
                f"{', '.join(duplicate['raw_headers'])}."
            )
        return {
            "workflows": [], "row_count": 0, "filename": filename,
            "columns_found": [], "source_sheet": None,
            "warnings": messages or ["BatchSLA schema validation failed."],
            "ingestion_status": "blocked",
            "mapping_report": {
                "schema_version": _BATCH_SLA_SCHEMA_VERSION,
                "status": "blocked",
                "sheets": _mapping_sheets,
            },
        }

    # Candidate sheets have been accepted. Ignore non-schema cover sheets.
    _dfs_to_process = [item for item in _dfs_to_process if item[2]["status"] == "accepted"]

    # ── Parse each sheet, deduplicate across sheets ────────────────────────────
    # Primary-normalized workflow name (strip env prefix → UPPER) is the dedup key.
    # A customer's run-history report commonly repeats the SAME workflow label
    # across many rows (one per historical execution date) — generic across
    # any customer, not specific to one file. When that happens, keep the row
    # with the WORST (largest) observed actual runtime, not just the first
    # occurrence — "first wins" silently discarded the customer's own
    # worst-case sample, understating risk in the wrong direction for an audit.
    _all_col_found: set[str] = set()
    _sheets_used: list[str] = []
    _pkey_index: dict[str, int] = {}   # pkey -> index into `workflows`
    _collapsed_counts: dict[str, int] = {}
    workflows: list[dict] = []

    for _df, _sheet_name, _sheet_report in _dfs_to_process:
        _all_col_found.update(_sheet_report["canonical_to_raw"].keys())
        _sh_wfs = _parse_sheet_workflows(_df, warnings, _sheet_name)
        _added = 0
        for wf in _sh_wfs:
            _wf_name = _strip_env_prefix(wf.get("workflow") or "").upper()
            # Dedup key includes the (normalized) Schedule text, not just the
            # workflow name — a customer can legitimately define TWO rows for
            # the SAME workflow name with different day-of-week schedules
            # (e.g. "Sun to Thu" main batch vs "Fri, Sat" maintenance window).
            # Keying on name alone silently dropped the second row.
            _sched_key = re.sub(r"\s+", " ", str(wf.get("schedule") or "").strip().upper())
            _pkey = f"{_wf_name}|{_sched_key}"
            if not _wf_name:
                continue
            if _pkey not in _pkey_index:
                _pkey_index[_pkey] = len(workflows)
                workflows.append(wf)
                _added += 1
            else:
                _collapsed_counts[_pkey] = _collapsed_counts.get(_pkey, 0) + 1
                _existing = workflows[_pkey_index[_pkey]]
                _new_h = wf.get("last_run_hours_xlsx")
                _old_h = _existing.get("last_run_hours_xlsx")
                if isinstance(_new_h, (int, float)) and (
                    not isinstance(_old_h, (int, float)) or _new_h > _old_h
                ):
                    workflows[_pkey_index[_pkey]] = wf
        if _added > 0:
            _sheets_used.append(_sheet_name)

    if _collapsed_counts:
        for _pkey, _n in _collapsed_counts.items():
            _wf_label = _pkey.split("|", 1)[0]
            warnings.append(
                f"Workflow '{_wf_label}': {_n} additional historical row(s) with the "
                "same workflow/schedule were found — the worst (longest) observed "
                "runtime among them was kept for buffer/status, the rest were "
                "collapsed. Upload a file with one row per contracted SLA "
                "definition (not per execution date) for a precise ceiling."
            )

    _explicit = sum(1 for w in workflows if w.get("sla_source") == "BATCH_SLA_XLSX")
    _fallback = sum(1 for w in workflows if w.get("sla_source") in ("SOW_EXTRACTED", "GLOBAL_DEFAULT"))
    if _fallback > 0 and _explicit == 0:
        warnings.append(
            f"XLSX has no SLA column — applied Tier 2 (SOW) / Tier 3 (defaults) "
            f"for {_fallback} workflow(s). Upload a file with an 'Expected SLA' "
            f"or 'SLA' column for explicit per-workflow targets."
        )

    # ── File-shape diagnostic: what kind of SLA data does this file actually
    # contain, and is any of it usable? Aggregated from the per-row sla_schema
    # tag so this works for ANY customer's column naming, not just the ones
    # seen so far — the classification is driven by which canonical columns
    # were found (Batch_Name/Start_Time/End_Time/Expected_SLA/Expected_End_
    # Time/Actual_Duration), not by hardcoded per-customer logic.
    _schema_counts: dict[str, int] = {}
    for w in workflows:
        _s = w.get("sla_schema") or "NO_SIGNAL"
        _schema_counts[_s] = _schema_counts.get(_s, 0) + 1
    _no_signal = _schema_counts.get("NO_SIGNAL", 0)
    _window_only = _schema_counts.get("WINDOW_NO_EXPECTED_COLUMN", 0)
    _duration_only = _schema_counts.get("DURATION_NO_EXPECTED_COLUMN", 0)
    _contract_conflicts = _schema_counts.get("CLOCK_DURATION_CONFLICT", 0)
    if workflows and _no_signal == len(workflows):
        warnings.append(
            f"CRITICAL: none of the {len(workflows)} workflow(s) in this file have "
            f"ANY usable SLA/runtime signal. Searched for — an explicit target "
            f"('Expected SLA'/'Expected End Time'/'SLA' column), a Start Time + "
            f"End Time window, or a Duration/'Total batch time' column — and "
            f"found none. Columns actually detected: {sorted(_all_col_found) or '(none)'}. "
            f"Every workflow will use the generic PE default threshold until a "
            f"file containing one of these is uploaded — confirm the file "
            f"format with the customer."
        )
    elif workflows and _no_signal > 0:
        warnings.append(
            f"{_no_signal} of {len(workflows)} workflow(s) have no usable SLA/"
            f"runtime signal (no Expected SLA, no Start+End window, no Duration "
            f"column for that specific row) and will use the generic PE default."
        )
    if _window_only + _duration_only > 0:
        warnings.append(
            f"CRITICAL: {_window_only + _duration_only} workflow(s) have a Start "
            f"Time/End Time window and/or a Duration column, but NO column the "
            f"customer explicitly labelled as the SLA target ('Expected SLA', "
            f"'Expected End Time', 'SLA', 'SLA Deadline'). A Start/End window or "
            f"a Duration figure tells you how long a batch runs or is scheduled "
            f"to run — it does NOT tell you what was contracted as acceptable "
            f"(a 6h observed window could have a 30-minute SLA or a 4-hour SLA; "
            f"the file does not say). No SLA was populated for these — they use "
            f"the generic PE default, clearly flagged, until the correct file "
            f"(with an explicit target column) is provided."
        )
    if _contract_conflicts:
        warnings.append(
            f"CRITICAL: {_contract_conflicts} workflow(s) contain conflicting Start Time/End Time "
            "and Duration contract values. No SLA was propagated for those rows; resolve the "
            "source contract before using them for compliance."
        )

    _equal_target_count = sum(1 for w in workflows if w.get("runtime_source_caveat") == "REPORTED_END_EQUALS_TARGET")
    if _equal_target_count > 0:
        warnings.append(
            f"{_equal_target_count} workflow(s) have a 'Current end time' equal to "
            f"'Expected End Time/SLA'. Runtime, headroom, buffer, and status use the "
            f"supplied Current end time; verify that it is an actual completion rather "
            f"than a template target copied into the source field."
        )

    return {
        "workflows": workflows,
        "row_count": len(workflows),
        "filename":  filename,
        "columns_found": list(_all_col_found),
        "source_sheet": ", ".join(_sheets_used) if _sheets_used else None,
        "warnings":  warnings,
        "ingestion_status": "accepted",
        "mapping_report": {
            "schema_version": _BATCH_SLA_SCHEMA_VERSION,
            "status": "accepted",
            "sheets": _mapping_sheets,
        },
        # Counts per sla_schema classification — lets a caller (or future UI)
        # show "14 explicit, 6 inferred-from-window, 0 no-signal" at a glance
        # instead of re-deriving it from the workflow list.
        "schema_summary": _schema_counts,
    }


def build_workbook_sla_snapshot(parsed: dict) -> dict:
    """Build the SLA Matrix payload directly from a BatchSLA workbook.

    This intentionally does not read Ctrl-M data.  A BatchSLA workbook can
    prove a contract and, when it has a distinct ``Current end time``, can
    also prove a workbook-reported completion.  It cannot prove an execution
    where that field is absent or copied from the contractual deadline, so
    those rows remain explicitly ``NOT_OBSERVED`` rather than being marked OK.
    """
    workflows = parsed.get("workflows") or []
    summary: list[dict] = []
    observed = 0
    counts = {"OK": 0, "LONG_JOB": 0, "AT_RISK": 0, "NO_BUFFER": 0, "BREACH": 0}

    for workflow in workflows:
        # The workbook screen must never present Tier-3/default resolver data
        # as if it came from the uploaded source. Keep such rows visible, but
        # explicitly mark the contract as absent from this workbook.
        contract_conflict = bool(workflow.get("contract_conflict")) or workflow.get("sla_source") == "CONTRACT_CONFLICT"
        declared_in_workbook = workflow.get("sla_source") == "BATCH_SLA_XLSX"
        sla_h = workflow.get("sla_hours") if declared_in_workbook else None
        runtime_h = workflow.get("last_run_hours_xlsx")
        measured = isinstance(runtime_h, Number) and isinstance(sla_h, Number) and float(sla_h) > 0
        if measured:
            observed += 1
            runtime_h = float(runtime_h)
            sla_h = float(sla_h)
            buffer_pct = round((sla_h - runtime_h) / sla_h * 100, 3)
            duration_headroom_mins = round((sla_h - runtime_h) * 60)
            status = workflow.get("compliance") or compliance_label(runtime_h, sla_h)
            if status in counts:
                counts[status] += 1
        else:
            runtime_h = None
            buffer_pct = None
            duration_headroom_mins = None
            if contract_conflict:
                status = "SLA_CONTRACT_CONFLICT"
                reason_code = "CLOCK_DURATION_CONFLICT"
                reason_detail = workflow.get("contract_conflict_detail") or (
                    "Workbook clock-window and declared Duration values conflict; no SLA was selected."
                )
            elif not declared_in_workbook:
                status = "SLA_MISSING"
                reason_code = "SLA_NOT_DECLARED_IN_WORKBOOK"
                reason_detail = "This workbook does not declare an SLA target for this row, so no default ceiling is shown as customer evidence."
            else:
                status = "NOT_OBSERVED"
                reason_code = "COMPLETION_NOT_REPORTED"
                reason_detail = "This workbook supplies the SLA contract but no distinct reported completion time."

        if measured:
            if workflow.get("runtime_source_caveat") == "REPORTED_END_EQUALS_TARGET":
                reason_code = "REPORTED_END_EQUALS_TARGET"
                reason_detail = "Duration is calculated from the supplied Start Time and Current end time. Current end equals Expected End; verify this is an actual completion, not a copied target."
            else:
                reason_code = "WORKBOOK_REPORTED_COMPLETION"
                reason_detail = "Duration is calculated only from this workbook's Start Time and Current end time."

        summary.append({
            "workflow_name": workflow.get("workflow"),
            "workflow_key": workflow.get("workflow"),
            "batch_type": workflow.get("batch_type"),
            "sla_h": sla_h,
            "sla_source": "batch_sla_xlsx_conflict" if contract_conflict else ("batch_sla_xlsx" if declared_in_workbook else "global"),
            "runtime_h": runtime_h,
            "buffer_pct": buffer_pct,
            "duration_headroom_mins": duration_headroom_mins,
            "status": status,
            "workbook_timing_source": workflow.get("workbook_timing_source"),
            "workbook_start_time": workflow.get("workbook_start_time"),
            "workbook_expected_end": workflow.get("workbook_expected_end"),
            "workbook_reported_end": workflow.get("workbook_reported_end"),
            "workbook_clock_window_hours": workflow.get("workbook_clock_window_hours"),
            "workbook_contract_duration_hours": workflow.get("workbook_contract_duration_hours"),
            "runtime_source_caveat": workflow.get("runtime_source_caveat"),
            "contract_conflict": contract_conflict,
            "contract_conflict_detail": workflow.get("contract_conflict_detail"),
            "measurement_reason_code": reason_code,
            "measurement_reason_detail": reason_detail,
        })

    compliance_pct = round(100 * (counts["OK"] + counts["LONG_JOB"] + counts["AT_RISK"] + counts["NO_BUFFER"]) / observed, 1) if observed else None
    declared = [float(w["sla_hours"]) for w in workflows if w.get("sla_source") == "BATCH_SLA_XLSX" and isinstance(w.get("sla_hours"), Number) and w.get("sla_hours") > 0]
    return {
        "workbook_only": True,
        "source": "batch_sla_xlsx",
        "filename": parsed.get("filename"),
        "total_jobs": len(workflows),
        "total_runs": observed,
        "observed_workflow_count": observed,
        "not_observed_workflow_count": len(workflows) - observed,
        "compliance_pct": compliance_pct,
        "window_day_compliance_pct": compliance_pct,
        "ok_runs": counts["OK"],
        "long_job_runs": counts["LONG_JOB"],
        "at_risk_runs": counts["AT_RISK"],
        "no_buffer_runs": counts["NO_BUFFER"],
        "breaching_runs": counts["BREACH"],
        "failed_runs": 0,
        "explicit_sla_matrix": bool(declared),
        "sla_limit_hrs": max(declared) if declared else None,
        "sla_label": "Workbook-declared SLA",
        "workflow_summary": summary,
        "job_summary": [],
        "breaches": [],
        "outliers": [],
        "resource_linked": [],
        "batch_sla_mapping_report": parsed.get("mapping_report") or {},
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

        # Try all sentinels in the list — use first match found.
        # Handles Haleon-style multi-value cells: "JOB_A  JOB_B  JOB_C"
        first_runs = pd.DataFrame()
        for _fj in (row.get("first_jobs_list") or [first_job]):
            _fj_u = str(_fj).strip().upper()
            if not _fj_u:
                continue
            _cand = ctrlm_df[ctrlm_upper.str.contains(_fj_u, na=False, regex=False)]
            if not _cand.empty:
                first_runs = _cand
                break

        last_runs = pd.DataFrame()
        for _lj in (row.get("last_jobs_list") or [last_job]):
            _lj_u = str(_lj).strip().upper()
            if not _lj_u:
                continue
            _cand = ctrlm_df[ctrlm_upper.str.contains(_lj_u, na=False, regex=False)]
            if not _cand.empty:
                last_runs = _cand
                break

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
#  Compile-time fallbacks only — DAILY/WEEKLY/BIWEEKLY/MONTHLY are Settings-
#  overridable and must be read live via _default_sla_for() below, not this
#  dict directly, or a per-engagement override in pe_config silently fails to
#  reach this file (this dict used to be read unconditionally — see
#  _test_algorithm_audit.py issue #1, which caught the same bug in
#  routers/sla_matrix.py; that fix never touched sla_merger.py until now).
GLOBAL_DEFAULTS: dict[str, float] = {
    "DAILY":       6.0,
    "WEEKLY":      8.0,
    "BIWEEKLY":    17.0,
    "MONTHLY":     17.0,
    "QUARTERLY":   12.0,
    "OUTBOUND":    1.0,
    "SEQUENCING":  3.0,   # Sequencing windows are typically shorter than main daily batch
}


def _default_sla_for(batch_type: str) -> float:
    """Tier-3 default SLA hours for a batch type — reads live pe_config for the
    Settings-overridable types, falls back to GLOBAL_DEFAULTS for the rest
    (QUARTERLY/OUTBOUND/SEQUENCING have no pe_config setting) or if the import
    fails."""
    try:
        from services import pe_config as _pc
        _live = {
            "DAILY":    _pc.SLA_DAILY_HRS,
            "WEEKLY":   _pc.SLA_WEEKLY_HRS,
            "BIWEEKLY": _pc.SLA_BIWEEKLY_HRS,
            "MONTHLY":  _pc.SLA_MONTHLY_HRS,
        }
        if batch_type in _live:
            return float(_live[batch_type])
    except Exception:
        pass
    return GLOBAL_DEFAULTS.get(batch_type, 6.0)


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
        "limit_hours": _default_sla_for(batch_type),
        "batch_type":  batch_type,
        "workflow":    None,
        "source":      "GLOBAL_DEFAULT",
        "tier":        3,
    }


# ── Compliance ────────────────────────────────────────────────────────────────

def _format_sla_margin(sla_h: Optional[float], actual_h: Optional[float]) -> Optional[str]:
    """Plain-English margin of runtime vs the SLA window.

    Returns e.g. "2h 0m before deadline", "15m over", "on the edge (0 slack)",
    or None when there is nothing to compare (no runtime).
    Positive margin (sla − actual) = finished early; negative = ran over.
    """
    if actual_h is None or sla_h is None or sla_h <= 0:
        return None
    margin_h = sla_h - actual_h
    total_min = round(abs(margin_h) * 60)
    if total_min < 1:
        return "on the edge (0 slack)"
    hh, mm = divmod(total_min, 60)
    parts = []
    if hh:
        parts.append(f"{hh}h")
    if mm or not hh:
        parts.append(f"{mm}m")
    span = " ".join(parts)
    return f"{span} before deadline" if margin_h > 0 else f"{span} over"


def compliance_label(actual_h: Optional[float], sla_h: Optional[float]) -> str:
    """Classify a workflow's last-known run against its SLA.

    Thresholds read from pe_config (single canonical source).
    Falls back to module-level defaults if pe_config is unavailable.
    Formula: buffer_pct = (sla_h - actual_h) / sla_h * 100
      buffer < 0%            → BREACH   (runtime exceeded the window)
      buffer == 0% (±0.5%)   → NO_BUFFER (runs to the exact edge — zero slack)
      0% < buffer <= AT_RISK → AT_RISK
      AT_RISK < buffer <= LJ → LONG_JOB
      buffer > LJ            → OK
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
    # NO_BUFFER: runtime == window (file stated the same span twice, or the
    # batch is genuinely designed to fill its entire window). Zero contractual
    # slack is a real PE concern, but it is NOT a breach — reserve red BREACH
    # for a genuinely negative buffer (runtime actually longer than the window).
    if abs(buffer_pct) < 0.5:
        return "NO_BUFFER"
    if buffer_pct < 0:
        return "BREACH"
    if buffer_pct <= _at:
        return "AT_RISK"
    if buffer_pct <= _lj:
        return "LONG_JOB"
    return "OK"
