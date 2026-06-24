"""
SLA Intelligence Engine — metadata-driven SLA interpretation.

Auto-detects SLA schema from uploaded customer files, normalizes schedule
and time semantics, chooses the correct calculation model, applies
contingency/buffer rules when present, flags ambiguity when data is
incomplete, and outputs audit-friendly results with source traceability.

SLA Models supported:
  WINDOW    — absolute start/end time cutoff (e.g. 6:00 AM → 12:00 PM)
  DURATION  — expected runtime duration     (e.g. Expected Run time = 2h)
  BUFFERED  — window + contingency margin   (e.g. End + 30m buffer)
  CALENDAR  — schedule-text only, no numeric SLA (e.g. "Runs every Mon-Fri")
  PARTIAL   — some rows have SLA, some don't

Public API:
    ingest_sla_file(raw_bytes, filename)  → SlaIngestResult
    resolve_sla(batch_name, schedule, sla_contracts) → ResolvedSla
    compare_actual(resolved_sla, actual_start, actual_end, actual_runtime_hrs) → SlaVerdict
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pe_dashboard.sla_engine")

# ── Schedule text classification ──────────────────────────────────────────────

_DAILY_PATTERNS = [
    r"(?:^|[^A-Za-z])DAILY(?:$|[^A-Za-z])",       # DAILY, DMD_DAILY, FF_DAILY
    r"(?:^|[^A-Za-z])DLY(?:$|[^A-Za-z])",          # _DLY suffix (common Ctrl-M abbreviation)
    r"\bMON\s*[-–]\s*FRI\b", r"\bMON\s*[-–]\s*THU[R]?\b",
    r"\bMON\s*[-–]\s*SAT\b", r"\bMON\s*[-–]\s*SUN\b",
    r"\bMON(?:DAY)?\s+TO\s+(?:FRI|FRIDAY|SAT|SATURDAY|THU|THURSDAY|THU[R]?S?|SUN|SUNDAY)\b",
    r"\bMONDAY\s+TO\s+(?:FRIDAY|SATURDAY|THURSDAY|SUNDAY)\b",
    r"\bMON\s*,\s*TUE\s*,\s*WED\b", r"\bEVERY\s+(WEEK\s*)?DAY\b",
    r"\bEVERY\s*DAY\b",
    r"\bEVERY\s*D(?:AY)?\b",
    r"\bRUNS\s+EVERY\s*(?:DAY|WEEKDAY|WORKING\s*DAY)\b",
    r"\bRUNS\s+EVERYDAY\b",
    r"\bOVERNIGHT\b", r"\bNIGHTLY\b",
    r"\bMONDAY\s*[-–]\s*SATURDAY\b",
    r"\bCYCLIC\b",
    r"\bD\d*\b",
]
_WEEKLY_PATTERNS = [
    r"(?:^|[^A-Za-z])WEEKLY(?:$|[^A-Za-z])",       # WEEKLY, DMD_BYC_WEEKLY
    r"\bEVERY\s+SUNDAY\b", r"\bEVERY\s+SATURDAY\b",
    r"\bEVERY\s+\w+DAY\b",
    r"\bWEEKEND\b", r"\bW\d*\b",
    # WKLY without a number suffix → standard WEEKLY
    r"\bWKLY(?![0-9])\b",
    # Thursday/Friday/Monday specific weekly runs
    r"\bOUTBOUND[_\-]THUR\b", r"[_\-]THUR[_\-]?\b", r"\bTHURSDAY\b",
    # WKLY[N] — parallel weekly (each N is independent; still classified WEEKLY for ceiling)
    r"\bWKLY[0-9]\b",
]
_MONTHLY_PATTERNS = [
    r"(?:^|[^A-Za-z])MONTHLY(?:$|[^A-Za-z])",
    r"\b1ST\s+WORKING\s*DAY\b", r"\bFIRST\s+WORKING\b",
    r"\bEND\s+OF\s+MONTH\b", r"\bEOM\b", r"\bMONTH\s*[-–]?\s*END\b",
    r"\bM\d+\b", r"\b\d+(?:ST|ND|RD|TH)\s+OF\s+(?:EACH\s+)?MONTH\b",
    r"\b\d+\s+(?:ST|ND|RD|TH)\s+OF\s+(?:EACH\s+)?MONTH\b",
    r"\bEVERY\s+\d+\s*(?:ST|ND|RD|TH)\b",
    r"\bON\s+EVERY\s+\d+\s*(?:ST|ND|RD|TH)\b",
    r"\bLAST\s+\w+\s+OF\s+(EACH\s+)?MONTH\b",
]
_ADHOC_PATTERNS = [
    r"(?:^|[^A-Za-z])AD\s*HOC(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])ADHOC(?:$|[^A-Za-z])",
    r"\bON\s*DEMAND\b",
    r"(?:^|[^A-Za-z])MANUAL(?:$|[^A-Za-z])",
    r"\bAS\s+NEEDED\b", r"\bSPECIAL\b",
]
# Schedules with defined semantics but no standard BY SLA ceiling.
# → compliance_scope=False, ceiling=None, flagged in warnings.
_FORTNIGHTLY_PATTERNS = [
    r"(?:^|[^A-Za-z])FORTNIGHTLY(?:$|[^A-Za-z])", r"\bFORT\s*NIGHT\b",
    r"(?:^|[^A-Za-z])BI\s*[\-–]?\s*WEEKLY(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])BIWEEKLY(?:$|[^A-Za-z])",
    r"\bEVERY\s+(?:TWO|2)\s+WEEKS?\b",
    r"\bEVERY\s+(?:OTHER|ALTERNATE)\s+WEEK\b",
]
_TWICE_DAILY_PATTERNS = [
    r"\bTWICE\s*[\-–]?\s*DAILY\b", r"\bTWICE\s+A\s+DAY\b",
    r"\b2\s*X\s*DAILY\b", r"\bBI\s*[\-–]?\s*DAILY\b",
]
_QUARTERLY_PATTERNS = [
    r"(?:^|[^A-Za-z])QUARTERLY(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])QUATERLY(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])QTR(?:$|[^A-Za-z])",
    r"\bEVERY\s+(?:THREE|3)\s+MONTHS?\b",
    # QTRENDYEAR / QTREND → quarterly trend run
    r"(?:^|[^A-Za-z])QTRENDYEAR(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])QTREND(?:$|[^A-Za-z])",
]

# Cyclic/intraday keywords — should be caught before WEEKLY/DAILY name matching.
# Use [^A-Za-z] boundary style since \b doesn't work after underscore.
_CYCLIC_NAME_PATTERNS = [
    r"(?:^|[^A-Za-z])INTRADAY(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])INTRA(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])POLLING(?:$|[^A-Za-z])",
    r"(?:^|[^A-Za-z])MONITORING(?:$|[^A-Za-z])",
]

_COMPILED_SCHED = {
    # More-specific patterns FIRST — first match wins.
    # TWICE_DAILY before DAILY (contains "DAILY"), FORTNIGHTLY before WEEKLY (contains "WEEKLY")
    "TWICE_DAILY":  [re.compile(p, re.I) for p in _TWICE_DAILY_PATTERNS],
    "FORTNIGHTLY":  [re.compile(p, re.I) for p in _FORTNIGHTLY_PATTERNS],
    "DAILY":        [re.compile(p, re.I) for p in _DAILY_PATTERNS],
    "WEEKLY":       [re.compile(p, re.I) for p in _WEEKLY_PATTERNS],
    "MONTHLY":      [re.compile(p, re.I) for p in _MONTHLY_PATTERNS],
    "ADHOC":        [re.compile(p, re.I) for p in _ADHOC_PATTERNS],
    "QUARTERLY":    [re.compile(p, re.I) for p in _QUARTERLY_PATTERNS],
    "CYCLIC":       [re.compile(p, re.I) for p in _CYCLIC_NAME_PATTERNS],
}

# DOW-specific weekly name patterns (Tier 2 in Stage 3).
# Maps regex → (schedule_type, expected_day)
# SATURDAY is special: DOW flag only, not WEEKLY_SPECIFIC_DAY.
_DOW_WEEKLY_PATTERNS: list = [
    # Ordered most-specific first
    (re.compile(r"[_\-]TUE(?:SDAY)?(?:[_\-]AM|[_\-]PM)?(?:$|[^A-Za-z])", re.I), "WEEKLY_SPECIFIC_DAY", "Tuesday"),
    (re.compile(r"[_\-]WED(?:NESDAY)?(?:$|[^A-Za-z])", re.I),                    "WEEKLY_SPECIFIC_DAY", "Wednesday"),
    (re.compile(r"[_\-]FRI(?:DAY)?(?:$|[^A-Za-z])", re.I),                       "WEEKLY_SPECIFIC_DAY", "Friday"),
    (re.compile(r"[_\-]MON(?:DAY)?(?:$|[^A-Za-z])", re.I),                       "WEEKLY_SPECIFIC_DAY", "Monday"),
    (re.compile(r"[_\-]SUN(?:DAY)?(?:$|[^A-Za-z])", re.I),                       "WEEKLY_SPECIFIC_DAY", "Sunday"),
    (re.compile(r"\bSUNDAY\b", re.I),                                              "WEEKLY_SPECIFIC_DAY", "Sunday"),
    (re.compile(r"\bFRIDAY\b", re.I),                                              "WEEKLY_SPECIFIC_DAY", "Friday"),
    (re.compile(r"\bMONDAY\b", re.I),                                              "WEEKLY_SPECIFIC_DAY", "Monday"),
    (re.compile(r"\bWEDNESDAY\b", re.I),                                           "WEEKLY_SPECIFIC_DAY", "Wednesday"),
    (re.compile(r"\bTUESDAY\b", re.I),                                             "WEEKLY_SPECIFIC_DAY", "Tuesday"),
]
# SAT in name → keep DAILY but flag has_saturday_jobs=True
_SAT_PATTERN = re.compile(r"[_\-]SAT(?:URDAY)?(?:$|[^A-Za-z])|\bSATURDAY\b", re.I)

# DAILY_EXCEPT patterns
_DAILY_EXCEPT_PATTERNS = [
    (re.compile(r"SU[_\-]FR[_\-]ExcptWed|SUN[_\-]FRI[_\-]ExcptWed", re.I),
     "DAILY_EXCEPT", {"skip_days": ["Wednesday"], "note": "Sun-Fri except Wednesday"}),
    (re.compile(r"SU[_\-]FR(?:$|[^A-Za-z])|SUN[_\-]FRI(?:$|[^A-Za-z])", re.I),
     "DAILY_EXCEPT", {"skip_days": [], "note": "Sunday-Friday"}),
    (re.compile(r"ExcptWed|ExcptWednesday|ExceptWed|ExceptWednesday", re.I),
     "DAILY_EXCEPT", {"skip_days": ["Wednesday"], "note": "Daily except Wednesday"}),
]

# BIMONTHLY pattern: BI_MON1ST_SUN, BI_MON2ND_SUN, etc.
_BIMONTHLY_PATTERN = re.compile(r"BI[_\-]MON([12])(?:ST|ND)[_\-]([A-Z]+)", re.I)

# PIPELINE_STAGE: DP01 through DP08 (sequential pipeline phases)
# Boundary-safe: match DP0N even inside longer names like DP05_BATCH
_PIPELINE_STAGE_PATTERN = re.compile(r"(?:^|[^A-Za-z])DP0[1-9](?:$|[^A-Za-z0-9])", re.I)

# Additional weekly patterns with underscore-safe boundaries
_WEEKLY_EXTRA_PATTERNS = [
    # WKLY without trailing digit → standard WEEKLY
    re.compile(r"(?:^|[^A-Za-z0-9])WKLY(?![0-9])", re.I),
    # WKLY[N] (1-9) → PARALLEL_WEEKLY classification (each N is an independent stream)
    # Still uses WEEKLY SLA ceiling — parallel streams share the same window target.
    re.compile(r"(?:^|[^A-Za-z0-9])WKLY[0-9]", re.I),
    re.compile(r"(?:^|[^A-Za-z])THURSDAY(?:$|[^A-Za-z])", re.I),            # THURSDAY
    re.compile(r"(?:^|[^A-Za-z])THUR(?:$|[^A-Za-z])", re.I),                # THUR
    re.compile(r"OUTBOUND[_\-]THUR", re.I),                                   # OUTBOUND_THUR
]

# WKLY[N] parallel weekly — separate compile for metadata enrichment
_PARALLEL_WKLY_PATTERN = re.compile(r"(?:^|[^A-Za-z0-9])WKLY([0-9])", re.I)

# WEEKEND keyword — schedule depends on run data:
#   gap > 14d → MONTHLY_WEEKEND (monthly with weekend timing)
#   cyclic    → CYCLIC
#   default   → WEEKLY_SPECIFIC_DAY (Saturday or Sunday)
_WEEKEND_PATTERN = re.compile(r"(?:^|[^A-Za-z])WEEKEND(?:$|[^A-Za-z])", re.I)

# Additional daily patterns with underscore-safe boundaries
_DAILY_EXTRA_PATTERNS = [
    re.compile(r"(?:^|[^A-Za-z0-9])MON[_\-]FRI(?:$|[^A-Za-z0-9])", re.I),  # MON_FRI
    re.compile(r"OB[_\-]MON[_\-]FRI", re.I),                                  # OB_MON_FRI
    # MIDWEEK / MID_WEEK → Mon/Wed/Thu runs, SLA ceiling 8h (treated as DAILY in name tier,
    # but ceiling overridden to 8h in Stage 6 via MIDWEEK entry in _SLA map)
    re.compile(r"(?:^|[^A-Za-z])MIDWEEK(?:$|[^A-Za-z])|(?:^|[^A-Za-z])MID[_\-]WEEK(?:$|[^A-Za-z])", re.I),
]

# MIDWEEK-specific pattern for returning MIDWEEK type instead of DAILY
_MIDWEEK_PATTERN = re.compile(
    r"(?:^|[^A-Za-z])MIDWEEK(?:$|[^A-Za-z])|(?:^|[^A-Za-z])MID[_\-]WEEK(?:$|[^A-Za-z])", re.I
)


def classify_schedule(text: str) -> str:
    """Classify schedule text → schedule type string.

    Priority order (Stage 3):
      0. Empty/None → UNKNOWN
      1. CYCLIC name patterns (INTRADAY, POLLING, MONITORING)
      2. DAILY_EXCEPT compound patterns (SU-FR, ExcptWed)
      3. BIMONTHLY (BI_MON1ST_SUN, BI_MON2ND_SUN)
      4. PIPELINE_STAGE (DP01-DP08)
      5. DOW-specific patterns (_TUE, _WED, _FRI, _MON, _SUN → WEEKLY_SPECIFIC_DAY)
         NOTE: _SAT/_SATURDAY is NOT returned as WEEKLY — caller must check classify_schedule_meta
      6. Standard keyword patterns (TWICE_DAILY, FORTNIGHTLY, DAILY, WEEKLY, MONTHLY, …)
      7. Compound conflict: name contains both WEEKLY and MONTHLY → data inference decides
         (returns UNKNOWN so classify_schedule_with_data handles it via data signals)

    Returns UNKNOWN only when no pattern matches.
    """
    if not text:
        return "UNKNOWN"
    t = str(text).upper()

    # Stage 3 Tier 1: CYCLIC name markers (fast exit — no SLA analysis)
    for pat in _COMPILED_SCHED.get("CYCLIC", []):
        if pat.search(t):
            return "CYCLIC"

    # Stage 3: DAILY_EXCEPT compound patterns (most specific — check first)
    for pat, stype, _ in _DAILY_EXCEPT_PATTERNS:
        if pat.search(t):
            return stype

    # MIDWEEK / MID_WEEK → return MIDWEEK type (8h SLA ceiling, Mon/Wed/Thu pattern)
    if _MIDWEEK_PATTERN.search(t):
        return "MIDWEEK"

    # MON_FRI / OB_MON_FRI → DAILY_EXCEPT_WEEKEND — check BEFORE DOW patterns
    # so MON_FRI → DAILY_EXCEPT_WEEKEND, not caught as _FRI → WEEKLY_SPECIFIC_DAY
    _mon_fri_pat = re.compile(r"(?:^|[^A-Za-z0-9])MON[_\-]FRI(?:$|[^A-Za-z0-9])|OB[_\-]MON[_\-]FRI", re.I)
    if _mon_fri_pat.search(t):
        return "DAILY_EXCEPT_WEEKEND"

    # WEEKEND keyword — ambiguous without data; return WEEKLY (data inference resolves further)
    if _WEEKEND_PATTERN.search(t):
        return "WEEKLY"   # data inference will distinguish MONTHLY_WEEKEND / CYCLIC / WEEKLY

    # WKLY[N] → PARALLEL_WEEKLY (multiple independent weekly streams)
    if _PARALLEL_WKLY_PATTERN.search(t):
        return "PARALLEL_WEEKLY"

    # Extra daily patterns with underscore-safe boundaries
    for pat in _DAILY_EXTRA_PATTERNS:
        if pat.search(t):
            return "DAILY"

    # Stage 3 Tier 3: BIMONTHLY
    if _BIMONTHLY_PATTERN.search(t):
        return "BIMONTHLY"

    # Stage 3 Tier 5: PIPELINE_STAGE (DP01-DP08)
    if _PIPELINE_STAGE_PATTERN.search(t):
        return "PIPELINE_STAGE"

    # Stage 3 Tier 2: DOW-specific WEEKLY patterns
    for pat, stype, _ in _DOW_WEEKLY_PATTERNS:
        if pat.search(t):
            # SAT/SATURDAY → NOT weekly; fall through to data inference
            if _SAT_PATTERN.search(t) and not pat.search(t.replace("SATURDAY", "").replace("SAT", "")):
                continue
            return stype

    # SEQUENCING: "Daily Sequencing", "PROD_SEQUENCING" — a distinct contractual
    # window, NOT the main daily batch. Check BEFORE _COMPILED_SCHED loop because
    # "Daily Sequencing" contains the word DAILY and would otherwise match the
    # DAILY pattern, collapsing two separate SLA commitments into one ceiling.
    if re.search(r"SEQUENC", t):
        return "SEQUENCING"

    # Stage 3 Tier 4: Standard keyword patterns
    # Compound WEEKLY+MONTHLY in same name → defer to data inference (Addition 3)
    _has_weekly  = bool(re.search(r"(?:^|[^A-Za-z])WEEKLY(?:$|[^A-Za-z])", t))
    _has_monthly = bool(re.search(r"(?:^|[^A-Za-z])MONTHLY(?:$|[^A-Za-z])", t))
    if _has_weekly and _has_monthly:
        return "UNKNOWN"   # classify_schedule_with_data resolves via Signal 1 gap analysis

    for sched_type, patterns in _COMPILED_SCHED.items():
        if sched_type == "CYCLIC":   # already handled above
            continue
        for pat in patterns:
            if pat.search(text):
                return sched_type

    # Extra weekly patterns (underscore-boundary-safe — WKLY without number, THUR, OUTBOUND_THUR)
    for pat in _WEEKLY_EXTRA_PATTERNS:
        if pat.search(t):
            return "WEEKLY"

    # ── Generic structural keywords (no customer/product strings) ─────────
    # Checked LAST so day-of-week and weekly patterns above win first
    # (e.g. OUTBOUND_THUR → WEEKLY, not OUTBOUND).
    #
    # Calendar-based: 4-4-5 retail calendar codes (444 / 445 / CALENDAR_44)
    if re.search(r"(?:^|[^0-9])44[45](?:$|[^0-9])|CALENDAR[_\-]?44", t):
        return "CALENDAR_BASED"
    # Morning report / AM runs → daily cadence
    if re.search(r"(?:^|[^A-Za-z])MRNG(?:$|[^A-Za-z])|MORNING[_\-]?RPT|AM[_\-]RPT|(?:^|[^A-Za-z])MORN(?:$|[^A-Za-z])", t):
        return "DAILY"
    # Outbound / EDI file delivery — cyclic polling, not a batch window.
    # Mark OUTBOUND so it is excluded from compliance; detect_cyclic_subs()
    # still confirms genuine high-frequency cyclic behaviour via its guards.
    if re.search(r"(?:^|[^A-Za-z])(?:OB|OUTBOUND|EDI)[_\-]", t):
        return "OUTBOUND"

    return "UNKNOWN"


# ── Data-driven schedule inference ───────────────────────────────────────────


def classify_schedule_meta(text: str) -> dict:
    """Extended schedule classification returning metadata beyond a plain type string.

    Returns a dict with:
        schedule_type      : str  — same as classify_schedule(text)
        expected_day       : str | None  — DOW for WEEKLY_SPECIFIC_DAY
        has_saturday_jobs  : bool — True when name contains SAT/SATURDAY
                                    (daily batch with Saturday-specific jobs,
                                     NOT a Saturday-only weekly schedule)
        bimonthly_occurrence: int | None — 1 or 2 for BI_MON patterns
        bimonthly_day      : str | None  — weekday name for BI_MON patterns
        pipeline_stage_num : int | None  — DP01..DP08 stage number
        daily_except_meta  : dict | None — {skip_days, note} for DAILY_EXCEPT
    """
    result: dict = {
        "schedule_type": "UNKNOWN",
        "expected_day": None,
        "has_saturday_jobs": False,
        "bimonthly_occurrence": None,
        "bimonthly_day": None,
        "pipeline_stage_num": None,
        "daily_except_meta": None,
    }
    if not text:
        return result

    t = str(text)

    # SAT flag — check early and separately
    if _SAT_PATTERN.search(t):
        result["has_saturday_jobs"] = True

    stype = classify_schedule(t)
    result["schedule_type"] = stype

    # Enrich DOW information for WEEKLY_SPECIFIC_DAY
    if stype == "WEEKLY_SPECIFIC_DAY":
        for pat, _, day in _DOW_WEEKLY_PATTERNS:
            if pat.search(t):
                result["expected_day"] = day
                break

    # Enrich PARALLEL_WEEKLY with stream number
    elif stype == "PARALLEL_WEEKLY":
        m_pw = _PARALLEL_WKLY_PATTERN.search(t)
        if m_pw:
            result["parallel_stream_num"] = int(m_pw.group(1))

    # Enrich BIMONTHLY
    elif stype == "BIMONTHLY":
        m = _BIMONTHLY_PATTERN.search(t)
        if m:
            result["bimonthly_occurrence"] = int(m.group(1))
            result["bimonthly_day"] = m.group(2).capitalize()

    # Enrich PIPELINE_STAGE
    elif stype == "PIPELINE_STAGE":
        m = _PIPELINE_STAGE_PATTERN.search(t)
        if m:
            num_str = re.sub(r"[^0-9]", "", m.group(0))
            result["pipeline_stage_num"] = int(num_str) if num_str else None

    # Enrich DAILY_EXCEPT
    elif stype == "DAILY_EXCEPT":
        for pat, _, meta in _DAILY_EXCEPT_PATTERNS:
            if pat.search(t):
                result["daily_except_meta"] = meta
                break

    return result


import statistics as _statistics  # stdlib — always available
from collections import Counter as _Counter
from dataclasses import dataclass as _infer_dc, field as _infer_field
from typing import List as _IList, Optional as _IOpt


@_infer_dc
class ScheduleInference:
    """Result of _infer_schedule_from_run_pattern() / classify_schedule_with_data()."""
    schedule_type:        str           # DAILY/WEEKLY/MONTHLY/FORTNIGHTLY/UNKNOWN/…
    ctrl_m_sla_hrs:       _IOpt[float]  # ceiling hrs; None = no standard SLA applies
    in_compliance_scope:  bool
    inference_source:     str           # NAME_KEYWORD/DATA_INFERRED/DEFAULT
    inference_confidence: str           # HIGH/MEDIUM/LOW/NONE
    dominant_day:         _IOpt[str]  = None  # e.g. 'Sunday' for WEEKLY
    dom_value:            _IOpt[int]  = None  # day-of-month for DATE_SPECIFIC_MONTHLY
    signals_used:         _IList[str] = _infer_field(default_factory=list)
    warning:              _IOpt[str]  = None


def _infer_schedule_from_run_pattern(
    run_dates: list,
    sub_app_name: str = "",
    sparse_export: bool = False,
) -> ScheduleInference:
    """Infer schedule type from actual run date patterns using 4 independent signals.

    Level-3 data-driven fallback called when name-based classification returns
    UNKNOWN.  Four signals vote and the weighted majority wins.

    Parameters
    ----------
    run_dates    : list of datetime.date (unique run dates for this sub_app)
    sub_app_name : original sub-application name (for warning messages only)
    sparse_export: True when the parent CSV is a SPARSE_MONTHLY export —
                   disables Signals 1 & 4 which are meaningless on monthly snapshots

    Returns
    -------
    ScheduleInference — never raises
    """
    from datetime import date as _date_t

    # ── helpers ───────────────────────────────────────────────────────────
    try:
        from services import pe_config as _pc
        _DAILY_H  = getattr(_pc, "SLA_DAILY_HRS",  6.0)
        _WEEKLY_H = getattr(_pc, "SLA_WEEKLY_HRS", 8.0)
    except Exception:
        _DAILY_H, _WEEKLY_H = 6.0, 8.0

    _SLA_MAP = {
        "DAILY": _DAILY_H, "TWICE_DAILY": _DAILY_H, "WEEKLY": _WEEKLY_H,
    }
    _SCOPE_TYPES = {"DAILY", "TWICE_DAILY", "WEEKLY"}

    def _make(stype, source, conf, dominant_day=None, dom_value=None,
              signals=None, warning=None):
        return ScheduleInference(
            schedule_type=stype,
            ctrl_m_sla_hrs=_SLA_MAP.get(stype),
            in_compliance_scope=stype in _SCOPE_TYPES,
            inference_source=source,
            inference_confidence=conf,
            dominant_day=dominant_day,
            dom_value=dom_value,
            signals_used=signals or [],
            warning=warning,
        )

    def _unknown(reason, signals=None):
        return _make(
            "UNKNOWN", "DEFAULT", "NONE", signals=signals,
            warning=(
                f"Cannot determine schedule type for '{sub_app_name}' "
                f"({reason}) — excluded from compliance. "
                "Upload BatchSLA XLSX to set contracted schedule type."
            ),
        )

    # ── Normalise input ───────────────────────────────────────────────────
    try:
        if not run_dates:
            return _unknown("INSUFFICIENT_DATA")

        dates = sorted({
            d if isinstance(d, _date_t) else _date_t.fromisoformat(str(d)[:10])
            for d in run_dates if d is not None
        })
        n = len(dates)
        if n < 2:
            return _unknown("INSUFFICIENT_DATA")

        date_range_days = max((dates[-1] - dates[0]).days, 1)

    except Exception as exc:
        return _unknown(f"parse_error:{exc}")

    # ── Special Case 3 — dense short range → unambiguous DAILY ───────────
    if n >= 20 and date_range_days <= 35:
        return _make("DAILY", "DATA_INFERRED", "HIGH",
                     signals=["DENSE_SHORT_RANGE"])

    # ── Signal weights and confidence multipliers ─────────────────────────
    _SIG_W = {"Signal1_Gap": 2, "Signal2_DOW": 3, "Signal3_DOM": 3, "Signal4_Density": 1}
    _CONF_MUL = {"high": 1.0, "medium": 0.6, "low": 0.3, None: 0.0}

    votes: list  = []   # (sched_type, signal_name, confidence)
    signals_fired: list = []
    dominant_day_out = None
    dom_value_out    = None

    # ── SIGNAL 1 — Gap Distribution Analysis ─────────────────────────────
    if not sparse_export and n >= 2:
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, n)]
        if gaps:
            median_gap = _statistics.median(gaps)
            mean_gap   = _statistics.mean(gaps)
            std_gap    = _statistics.stdev(gaps) if len(gaps) >= 2 else 0.0
            gap_cv     = (std_gap / mean_gap) if mean_gap > 0 else 0.0

            s1_vote = s1_conf = None
            if median_gap <= 1.5:
                s1_vote, s1_conf = "DAILY",       "high"
            elif 1.5 < median_gap <= 2.5:
                s1_vote, s1_conf = "DAILY",       "medium"
            elif 5.5 <= median_gap <= 7.5:
                s1_vote, s1_conf = "WEEKLY",      "high"
            elif 13 <= median_gap <= 15:
                s1_vote, s1_conf = "FORTNIGHTLY", "medium"
            elif 25 <= median_gap <= 35:
                s1_vote, s1_conf = "MONTHLY",     "medium"
            elif median_gap > 60:
                s1_vote, s1_conf = "QUARTERLY",   "low"

            # High CV → irregular gaps → reduce confidence one level
            if s1_vote and gap_cv > 1.5:
                _levels = [None, "low", "medium", "high"]
                idx = _levels.index(s1_conf) - 1
                s1_conf = _levels[max(0, idx)]

            if s1_vote and s1_conf:
                votes.append((s1_vote, "Signal1_Gap", s1_conf))
                signals_fired.append(
                    f"gap(median={median_gap:.1f}d,cv={gap_cv:.2f}→{s1_vote}/{s1_conf})"
                )

    # ── SIGNAL 3 — Day-of-Month Pattern ──────────────────────────────────
    # Computed BEFORE Signal 2 so we can suppress DOW when DOM is high-confidence monthly.
    _s3_is_monthly_high = False   # flag used by Signal 2 suppression below
    s3_vote = s3_conf = None
    if n >= 2:
        dom_vals    = [d.day for d in dates]
        dom_counter = _Counter(dom_vals)
        dom_unique  = set(dom_vals)

        if len(dom_unique) == 1:
            s3_vote, s3_conf = "DATE_SPECIFIC_MONTHLY", "high"
            dom_value_out = dom_vals[0]
            _s3_is_monthly_high = True
        elif len(dom_unique) == 2 and n >= 4:
            s3_vote, s3_conf = "BIMONTHLY_SPECIFIC", "high"
            _s3_is_monthly_high = True
        elif max(dom_vals) <= 5 and n >= 6:     # ≥6 to avoid quarterly-on-early-day false positive
            s3_vote, s3_conf = "MONTHLY", "medium"
        elif min(dom_vals) >= 28 and n >= 6:    # same guard for end-of-month
            s3_vote, s3_conf = "MONTHLY", "medium"

        if s3_vote and s3_conf:
            votes.append((s3_vote, "Signal3_DOM", s3_conf))
            signals_fired.append(
                f"dom(unique={sorted(dom_unique)[:5]}→{s3_vote}/{s3_conf})"
            )

    # ── SIGNAL 2 — Day-of-Week Concentration ─────────────────────────────
    # Suppressed when:
    #   • Signal 3 fired HIGH for a monthly type  (DOW spread is a monthly artifact)
    #   • Signal 1 voted FORTNIGHTLY              (every-14-day run always same weekday)
    _DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
                  4: "Friday",  5: "Saturday", 6: "Sunday"}
    _s1_is_fortnightly = any(v[0] == "FORTNIGHTLY" for v in votes)

    if n >= 2 and not _s3_is_monthly_high and not _s1_is_fortnightly:
        dow_counts   = _Counter(d.weekday() for d in dates)
        n_unique_dows = len(dow_counts)
        dominant_dow  = dow_counts.most_common(1)[0][0]
        dominant_pct  = dow_counts[dominant_dow] / n

        s2_vote = s2_conf = None
        if n_unique_dows == 1:
            s2_vote, s2_conf = "WEEKLY", ("high" if n >= 3 else "low")
            dominant_day_out = _DOW_NAMES.get(dominant_dow)
        elif n_unique_dows <= 2 and dominant_pct >= 0.70 and n >= 4:
            s2_vote, s2_conf = "WEEKLY", "medium"
            dominant_day_out = _DOW_NAMES.get(dominant_dow)
        elif n_unique_dows >= 5 and n >= 7:
            # Extra guard: DOW spread only supports DAILY when gap analysis agrees
            # (not when median gap is > 7 days — that's just a monthly or sparse pattern)
            _median_gap_approx = date_range_days / max(n - 1, 1)
            if _median_gap_approx <= 3.0:
                s2_vote, s2_conf = "DAILY", "high"
            # else: abstain — spread across 5+ days but not dense enough for DAILY
        # 3-4 unique DOWs with no concentration → abstain

        if s2_vote and s2_conf:
            votes.append((s2_vote, "Signal2_DOW", s2_conf))
            signals_fired.append(
                f"dow(n_dows={n_unique_dows},"
                f"dom={_DOW_NAMES.get(dominant_dow)}/{dominant_pct:.0%}→{s2_vote}/{s2_conf})"
            )
    elif _s3_is_monthly_high:
        signals_fired.append("dow(suppressed:dom_monthly_high)")
    elif _s1_is_fortnightly:
        signals_fired.append("dow(suppressed:fortnightly_gap)")

    # ── SIGNAL 4 — Run Density vs Date Range ─────────────────────────────
    if not sparse_export:
        den_daily   = n / date_range_days
        den_weekly  = n / (date_range_days / 7.0)
        den_monthly = n / (date_range_days / 30.0)

        s4_vote = s4_conf = None
        if 0.60 <= den_daily <= 1.10:
            s4_vote, s4_conf = "DAILY",  "high"
        elif 0.40 <= den_daily < 0.60:
            s4_vote, s4_conf = "DAILY",  "medium"
        elif 0.65 <= den_weekly <= 1.35:
            s4_vote, s4_conf = "WEEKLY", "medium"
        elif 0.65 <= den_monthly <= 1.35:
            s4_vote, s4_conf = "MONTHLY", "medium"

        if s4_vote and s4_conf:
            votes.append((s4_vote, "Signal4_Density", s4_conf))
            signals_fired.append(
                f"density(d={den_daily:.2f},w={den_weekly:.2f},m={den_monthly:.2f}"
                f"→{s4_vote}/{s4_conf})"
            )

    # ── Vote aggregation — weighted majority ──────────────────────────────
    if not votes:
        return _unknown(
            "SPARSE_EXPORT_AMBIGUOUS" if sparse_export else "NO_SIGNAL",
            signals_fired,
        )

    score: dict = {}
    total_weight = 0.0
    for (stype, sig_name, conf) in votes:
        w   = _SIG_W.get(sig_name, 1)
        mul = _CONF_MUL.get(conf, 0.0)
        score[stype] = score.get(stype, 0.0) + w * mul
        total_weight += w * mul

    winner   = max(score, key=lambda k: score[k])
    conf_pct = (score[winner] / total_weight * 100) if total_weight > 0 else 0.0

    if conf_pct >= 70:
        return _make(winner, "DATA_INFERRED", "HIGH",
                     dominant_day=dominant_day_out, dom_value=dom_value_out,
                     signals=signals_fired)
    elif conf_pct >= 45:
        return _make(
            winner, "DATA_INFERRED", "MEDIUM",
            dominant_day=dominant_day_out, dom_value=dom_value_out,
            signals=signals_fired,
            warning=(
                f"Schedule inferred as {winner} with medium confidence for "
                f"'{sub_app_name}' (score {conf_pct:.0f}%) — verify with customer. "
                f"Signals: {'; '.join(signals_fired)}"
            ),
        )
    else:
        return _unknown(f"LOW_CONFIDENCE({conf_pct:.0f}%)", signals_fired)


def classify_schedule_with_data(
    text: str,
    run_dates: _IOpt[list] = None,
    sub_app_name: str = "",
    sparse_export: bool = False,
) -> ScheduleInference:
    """Full 5-level schedule classification pipeline.

    Level 1 — XLSX override    (caller must apply; not handled here)
    Level 2 — Name keyword     (classify_schedule regex patterns)
    Level 2b— Signal 5 conflict check: name vs data conflict resolution
    Level 3 — Data inference   (_infer_schedule_from_run_pattern)
    Level 4 — Safe default     UNKNOWN → 6h + UNVERIFIED flag (Stage 6)

    Signal 5 conflict rules (Stage 4):
      - name=WEEKLY but data=DAILY (HIGH) → data wins + NAMED_WEEKLY_RUNS_DAILY warning
      - name=DAILY but data=WEEKLY (HIGH) → data wins + CONFLICT_DAILY_NAMED_WEEKLY warning
      - CYCLIC always wins unconditionally (Stage 2 handles this in detect_cyclic_subs)
      - All other conflicts: name wins, data adds warning

    Stage 6 SLA ceiling mapping:
      DAILY / DAILY_EXCEPT / TWICE_DAILY → 6h
      WEEKLY / WEEKLY_SPECIFIC_DAY / FORTNIGHTLY / PARALLEL_WORKFLOW → 8h
      MONTHLY / BIMONTHLY / DATE_SPECIFIC_MONTHLY / PIPELINE_STAGE / ADHOC → None
      UNKNOWN → 6h + UNVERIFIED flag (in_compliance_scope=True with warning)

    Always returns ScheduleInference; never raises.
    """
    try:
        from services import pe_config as _pc
        _DAILY_H  = getattr(_pc, "SLA_DAILY_HRS",  6.0)
        _WEEKLY_H = getattr(_pc, "SLA_WEEKLY_HRS", 8.0)
    except Exception:
        _DAILY_H, _WEEKLY_H = 6.0, 8.0

    # Stage 6 — complete SLA ceiling map
    _SLA: dict = {
        "DAILY":                _DAILY_H,
        "TWICE_DAILY":          _DAILY_H,
        "DAILY_EXCEPT":         _DAILY_H,
        "DAILY_EXCEPT_WEEKEND": _DAILY_H,   # MON_FRI / OB_MON_FRI
        "MIDWEEK":              _WEEKLY_H,  # Mon/Wed/Thu pattern — 8h ceiling (not 6h)
        "WEEKLY":               _WEEKLY_H,
        "WEEKLY_SPECIFIC_DAY":  _WEEKLY_H,
        "FORTNIGHTLY":          _WEEKLY_H,  # Stage 6: FORTNIGHTLY → 8h (not None)
        "BIWEEKLY":             _WEEKLY_H,
        "PARALLEL_WORKFLOW":    _WEEKLY_H,
        "PARALLEL_WEEKLY":      _WEEKLY_H,  # WKLY[N] streams — each gets 8h ceiling
    }
    # Types excluded from compliance (ceiling = None)
    _NO_SLA_TYPES = {
        "MONTHLY", "BIMONTHLY", "DATE_SPECIFIC_MONTHLY", "QUARTERLY",
        "PIPELINE_STAGE", "ADHOC", "CYCLIC", "CYCLIC_INTERVAL",
        "CALENDAR_BASED", "OUTBOUND", "UNKNOWN",
    }
    _SCOPE: set = set(_SLA.keys())

    def _make_name(name_type: str, meta: _IOpt[dict] = None) -> ScheduleInference:
        dom_v = None
        exp_day = None
        if meta:
            exp_day = meta.get("expected_day")
        return ScheduleInference(
            schedule_type=name_type,
            ctrl_m_sla_hrs=_SLA.get(name_type),
            in_compliance_scope=name_type in _SCOPE,
            inference_source="NAME_KEYWORD",
            inference_confidence="HIGH",
            dominant_day=exp_day,
        )

    # Level 2 — name keyword
    meta = classify_schedule_meta(text or "")
    name_type = meta["schedule_type"]

    # Addition 3 — Compound WEEKLY+MONTHLY name resolution via data gap analysis.
    # classify_schedule() returns UNKNOWN when name contains both WEEKLY and MONTHLY.
    # Here we use Signal 1 (median gap) to pick the correct type.
    # WEEKLY is the safe default when data is ambiguous (lower SLA ceiling).
    _t_upper = (text or "").upper()
    _both_wm = (
        bool(re.search(r"(?:^|[^A-Za-z])WEEKLY(?:$|[^A-Za-z])", _t_upper)) and
        bool(re.search(r"(?:^|[^A-Za-z])MONTHLY(?:$|[^A-Za-z])", _t_upper))
    )
    if name_type == "UNKNOWN" and _both_wm and run_dates and len(run_dates) >= 4:
        try:
            import statistics as _stats
            from datetime import date as _dt
            _dates = sorted({
                d if hasattr(d, "day") else _dt.fromisoformat(str(d)[:10])
                for d in run_dates if d is not None
            })
            _gaps = [(_dates[i] - _dates[i - 1]).days for i in range(1, len(_dates))]
            if _gaps:
                _median_gap = _stats.median(_gaps)
                if _median_gap <= 10:
                    name_type = "WEEKLY"   # data shows weekly cadence
                elif _median_gap >= 20:
                    name_type = "MONTHLY"  # data shows monthly cadence
                else:
                    name_type = "WEEKLY"   # ambiguous → safer WEEKLY default (lower SLA)
            else:
                name_type = "WEEKLY"
        except Exception:
            name_type = "WEEKLY"   # fallback: safe default

    # Addition 2 — WEEKEND keyword: resolve to specific type using data
    # classify_schedule() returns WEEKLY for WEEKEND; refine here if data available
    if name_type == "WEEKLY" and _WEEKEND_PATTERN.search(text or "") and run_dates and len(run_dates) >= 4:
        try:
            import statistics as _stats_w
            from datetime import date as _dtw
            _wdates = sorted({
                d if hasattr(d, "day") else _dtw.fromisoformat(str(d)[:10])
                for d in run_dates if d is not None
            })
            _wgaps = [(_wdates[i] - _wdates[i - 1]).days for i in range(1, len(_wdates))]
            if _wgaps:
                _wmed = _stats_w.median(_wgaps)
                if _wmed >= 20:
                    name_type = "MONTHLY"   # WEEKEND but monthly cadence
                # else: stays WEEKLY (weekend-of-week pattern)
        except Exception:
            pass  # keep WEEKLY

    # Fast exit for types that don't need conflict checking
    _CONFLICT_CHECK_TYPES = {"WEEKLY", "DAILY"}
    if name_type != "UNKNOWN" and name_type not in _CONFLICT_CHECK_TYPES:
        return _make_name(name_type, meta)

    # Level 2b — Signal 5 conflict check (only for WEEKLY and DAILY name results)
    if name_type in _CONFLICT_CHECK_TYPES and run_dates is not None and len(run_dates) >= 6:
        inferred = _infer_schedule_from_run_pattern(
            run_dates,
            sub_app_name=sub_app_name or text,
            sparse_export=sparse_export,
        )
        data_type = inferred.schedule_type
        data_conf = inferred.inference_confidence

        if data_conf == "HIGH" and data_type != name_type:
            if name_type == "WEEKLY" and data_type == "DAILY":
                return ScheduleInference(
                    schedule_type="DAILY",
                    ctrl_m_sla_hrs=_DAILY_H,
                    in_compliance_scope=True,
                    inference_source="DATA_OVERRIDE",
                    inference_confidence="HIGH",
                    signals_used=inferred.signals_used,
                    warning=(
                        f"Sub_app '{sub_app_name or text}' named WEEKLY but run data shows "
                        f"daily pattern — using DAILY ({_DAILY_H}h ceiling). "
                        "Verify schedule with customer."
                    ),
                )
            elif name_type == "DAILY" and data_type == "WEEKLY":
                return ScheduleInference(
                    schedule_type="WEEKLY",
                    ctrl_m_sla_hrs=_WEEKLY_H,
                    in_compliance_scope=True,
                    inference_source="DATA_OVERRIDE",
                    inference_confidence="HIGH",
                    signals_used=inferred.signals_used,
                    warning=(
                        f"Sub_app '{sub_app_name or text}' named DAILY but run data shows "
                        f"weekly pattern — using WEEKLY ({_WEEKLY_H}h ceiling)."
                    ),
                )

    # name resolved with no conflict (or UNKNOWN, needs data inference)
    if name_type != "UNKNOWN":
        return _make_name(name_type, meta)

    # Level 3 — data inference
    if run_dates is not None and len(run_dates) >= 2:
        inferred = _infer_schedule_from_run_pattern(
            run_dates,
            sub_app_name=sub_app_name or text,
            sparse_export=sparse_export,
        )
        if inferred.schedule_type != "UNKNOWN" or inferred.warning:
            return inferred

    # Level 4 — UNKNOWN + 6h UNVERIFIED default (Stage 6)
    return ScheduleInference(
        schedule_type="UNKNOWN",
        ctrl_m_sla_hrs=_DAILY_H,   # Stage 6: UNKNOWN → 6h with UNVERIFIED flag
        in_compliance_scope=True,
        inference_source="DEFAULT",
        inference_confidence="NONE",
        warning=(
            f"Schedule not detected for '{sub_app_name or text}' — "
            f"using {_DAILY_H}h default ceiling (UNVERIFIED). "
            "Upload BatchSLA XLSX to set contracted SLA."
        ),
    )


# ── Column name normalization ─────────────────────────────────────────────────

# Maps messy customer column names → canonical field names
_COLUMN_MAP: Dict[str, str] = {}
_COL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # ── Batch / Job name ─────────────────────────────────────────────────────
    (re.compile(r"batch[\s_-]?name|batch[\s_-]?type|window[\s_-]?name|module", re.I), "batch_name"),
    (re.compile(r"^batch$|^job$|^task$|^process$|^workflow$|^stream$", re.I), "batch_name"),
    (re.compile(r"job[\s_-]?type|job[\s_-]?name|task[\s_-]?name|process[\s_-]?name", re.I), "batch_name"),
    # When a customer's SLA XLSX uses "Sub Application" / "Sub_Application" as the
    # workflow key column, it must map to batch_name (the SLA contract identifier).
    (re.compile(r"sub[\s_-]?application|sub[\s_-]?app(?:lication)?$", re.I), "batch_name"),
    # ── Schedule / frequency ─────────────────────────────────────────────────
    (re.compile(r"schedule[\s_-]?(type|name)?$", re.I), "schedule_text"),
    (re.compile(r"^day$|^days$|^frequency$|^run[\s_-]?days?$", re.I), "schedule_text"),
    (re.compile(r"cadence|run[\s_-]?type|run[\s_-]?period|^type$|^period$", re.I), "schedule_text"),
    # ── First / Last job ─────────────────────────────────────────────────────
    (re.compile(r"first[\s_-]?job[\s_-]?name?|first[\s_-]?jobname?", re.I), "first_job"),
    (re.compile(r"last[\s_-]?job[\s_-]?name?|last[\s_-]?jobname?", re.I), "last_job"),
    # ── Hard SLA deadline (most-specific first) ───────────────────────────────
    (re.compile(r"sla[\s_-]?cutoff|cutoff|sla[\s_-]?deadline", re.I), "sla_end_time"),
    (re.compile(r"expected[\s_-]?end[\s_-]?time[\s_-]?/?sla|expected[\s_-]?end[\s_-]?time"
                r"|expected[\s_-]?end|expected[\s_-]?finish|deadline|close"
                r"|window[\s_-]?end|sla[\s_-]?end|sla[\s_-]?time$|^sla$"
                r"|due[\s_-]?by|due[\s_-]?time|due[\s_-]?date|^due$"
                r"|complete[\s_-]?by|must[\s_-]?complete[\s_-]?by"
                r"|^end$|^finish$|^closes?$"
                r"|completion[\s_-]?time|end[\s_-]?by|finish[\s_-]?by|close[\s_-]?time"
                r"|must[\s_-]?end[\s_-]?by", re.I), "sla_end_time"),
    # ── Target / expected completion (when separate from hard deadline) ───────
    (re.compile(r"expected[\s_-]?completion|target[\s_-]?completion"
                r"|planned[\s_-]?end|planned[\s_-]?completion"
                r"|target[\s_-]?end[\s_-]?time|target[\s_-]?end"
                r"|expected[\s_-]?complete|target[\s_-]?time", re.I), "expected_completion_time"),
    # ── Start time ───────────────────────────────────────────────────────────
    (re.compile(r"start[\s_-]?time|window[\s_-]?start|begin[\s_-]?time"
                r"|kick[\s_-]?off|launch|^start$|^begins?$|^open$"
                r"|start[\s_-]?date|from[\s_-]?time|window[\s_-]?from", re.I), "start_time"),
    # ── Actual / observed end ────────────────────────────────────────────────
    (re.compile(r"current[\s_-]?end[\s_-]?time|actual[\s_-]?end", re.I), "actual_end_time"),
    # ── Expected / allowed duration ──────────────────────────────────────────
    (re.compile(r"expected[\s_-]?run[\s_-]?time|expected[\s_-]?duration"
                r"|sla[\s_-]?duration|sla[\s_-]?runtime|sla[\s_-]?window"
                r"|max[\s_-]?time|max[\s_-]?duration"
                r"|allowed[\s_-]?time|allowed[\s_-]?duration"
                r"|run[\s_-]?window|time[\s_-]?window|window[\s_-]?hours?"
                r"|batch[\s_-]?duration|run[\s_-]?duration|^duration$", re.I), "sla_duration"),
    (re.compile(r"current[\s_-]?run[\s_-]?time|actual[\s_-]?run[\s_-]?time|actual[\s_-]?duration", re.I), "actual_duration"),
    # ── Buffer / contingency ─────────────────────────────────────────────────
    (re.compile(r"buffer|contingency|margin|grace", re.I), "buffer"),
    # ── Timezone ─────────────────────────────────────────────────────────────
    (re.compile(r"time[\s_-]?zone|tz", re.I), "timezone"),
    # ── Comments / notes ─────────────────────────────────────────────────────
    (re.compile(r"comment|note|remark|interpretation|agreement", re.I), "comments"),
    # ── Status ───────────────────────────────────────────────────────────────
    (re.compile(r"pass[\s_-]?fail|sla[\s_-]?status|status|result|outcome", re.I), "status"),
]

# ── Known canonical field names produced by _COL_PATTERNS ────────────────────
_KNOWN_CANONICAL: frozenset = frozenset({
    "batch_name", "schedule_text", "first_job", "last_job",
    "sla_end_time", "expected_completion_time", "start_time",
    "actual_end_time", "sla_duration", "actual_duration",
    "buffer", "timezone", "comments", "status",
})


def _normalize_column(raw_col: str) -> str:
    """Map a raw column name to canonical field name.

    Pre-processes: strips parenthetical suffixes like '(2019)', '(PHT)', '(hrs)'
    before pattern matching, so 'Expected End Time/SLA (2019)' → 'sla_end_time'.
    """
    clean = str(raw_col).strip()
    # Strip parenthetical suffixes: " (2019)", " (PHT)", " (hrs)", etc.
    clean = re.sub(r'\s*\([^)]*\)\s*$', '', clean).strip()
    for pat, canonical in _COL_PATTERNS:
        if pat.search(clean):
            return canonical
    return clean.lower().replace(" ", "_").replace("-", "_").replace("/", "_")


# ── Value-based column inference (fallback when header doesn't match) ─────────

_SCHEDULE_VALUE_WORDS = frozenset({
    "DAILY", "WEEKLY", "MONTHLY", "ADHOC", "AD HOC", "NIGHTLY",
    "OVERNIGHT", "WEEKEND", "WEEKDAY", "EVERYDAY", "BI-WEEKLY",
    "MON-FRI", "MONDAY TO FRIDAY", "EVERY WEEK", "EVERY DAY",
})


def _extract_tz_from_value(raw: str) -> str:
    """Extract timezone abbreviation from a time string like '7:00 AM IST'.
    Returns the timezone abbreviation (e.g. 'IST') or empty string.
    """
    m = _TZ_SUFFIX.search(str(raw).strip())
    return m.group(0).strip().upper() if m else ""


def _infer_unmapped_columns(
    df: "pd.DataFrame",
    existing_canonical: "set[str]",
) -> Dict[str, str]:
    """Value-based fallback: scan cell values of unmapped columns to infer
    their canonical field.

    Handles customers who name their columns anything — 'Time', 'Hours',
    'Window', 'Freq', 'Due', etc.  Pattern-based matching covers ~95% of
    cases; this function catches the remaining ~5%.

    Returns {raw_col_name → canonical_field_name}.
    """
    import pandas as pd

    extra: Dict[str, str] = {}
    time_candidates: list = []   # columns whose values look like clock times

    for col in df.columns:
        if col in existing_canonical:
            continue  # already mapped by header
        series = df[col].dropna()
        if len(series) < 2:
            continue
        sample = series.head(20).astype(str).tolist()
        n = len(sample)

        # ── Test 1: values look like clock times? ─────────────────────────
        time_hits = sum(1 for v in sample if _parse_time(v) is not None)
        if time_hits / n > 0.5:
            time_candidates.append(col)
            continue

        # ── Test 2: values look like schedule/frequency words? ────────────
        sched_hits = sum(
            1 for v in sample
            if v.upper().strip() in _SCHEDULE_VALUE_WORDS
            or any(w in v.upper() for w in ("DAILY", "WEEKLY", "MONTHLY"))
        )
        if sched_hits / n > 0.5 and "schedule_text" not in existing_canonical:
            extra[col] = "schedule_text"
            existing_canonical.add("schedule_text")
            continue

        # ── Test 3: values look like durations? ───────────────────────────
        # Require EXPLICIT format (HH:MM, "Xh Ym", "X min") — exclude plain
        # integers to avoid falsely mapping Ctrl-M's Run_Sec column (values
        # like 3600, 7200) to sla_duration via _parse_duration_hrs's pure-
        # number fallback.
        _DUR_EXPLICIT = re.compile(
            r'\d+:\d{2}|\d+\s*h(?:ours?)?|\d+\s*m(?:in(?:utes?)?)?', re.I
        )
        dur_hits = sum(1 for v in sample if _DUR_EXPLICIT.search(v))
        if dur_hits / n > 0.6 and "sla_duration" not in existing_canonical:
            extra[col] = "sla_duration"
            existing_canonical.add("sla_duration")
            continue

    # ── Assign time candidates in temporal order ──────────────────────────
    # First time column → start_time (if not yet mapped)
    # Second → sla_end_time  |  Third → expected_completion_time
    _time_slots = [
        ("start_time",              "start_time"),
        ("sla_end_time",            "sla_end_time"),
        ("expected_completion_time","expected_completion_time"),
    ]
    for col in time_candidates:
        for slot_key, canonical in _time_slots:
            if slot_key not in existing_canonical:
                extra[col] = canonical
                existing_canonical.add(slot_key)
                break

    return extra


# ── Time parsing ──────────────────────────────────────────────────────────────

_TIME_FORMATS = [
    "%I:%M %p", "%I.%M %p", "%I.%M%p",  # 9:00 PM, 10.45 AM (dot-notation Haleon)
    "%H:%M", "%I %p", "%I:%M%p",
    "%H:%M:%S", "%I:%M:%S %p", "%H:%M:%S.%f",
    "%I%p",   # 5AM (no colon/space)
]
_TZ_SUFFIX = re.compile(
    r'\s+(?:CST|CDT|EST|EDT|PST|PDT|MST|MDT|IST|GMT|UTC[+-]?\d*|PHT|ET|CT|PT|MT)\s*$',
    re.IGNORECASE)


def _parse_time(raw: str) -> Optional[dtime]:
    """Parse time string in various formats → datetime.time or None.

    Handles: 9:00 PM, 10.45 AM, 21:00, 5AM, 5AM CST, 11:00 AM (Sequencing SLA)
    """
    if not raw or str(raw).lower() in ("nan", "none", "", "nat"):
        return None
    raw = str(raw).strip()
    # Strip trailing timezone qualifiers: "5AM CST", "5AM IST"
    raw = _TZ_SUFFIX.sub('', raw).strip()
    # Strip parenthetical notes: "11:00 AM (Morning SLA)"
    raw = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
    # Extract leading time token to drop trailing noise
    _tm = re.match(r'^(\d{1,2}[.:]\d{2}(?::\d{2})?(?:\s*[AP]M)?|\d{1,2}\s*[AP]M|\d{1,2}[AP]M)', raw, re.I)
    if _tm:
        raw = _tm.group(1).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).time()
        except (ValueError, TypeError):
            continue
    m = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?', raw, re.I)
    if m:
        try:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ampm = (m.group(4) or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            elif ampm == "AM" and hh == 12:
                hh = 0
            return dtime(hh, mm)
        except Exception:
            pass
    return None


def _parse_duration_hrs(raw: str, start_time: Optional[dtime] = None) -> Optional[float]:
    """Parse a duration string → decimal hours.

    Handles all real-customer formats:
      HH:MM                    → hours + min/60
      Xh Ym / X hrs Y min      → composite
      X hours Y min / Xhr Ymin → composite (Dole, WESCO)
      X-Y hrs / X-Y min        → range midpoint
      X hrs+Y hrs               → engine+buffer sum (e.g. "11 hrs+4 hrs")
      9PM / 11:23 AM            → clock end-time (uses start_time param)
      Pure number               → hours if <24, else minutes
    """
    if not raw or str(raw).lower() in ("nan", "none", "", "nat"):
        return None
    raw = str(raw).strip()
    # Strip parenthetical suffixes: "(contracted)", "(2019)"
    raw = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()

    # ── Pattern 0: HH:MM[:SS] ────────────────────────────────────────────────
    m = re.match(r'^(\d{1,3}):(\d{2})(?::(\d{2}))?$', raw)
    if m:
        hrs = int(m.group(1)) + int(m.group(2)) / 60
        if m.group(3):
            hrs += int(m.group(3)) / 3600
        return round(hrs, 3)

    # ── Pattern 1: "X hrs+Y hrs" / "X hrs + Y hrs" (engine+buffer composite) ─
    m_plus = re.match(
        r'(\d+(?:\.\d+)?)\s*h[rs]*\s*\+\s*(\d+(?:\.\d+)?)\s*h[rs]*', raw, re.I)
    if m_plus:
        return round(float(m_plus.group(1)) + float(m_plus.group(2)), 3)

    # ── Pattern 2: "X-Y hrs" / "X–Y min" (range → midpoint) ─────────────────
    m_range = re.match(
        r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(h(?:ours?|rs?)?|min(?:utes?)?)?',
        raw, re.I)
    if m_range:
        lo, hi = float(m_range.group(1)), float(m_range.group(2))
        unit = (m_range.group(3) or "hrs").lower()
        mid = (lo + hi) / 2
        return round(mid / 60 if unit.startswith("min") else mid, 3)

    # ── Pattern 3: "X hours Y min" / "X hrs Y min" / "Xhr Ymin" ─────────────
    m_comp = re.search(
        r'(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?\s*(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?',
        raw, re.I)
    if m_comp:
        return round(float(m_comp.group(1)) + float(m_comp.group(2)) / 60, 3)

    # ── Pattern 4: plain hours/min components (allows one unit) ──────────────
    m_h = re.search(r'(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?', raw, re.I)
    hrs = float(m_h.group(1)) if m_h else 0.0
    m_m = re.search(r'(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?(?!\w)', raw, re.I)
    if m_m:
        hrs += float(m_m.group(1)) / 60
    if hrs > 0:
        return round(hrs, 3)

    # ── Pattern 5: clock end-time ("9PM", "11:23 AM") — needs start_time ─────
    if start_time is not None:
        end_t = _parse_time(raw)
        if end_t is not None:
            from datetime import date as _date, datetime as _dt
            base = _date(2000, 1, 1)
            dt_s = _dt.combine(base, start_time)
            dt_e = _dt.combine(base, end_t)
            delta_h = (dt_e - dt_s).total_seconds() / 3600.0
            if delta_h < 0:
                delta_h += 24.0  # overnight window
            if 0 < delta_h <= 24:
                return round(delta_h, 3)

    # ── Pattern 6: pure numeric (hours if <24, minutes otherwise) ────────────
    try:
        val = float(raw)
        if val <= 0:
            return None
        return round(val, 3) if val < 24 else round(val / 60, 3)
    except (ValueError, TypeError):
        return None


def _time_delta_hours(start: Optional[dtime], end: Optional[dtime]) -> Optional[float]:
    """Hours between two time objects. Handles overnight crossing."""
    if start is None or end is None:
        return None
    base = date(2000, 1, 1)
    dt_start = datetime.combine(base, start)
    dt_end = datetime.combine(base, end)
    delta = (dt_end - dt_start).total_seconds() / 3600.0
    if delta < 0:
        delta += 24.0
    return round(delta, 3) if delta > 0 else None


# ── Business-context interpretation (generic, customer-agnostic) ─────────────

_ACK_PATTERNS = re.compile(
    r"\b("
    r"no\s*breach|"
    r"not\s*a\s*breach|"
    r"agreed|"
    r"acceptable|"
    r"buffer\s*(agreed|approved|exists)|"
    r"approved\s*by|"
    r"system\s*(is\s*)?available\s*(around\s*)?\d+\s*[-–]?\s*\d*\s*min|"
    r"available\s*\d+\s*[-–]?\s*\d*\s*min\s*earlier|"
    r"spooling\s*jobs|"
    r"runs?\s*through(?:\s*out)?\s*the\s*day|"
    r"running\s*every\s*\d+\s*min"
    r")\b",
    re.I,
)
_CYCLIC_PATTERNS = re.compile(
    r"\b("
    r"cyclic|intraday|every\s*\d+\s*min|every\s*\d+\s*minutes|"
    r"continuous(?:ly)?|24[x×]7|round[\s-]*the[\s-]*clock"
    r")\b",
    re.I,
)


def _detect_business_ack(comments: str) -> bool:
    return bool(_ACK_PATTERNS.search(comments or ""))


def _detect_cyclic(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    return bool(_CYCLIC_PATTERNS.search(blob))


def _compute_buffer(window_hrs: Optional[float],
                    actual_window_hrs: Optional[float],
                    buffer_minutes: Optional[float]
                    ) -> Tuple[Optional[float], Optional[float]]:
    """Return (buffer_hrs, buffer_pct).

    Priority: explicit buffer column > derived from (window − actual).
    """
    if buffer_minutes and buffer_minutes > 0:
        b_hrs = buffer_minutes / 60.0
        b_pct = (b_hrs / window_hrs * 100.0) if window_hrs and window_hrs > 0 else None
        return round(b_hrs, 3), (round(b_pct, 1) if b_pct is not None else None)
    if window_hrs and actual_window_hrs is not None:
        b_hrs = window_hrs - actual_window_hrs
        b_pct = (b_hrs / window_hrs * 100.0) if window_hrs > 0 else None
        return round(b_hrs, 3), (round(b_pct, 1) if b_pct is not None else None)
    return None, None


def _derive_health(window_hrs: Optional[float],
                   actual_window_hrs: Optional[float],
                   buffer_pct: Optional[float],
                   business_ack: bool,
                   is_cyclic: bool
                   ) -> Tuple[str, str]:
    """Decide the traffic-light status. Returns (status, reason)."""
    if is_cyclic:
        return "CYCLIC", "Cyclic / intraday — SLA window not applicable"
    if actual_window_hrs is None or window_hrs is None:
        return "NO_DATA", "No actual end time — health undetermined"
    if actual_window_hrs > window_hrs:
        if business_ack:
            return "ACK", "Overrun acknowledged in comments"
        overrun = actual_window_hrs - window_hrs
        return "BREACH", f"Overran SLA by {overrun:.1f}h"
    # within window
    if buffer_pct is not None and buffer_pct < 10.0:
        return "AT_RISK", f"Only {buffer_pct:.0f}% buffer remaining"
    if business_ack:
        return "ACK", "Within SLA — business note acknowledged"
    return "OK", "Within SLA window"


def _parse_buffer_minutes(raw: str) -> Optional[float]:
    """Parse buffer/contingency value → minutes."""
    if not raw or str(raw).lower() in ("nan", "none", "", "nat", "0"):
        return None
    raw = str(raw).strip()
    m = re.search(r'(\d+(?:\.\d+)?)\s*m(?:in)?', raw, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*h', raw, re.I)
    if m:
        return float(m.group(1)) * 60
    try:
        val = float(raw)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class SlaContract:
    """One resolved SLA rule for a batch/schedule."""
    batch_name: str
    schedule_type: str          # DAILY | WEEKLY | MONTHLY | ADHOC | UNKNOWN
    schedule_raw: str           # original schedule text
    sla_model: str              # WINDOW | DURATION | BUFFERED | CALENDAR | PARTIAL | ASSUMED
    sla_start: Optional[dtime] = None
    sla_end: Optional[dtime] = None
    sla_duration_hrs: Optional[float] = None
    sla_window_hrs: Optional[float] = None   # computed from start→end or explicit
    buffer_minutes: Optional[float] = None
    first_job: str = ""
    last_job: str = ""
    timezone: str = ""
    comments: str = ""
    interpretation_notes: str = ""
    source_row: int = -1        # row index in original file
    source_sheet: str = ""
    completeness: str = "complete"  # complete | partial | missing
    # Pre-agreed buffer fields — set when file contains BOTH a target completion
    # time AND a hard SLA deadline (Gap 1 fix).
    expected_completion: Optional[dtime] = None   # target completion time (< sla_end)
    expected_window_hrs: Optional[float] = None   # start → expected_completion (hours)
    pre_agreed_buffer_hrs: Optional[float] = None # sla_end − expected_completion (contractual buffer)
    # Enrichment — derived from row + comments, used by UI/display gate
    actual_end: Optional[dtime] = None        # observed batch end time from file
    actual_window_hrs: Optional[float] = None # start → actual_end
    buffer_hrs: Optional[float] = None        # window − actual_window (or buffer_minutes/60)
    buffer_pct: Optional[float] = None        # buffer_hrs / window * 100
    business_acknowledged: bool = False       # comments waive breach (no breach / agreed)
    is_cyclic: bool = False                   # cyclic / intraday / every Nmin
    health_status: str = "NO_DATA"            # OK | AT_RISK | BREACH | ACK | CYCLIC | NO_DATA
    health_reason: str = ""                   # human-readable explanation
    # Two-tier SLA classification:
    #   JOB_SPECIFIC  — row has a real job/workflow name (not just a schedule-type word)
    #                   → this is an operational target, buffer is the decision factor
    #   SOW_SCHEDULE  — row is purely a schedule group (DAILY / WEEKLY / MONTHLY)
    #                   → this is the signed contract ceiling, breach = contract violation
    #   INFERRED      — could not determine which tier this belongs to
    sla_source_type: str = "INFERRED"         # JOB_SPECIFIC | SOW_SCHEDULE | INFERRED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_name": self.batch_name,
            "schedule_type": self.schedule_type,
            "schedule_raw": self.schedule_raw,
            "sla_model": self.sla_model,
            "sla_start": self.sla_start.isoformat() if self.sla_start else None,
            "sla_end": self.sla_end.isoformat() if self.sla_end else None,
            "sla_duration_hrs": self.sla_duration_hrs,
            "sla_window_hrs": self.sla_window_hrs,
            "buffer_minutes": self.buffer_minutes,
            "expected_completion": self.expected_completion.isoformat() if self.expected_completion else None,
            "expected_window_hrs": self.expected_window_hrs,
            "pre_agreed_buffer_hrs": self.pre_agreed_buffer_hrs,
            "first_job": self.first_job,
            "last_job": self.last_job,
            "timezone": self.timezone,
            "comments": self.comments,
            "interpretation_notes": self.interpretation_notes,
            "source_row": self.source_row,
            "source_sheet": self.source_sheet,
            "completeness": self.completeness,
            "actual_end": self.actual_end.isoformat() if self.actual_end else None,
            "actual_window_hrs": self.actual_window_hrs,
            "buffer_hrs": self.buffer_hrs,
            "buffer_pct": self.buffer_pct,
            "business_acknowledged": self.business_acknowledged,
            "is_cyclic": self.is_cyclic,
            "health_status": self.health_status,
            "health_reason": self.health_reason,
            "sla_source_type": self.sla_source_type,
        }


@dataclass
class SlaIngestResult:
    """Full result of ingesting an SLA file."""
    filename: str
    schema_type: str          # "window" | "duration" | "buffered" | "mixed" | "unrecognised"
    detected_model: str       # human-readable label
    # Two-tier classification at file level:
    #   JOB_MATRIX   — file has per-job/workflow rows (operational SLA matrix)
    #   SOW_SCHEDULE — file has only DAILY/WEEKLY/MONTHLY rows (signed contract)
    #   MIXED        — contains both (SOW wrapper + job-level specifics)
    contract_type: str = "MIXED"              # JOB_MATRIX | SOW_SCHEDULE | MIXED
    contracts: List[SlaContract] = field(default_factory=list)
    ceilings: Dict[str, float] = field(default_factory=dict)    # DAILY/WEEKLY/MONTHLY → hours (file-sourced only)
    missing_ceilings: List[str] = field(default_factory=list)   # schedule types absent from the file
    schedule_map: Dict[str, str] = field(default_factory=dict)  # batch_name → schedule_type
    warnings: List[Dict[str, str]] = field(default_factory=list)
    sections_detected: List[str] = field(default_factory=list)  # e.g. ["sla_table", "actuals", "comparison"]
    raw_columns: List[str] = field(default_factory=list)
    normalized_columns: Dict[str, str] = field(default_factory=dict)
    total_rows: int = 0
    valid_rows: int = 0
    partial_rows: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "schema_type": self.schema_type,
            "detected_model": self.detected_model,
            "contracts": [c.to_dict() for c in self.contracts],
            "ceilings": self.ceilings,
            "missing_ceilings": self.missing_ceilings,
            "schedule_map": self.schedule_map,
            "warnings": self.warnings,
            "sections_detected": self.sections_detected,
            "raw_columns": self.raw_columns,
            "normalized_columns": self.normalized_columns,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
            "partial_rows": self.partial_rows,
            "contract_type": self.contract_type,
        }


@dataclass
class ResolvedSla:
    """Result of resolving the right SLA for a specific batch execution."""
    sla_hrs: float
    sla_model: str              # WINDOW | DURATION | BUFFERED | ASSUMED
    source: str                 # "sla_matrix" | "customer_fallback" | "assumed"
    source_detail: str          # human-readable description
    schedule_type: str          # DAILY | WEEKLY | MONTHLY | ADHOC | UNKNOWN
    matched_contract: Optional[SlaContract] = None
    buffer_minutes: Optional[float] = None
    confidence: str = "high"    # high | medium | low
    blocked: bool = False       # True → cannot produce green compliance
    block_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sla_hrs": self.sla_hrs,
            "sla_model": self.sla_model,
            "source": self.source,
            "source_detail": self.source_detail,
            "schedule_type": self.schedule_type,
            "buffer_minutes": self.buffer_minutes,
            "confidence": self.confidence,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "matched_contract": self.matched_contract.to_dict() if self.matched_contract else None,
        }


@dataclass
class SlaVerdict:
    """Result of comparing actual execution against resolved SLA."""
    status: str                 # PASS | BREACH | AT_RISK | PASS_WITH_BUFFER | FAIL
    strict_status: str          # PASS | BREACH (without buffer)
    buffered_status: str        # PASS | BREACH (with buffer applied)
    actual_hrs: float
    sla_hrs: float
    margin_hrs: float           # positive = headroom, negative = overrun
    buffer_applied: bool
    buffer_margin_hrs: Optional[float] = None
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "strict_status": self.strict_status,
            "buffered_status": self.buffered_status,
            "actual_hrs": round(self.actual_hrs, 3),
            "sla_hrs": round(self.sla_hrs, 3),
            "margin_hrs": round(self.margin_hrs, 3),
            "buffer_applied": self.buffer_applied,
            "buffer_margin_hrs": round(self.buffer_margin_hrs, 3) if self.buffer_margin_hrs is not None else None,
            "explanation": self.explanation,
        }


# ── Section boundary detection ────────────────────────────────────────────────

_SECTION_MARKERS = {
    "sla_table":  re.compile(r"batch\s*name|schedule|expected\s*end|sla\s*window", re.I),
    "actuals":    re.compile(r"actual\s*start|actual\s*end|current\s*end|date.wise|sample", re.I),
    "comparison": re.compile(r"test\s*vs\s*prod|prod\s*vs\s*test|comparison|environment", re.I),
    "contingency": re.compile(r"contingency|buffer|grace|pass.?fail\s*after", re.I),
    "notes":      re.compile(r"note|comment|remark|assumption|interpretation", re.I),
}


def _detect_sections(df_raw) -> List[str]:
    """Detect logical sections in a worksheet by scanning all cell text."""
    import pandas as pd
    found = set()
    text_blob = " ".join(
        str(v) for v in df_raw.values.flatten()
        if pd.notna(v) and str(v).strip()
    )[:5000]
    for section, pat in _SECTION_MARKERS.items():
        if pat.search(text_blob):
            found.add(section)
    return sorted(found)


# ── File ingestion ────────────────────────────────────────────────────────────

def _truncate_at_secondary_header(df_raw):
    """Detect a mid-sheet secondary header and truncate the dataframe.

    Real-world SLA workbooks frequently stack a second table below the first
    (e.g. per-run actuals after the SLA contract list, separated by blank
    rows). We detect this by scanning every row for cells whose VALUES look
    like header tokens (``DATE``, ``DAY``, ``START TIME``, ``END TIME``,
    ``Duration``, ``First Job``, etc.). If at least two such tokens appear
    in a single row AND that row sits below the first row, we truncate
    everything from that row onward — those are not SLA contracts, those
    are run-history records masquerading as them.
    """
    import pandas as pd
    if df_raw is None or df_raw.empty:
        return df_raw
    header_tokens = re.compile(
        r"^(date|day|start\s*time|end\s*time|duration|first\s*job|"
        r"last\s*job|run\s*time|job\s*name|status|run\s*date)\b",
        re.I,
    )
    for i in range(1, len(df_raw)):
        row = df_raw.iloc[i]
        hits = 0
        for v in row.values:
            if pd.notna(v) and isinstance(v, str) and header_tokens.match(v.strip()):
                hits += 1
                if hits >= 2:
                    return df_raw.iloc[:i].copy()
    return df_raw


def _ingest_sla_sheet(df_raw, sheet_name: str, result: "SlaIngestResult") -> int:
    """Parse a single sheet's dataframe into contracts. Returns rows added."""
    import pandas as pd

    df_raw = _truncate_at_secondary_header(df_raw)
    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    if not result.raw_columns:
        result.raw_columns = list(df_raw.columns)
    result.total_rows += len(df_raw)

    # Normalize column names
    col_map = {raw: _normalize_column(raw) for raw in df_raw.columns}
    df = df_raw.rename(columns=col_map)

    # Coalesce duplicate canonical columns (see comment below)
    if not df.columns.is_unique:
        seen: Dict[str, int] = {}
        new_frames = {}
        order: List[str] = []
        for col in df.columns:
            if col in seen:
                continue
            seen[col] = 1
            same = [c for c in df.columns if c == col]
            if len(same) == 1:
                new_frames[col] = df[col]
            else:
                block = df.loc[:, same]
                new_frames[col] = block.bfill(axis=1).iloc[:, 0]
            order.append(col)
        df = pd.DataFrame({c: new_frames[c] for c in order})

    # ── Value-based column inference fallback ────────────────────────────────
    # After header-based normalization, any column whose canonical name is NOT
    # in _KNOWN_CANONICAL is "unmapped".  Scan the cell VALUES to infer what
    # the column actually represents — handles arbitrary customer naming.
    _already_mapped = set(df.columns) & _KNOWN_CANONICAL
    _extra_map = _infer_unmapped_columns(df, _already_mapped)
    if _extra_map:
        df = df.rename(columns=_extra_map)
        col_map.update(_extra_map)
    if not result.normalized_columns:
        result.normalized_columns = col_map

    has_start       = "start_time"              in df.columns
    has_end         = "sla_end_time"             in df.columns
    has_exp_compl   = "expected_completion_time" in df.columns   # target completion (Gap 1)
    has_duration    = "sla_duration"             in df.columns
    has_buffer      = "buffer"                   in df.columns
    has_schedule    = "schedule_text"            in df.columns
    has_batch_name  = "batch_name"               in df.columns

    def _cell(row, key: str) -> str:
        try:
            v = row.get(key, "")
        except Exception:
            v = ""
        if hasattr(v, "iloc"):
            try:
                v = v.dropna().iloc[0] if v.dropna().size else ""
            except Exception:
                v = ""
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except Exception:
            pass
        s = str(v).strip()
        if "dtype:" in s and "Name:" in s:
            return ""
        if s.lower() in ("nan", "none", "nat"):
            return ""
        return s

    added = 0
    for idx, row in df.iterrows():
        batch_name    = _cell(row, "batch_name")    if has_batch_name else ""
        schedule_raw  = _cell(row, "schedule_text") if has_schedule   else ""
        schedule_type = classify_schedule(schedule_raw or batch_name)

        start_t = _parse_time(_cell(row, "start_time"))   if has_start    else None
        end_cell = _cell(row, "sla_end_time") if has_end else ""
        end_t    = _parse_time(end_cell) if end_cell else None
        dur_hrs  = _parse_duration_hrs(_cell(row, "sla_duration")) if has_duration else None
        # Generic fallback: the "Expected End Time/SLA" column sometimes holds a
        # duration string ('1.5 hrs', '45 min') rather than a clock time. If
        # _parse_time returned None and we have no explicit duration column, try
        # interpreting the same cell as a duration.
        if end_t is None and end_cell and dur_hrs is None:
            dur_hrs = _parse_duration_hrs(end_cell)
        window_hrs = _time_delta_hours(start_t, end_t)
        if window_hrs is None and dur_hrs is not None:
            window_hrs = dur_hrs

        # ── Pre-agreed buffer: expected completion vs hard SLA deadline (Gap 1) ────
        # When the SLA file has BOTH a target-completion column AND a hard SLA
        # deadline, compute the contractual buffer (SLA deadline − target completion).
        # This lets us show: "Contract says batch targets 6 AM, SLA is 7 AM → 1h buffer."
        # Parse buffer_min FIRST so the fallback derivation below can check it.
        buffer_min = _parse_buffer_minutes(_cell(row, "buffer")) if has_buffer else None
        exp_compl_t        = (_parse_time(_cell(row, "expected_completion_time"))
                              if has_exp_compl else None)
        expected_window_hrs = _time_delta_hours(start_t, exp_compl_t) if exp_compl_t else None
        pre_agreed_buf_hrs  = _time_delta_hours(exp_compl_t, end_t)   if (exp_compl_t and end_t) else None
        # Derive buffer_min from pre_agreed when no explicit buffer column
        if pre_agreed_buf_hrs and pre_agreed_buf_hrs > 0 and not buffer_min:
            buffer_min = round(pre_agreed_buf_hrs * 60, 1)
        # When only expected_completion column exists (no separate sla_end), treat it
        # as the SLA deadline for backward-compat (single-column customers).
        if end_t is None and exp_compl_t is not None:
            end_t      = exp_compl_t
            window_hrs = expected_window_hrs
            exp_compl_t        = None   # no separate target vs deadline in this case
            expected_window_hrs = None
            pre_agreed_buf_hrs  = None

        first_job  = _cell(row, "first_job") if "first_job" in df.columns else ""
        last_job   = _cell(row, "last_job")  if "last_job"  in df.columns else ""
        tz         = _cell(row, "timezone")  if "timezone"  in df.columns else ""
        comments   = _cell(row, "comments")  if "comments"  in df.columns else ""

        # Fallback: capture timezone embedded in time-cell values (e.g. '7:00 AM IST')
        if not tz:
            for _tc in ("start_time", "sla_end_time", "expected_completion_time"):
                if _tc in df.columns:
                    _tz_found = _extract_tz_from_value(_cell(row, _tc))
                    if _tz_found:
                        tz = _tz_found
                        break

        if not batch_name and not schedule_raw and window_hrs is None and dur_hrs is None:
            continue

        if window_hrs is not None and window_hrs > 0:
            completeness = "complete"
            result.valid_rows += 1
        elif dur_hrs is not None and dur_hrs > 0:
            completeness = "complete"
            result.valid_rows += 1
        elif batch_name or schedule_raw:
            completeness = "partial"
            result.partial_rows += 1
        else:
            continue

        if (has_buffer and buffer_min and buffer_min > 0 and window_hrs) or (
                pre_agreed_buf_hrs and pre_agreed_buf_hrs > 0):  # implicit buffer from target+deadline
            row_model = "BUFFERED"
        elif window_hrs is not None:
            row_model = "WINDOW"
        elif dur_hrs is not None:
            row_model = "DURATION"
        elif schedule_raw:
            row_model = "CALENDAR"
        else:
            row_model = "PARTIAL"

        interp_notes = ""
        if comments and comments.lower() not in ("nan", "none", ""):
            if re.search(r"unofficial|no breach|agreed|buffer|optimizer|existing", comments, re.I):
                interp_notes = f"Business context: {comments[:200]}"
            else:
                interp_notes = comments[:200]

        contract = SlaContract(
            batch_name=batch_name or f"Row_{idx}",
            schedule_type=schedule_type,
            schedule_raw=schedule_raw,
            sla_model=row_model,
            sla_start=start_t,
            sla_end=end_t,
            sla_duration_hrs=dur_hrs,
            sla_window_hrs=window_hrs,
            buffer_minutes=buffer_min,
            expected_completion=exp_compl_t,
            expected_window_hrs=expected_window_hrs,
            pre_agreed_buffer_hrs=pre_agreed_buf_hrs,
            first_job=first_job,
            last_job=last_job,
            timezone=tz,
            comments=comments,
            interpretation_notes=interp_notes,
            source_row=int(idx) + 2,
            source_sheet=sheet_name,
            completeness=completeness,
        )
        # ── Enrichment: actual end + derived buffer + health traffic-light ──
        actual_end_t = (
            _parse_time(_cell(row, "actual_end_time"))
            if "actual_end_time" in df.columns else None
        )
        actual_window_hrs = _time_delta_hours(start_t, actual_end_t) if actual_end_t else None
        buffer_hrs, buffer_pct = _compute_buffer(window_hrs, actual_window_hrs, buffer_min)
        is_cyclic_flag        = _detect_cyclic(comments, schedule_raw, batch_name)
        business_ack_flag     = _detect_business_ack(comments)
        health_status, health_reason = _derive_health(
            window_hrs, actual_window_hrs, buffer_pct, business_ack_flag, is_cyclic_flag,
        )
        contract.actual_end           = actual_end_t
        contract.actual_window_hrs    = actual_window_hrs
        contract.buffer_hrs           = buffer_hrs
        contract.buffer_pct           = buffer_pct
        contract.business_acknowledged = business_ack_flag
        contract.is_cyclic            = is_cyclic_flag
        contract.health_status        = health_status
        contract.health_reason        = health_reason

        # ── Two-tier SLA classification per contract ────────────────────────────
        # SOW_SCHEDULE: batch_name is ONLY a schedule-type word (even with env prefix)
        #   e.g.  "DAILY", "PROD_WEEKLY", "W" → SOW_SCHEDULE
        # JOB_SPECIFIC: batch_name has real job/workflow tokens beyond schedule type
        #   e.g.  "PROD_WEEKLY_WF1_REQPL", "WELLA_PIPO_GMPLPOPT_WLY" → JOB_SPECIFIC
        _SOW_BATCH_NAMES_SET = {
            "DAILY", "WEEKLY", "MONTHLY", "ADHOC", "AD HOC",
            "NIGHTLY", "OVERNIGHT", "CUSTOM", "D", "W", "M",
            "BIWEEKLY", "BI-WEEKLY", "WEEKEND", "WEEKDAY",
        }
        _bn_upper = batch_name.upper().strip()
        # Strip common env-prefix tokens before tokenising
        _bn_normed = re.sub(
            r'^(PROD|TEST|UAT|DEV|SIT|INT|PRE|PREPROD|QA)[_\s\-]+',
            '', _bn_upper
        )
        _bn_tokens = set(_bn_normed.replace("-", "_").replace(" ", "_").split("_"))
        _bn_tokens.discard("")  # remove empty strings
        _is_sow_only = (
            len(_bn_tokens) == 1
            and next(iter(_bn_tokens), "") in _SOW_BATCH_NAMES_SET
        )
        _is_placeholder = contract.batch_name.startswith("Row_") or not batch_name
        contract.sla_source_type = (
            "SOW_SCHEDULE" if _is_sow_only
            else ("INFERRED" if _is_placeholder else "JOB_SPECIFIC")
        )

        result.contracts.append(contract)
        added += 1

        if batch_name:
            result.schedule_map[batch_name] = schedule_type

    return added


def ingest_sla_file(raw_bytes: bytes, filename: str) -> SlaIngestResult:
    """Ingest an SLA file and extract structured contracts.

    Pipeline: ingest → walk every sheet → detect layout → normalize columns →
              parse schedule → resolve SLA model → build contracts.

    Multi-sheet support: real customer SLA workbooks (e.g. WESCO) stack
    summary contracts on one sheet and the per-batch detailed SLAs on
    another. Reading only sheet 0 silently misses the most important data.
    Every sheet is parsed; contracts are merged.
    """
    import pandas as pd

    result = SlaIngestResult(filename=filename, schema_type="unrecognised",
                              detected_model="Unknown")

    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""

    sheet_frames: List[Tuple[str, "pd.DataFrame"]] = []
    try:
        if ext in ("xlsx", "xls"):
            xl = pd.ExcelFile(io.BytesIO(raw_bytes), engine="openpyxl")
            for sh in xl.sheet_names:
                try:
                    sheet_frames.append((sh, xl.parse(sh)))
                except Exception:
                    continue
        elif ext == "csv":
            sheet_frames.append(("csv", pd.read_csv(io.BytesIO(raw_bytes))))
        else:
            result.warnings.append({"code": "UNSUPPORTED_FORMAT",
                                     "text": f"File type .{ext} not supported for SLA parsing"})
            return result
    except Exception as exc:
        result.warnings.append({"code": "PARSE_FAILED",
                                 "text": f"Could not read file: {exc}"})
        return result

    # Section detection — run on the highest-priority sheet that has SLA columns
    if sheet_frames:
        # Priority: Sheet1 > Batch Details > C&A > BatchSLA > SLA > others
        _SHEET_PRIORITY = ["sheet1", "batch details", "c&a", "batchsla", "sla"]
        def _sheet_rank(name: str) -> int:
            n = name.lower().strip()
            for i, s in enumerate(_SHEET_PRIORITY):
                if n == s:
                    return i
            return len(_SHEET_PRIORITY)
        sorted_frames = sorted(sheet_frames, key=lambda t: _sheet_rank(t[0]))
        try:
            result.sections_detected = _detect_sections(sorted_frames[0][1])
        except Exception:
            pass

    # Walk every sheet and merge contracts. We treat the schema as the union
    # of capabilities seen across sheets — if any sheet has start+end+buffer
    # we call the file BUFFERED, else WINDOW, etc.
    has_start_any = has_end_any = has_dur_any = has_buf_any = has_sched_any = has_batch_any = has_exp_compl_any = False
    sheets_with_data = 0
    for sh_name, df_raw in sheet_frames:
        if df_raw is None or df_raw.empty:
            continue
        cols = {_normalize_column(str(c)) for c in df_raw.columns}
        if not ({"batch_name", "schedule_text", "sla_end_time",
                 "sla_duration", "start_time", "expected_completion_time"} & cols):
            # Sheet has no SLA-shaped columns — skip
            continue
        has_start_any    |= "start_time"              in cols
        has_end_any      |= "sla_end_time"            in cols
        has_dur_any      |= "sla_duration"            in cols
        has_buf_any      |= "buffer"                  in cols
        has_sched_any    |= "schedule_text"           in cols
        has_batch_any    |= "batch_name"              in cols
        has_exp_compl_any|= "expected_completion_time" in cols  # Gap 1

        added = _ingest_sla_sheet(df_raw, sh_name, result)
        if added:
            sheets_with_data += 1

    # Detect overall schema type from union of capabilities
    if has_start_any and has_end_any and (has_buf_any or has_exp_compl_any):
        result.schema_type = "buffered"
        result.detected_model = "Window + Contingency SLA (start→end with buffer)"
    elif has_start_any and has_end_any:
        result.schema_type = "window"
        result.detected_model = "Absolute Window SLA (start→end cutoff)"
    elif has_dur_any:
        result.schema_type = "duration"
        result.detected_model = "Duration-based SLA (expected runtime)"
    elif has_sched_any and not has_end_any and not has_dur_any:
        result.schema_type = "calendar"
        result.detected_model = "Schedule-only (no numeric SLA provided)"
    elif sheets_with_data:
        result.schema_type = "mixed"
        result.detected_model = "Mixed/incomplete SLA structure"

    # Build ceilings (widest SLA per schedule type, across all sheets)
    # Use MAX because the global ceiling represents the overall batch window,
    # not individual workflow SLAs.  Per-workflow compliance is reported
    # separately via workflow_sla_summary.
    for contract in result.contracts:
        if contract.sla_window_hrs and contract.sla_window_hrs > 0:
            sched = contract.schedule_type
            if sched in ("DAILY", "WEEKLY", "MONTHLY"):
                existing = result.ceilings.get(sched)
                if existing is None or contract.sla_window_hrs > existing:
                    result.ceilings[sched] = contract.sla_window_hrs

    # Generate warnings
    if result.partial_rows > 0:
        result.warnings.append({
            "code": "PARTIAL_ROWS",
            "text": f"{result.partial_rows} row(s) have batch names but no numeric SLA — "
                    "these will use assumed defaults.",
            "severity": "warning",
        })
    if result.valid_rows == 0:
        result.warnings.append({
            "code": "NO_VALID_SLA",
            "text": "No valid SLA windows could be extracted from this file. "
                    "Check that the file has Start Time + Expected End Time columns.",
            "severity": "critical",
        })
    if "MONTHLY" not in result.ceilings:
        result.missing_ceilings.append("MONTHLY")
        result.warnings.append({
            "code": "MISSING_MONTHLY",
            "text": "No MONTHLY SLA found in file — no default applied. Monthly jobs will be assessed without a ceiling.",
            "severity": "warning",
        })

    # ── Compute file-level contract_type from the mix of sla_source_type labels ──
    # JOB_MATRIX   ≥ 70% of contracts are JOB_SPECIFIC
    # SOW_SCHEDULE ≥ 70% of contracts are SOW_SCHEDULE
    # MIXED        — anything else (heterogeneous or INFERRED-dominated)
    _total_c  = len(result.contracts)
    if _total_c > 0:
        _n_job  = sum(1 for c in result.contracts if c.sla_source_type == "JOB_SPECIFIC")
        _n_sow  = sum(1 for c in result.contracts if c.sla_source_type == "SOW_SCHEDULE")
        if _n_job / _total_c >= 0.70:
            result.contract_type = "JOB_MATRIX"
        elif _n_sow / _total_c >= 0.70:
            result.contract_type = "SOW_SCHEDULE"
        else:
            result.contract_type = "MIXED"

    return result


# ── Legacy single-sheet ingestion (kept for back-compat / tests) ─────────────

def _ingest_sla_file_single_sheet(raw_bytes: bytes, filename: str) -> SlaIngestResult:
    """Original single-sheet ingestion path. Retained for compatibility."""
    import pandas as pd

    result = SlaIngestResult(filename=filename, schema_type="unrecognised",
                              detected_model="Unknown")

    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""

    try:
        if ext in ("xlsx", "xls"):
            df_raw = pd.read_excel(io.BytesIO(raw_bytes), sheet_name=0, engine="openpyxl")
        elif ext == "csv":
            df_raw = pd.read_csv(io.BytesIO(raw_bytes))
        else:
            result.warnings.append({"code": "UNSUPPORTED_FORMAT",
                                     "text": f"File type .{ext} not supported for SLA parsing"})
            return result
    except Exception as exc:
        result.warnings.append({"code": "PARSE_FAILED",
                                 "text": f"Could not read file: {exc}"})
        return result

    df_raw.columns = [str(c).strip() for c in df_raw.columns]
    result.raw_columns = list(df_raw.columns)
    result.total_rows = len(df_raw)

    # Detect sections in the worksheet
    result.sections_detected = _detect_sections(df_raw)

    # Normalize column names
    col_map = {}
    for raw_col in df_raw.columns:
        canonical = _normalize_column(raw_col)
        col_map[raw_col] = canonical
    result.normalized_columns = col_map

    # Rename for processing
    df = df_raw.rename(columns=col_map)

    # ── Coalesce duplicate canonical columns ─────────────────────────────
    # Several customer headers (e.g. "Batch Name", "Module", "Window Name")
    # all map to the canonical "batch_name". After rename, df can contain
    # multiple columns with the same name, which makes ``row.get(name)``
    # return a Series instead of a scalar — and ``str(Series)`` produces
    # the infamous  "batch_name  NaN\nbatch_name  NaN\nName: 3, dtype: object"
    # garbage that leaks straight into the dashboard.
    #
    # Fix: for every duplicated canonical name, collapse the columns by
    # taking the first non-null value across them per row, then drop the
    # extras. This also de-noises rows where one source column is blank
    # but another carries the real value.
    if not df.columns.is_unique:
        seen: Dict[str, int] = {}
        new_frames = {}
        order: List[str] = []
        for col in df.columns:
            if col in seen:
                continue
            seen[col] = 1
            same = [c for c in df.columns if c == col]
            if len(same) == 1:
                new_frames[col] = df[col]
            else:
                # Coalesce: bfill across the duplicate columns, take the first.
                block = df.loc[:, same]
                new_frames[col] = block.bfill(axis=1).iloc[:, 0]
            order.append(col)
        df = pd.DataFrame({c: new_frames[c] for c in order})

    # Detect SLA model type from available columns
    has_start = "start_time" in df.columns
    has_end = "sla_end_time" in df.columns
    has_duration = "sla_duration" in df.columns
    has_buffer = "buffer" in df.columns
    has_schedule = "schedule_text" in df.columns
    has_batch_name = "batch_name" in df.columns

    if has_start and has_end and has_buffer:
        schema_type = "buffered"
        detected_model = "Window + Contingency SLA (start→end with buffer)"
    elif has_start and has_end:
        schema_type = "window"
        detected_model = "Absolute Window SLA (start→end cutoff)"
    elif has_duration:
        schema_type = "duration"
        detected_model = "Duration-based SLA (expected runtime)"
    elif has_schedule and not has_end and not has_duration:
        schema_type = "calendar"
        detected_model = "Schedule-only (no numeric SLA provided)"
    else:
        schema_type = "mixed"
        detected_model = "Mixed/incomplete SLA structure"

    result.schema_type = schema_type
    result.detected_model = detected_model

    # Parse each row into an SLA contract
    valid_count = 0
    partial_count = 0

    def _cell(row, key: str) -> str:
        """Return a clean scalar string for a row cell.

        Defensive against pandas Series that can leak through duplicate
        columns, NaN floats, and stringified-Series garbage of the form
        ``"batch_name  NaN\\nName: 3, dtype: object"``.
        """
        try:
            v = row.get(key, "")
        except Exception:
            v = ""
        # Series → take first non-null scalar
        if hasattr(v, "iloc"):
            try:
                v = v.dropna().iloc[0] if v.dropna().size else ""
            except Exception:
                v = ""
        if v is None:
            return ""
        try:
            import pandas as _pd
            if _pd.isna(v):
                return ""
        except Exception:
            pass
        s = str(v).strip()
        # Drop pandas Series/DataFrame repr leaks
        if "dtype:" in s and "Name:" in s:
            return ""
        if s.lower() in ("nan", "none", "nat"):
            return ""
        return s

    for idx, row in df.iterrows():
        batch_name    = _cell(row, "batch_name")    if has_batch_name else ""
        schedule_raw  = _cell(row, "schedule_text") if has_schedule   else ""
        schedule_type = classify_schedule(schedule_raw or batch_name)

        # Parse times
        start_t  = _parse_time(_cell(row, "start_time"))   if has_start    else None
        end_cell = _cell(row, "sla_end_time") if has_end else ""
        end_t    = _parse_time(end_cell) if end_cell else None
        dur_hrs  = _parse_duration_hrs(_cell(row, "sla_duration")) if has_duration else None
        # Generic fallback: the "Expected End Time/SLA" column sometimes holds a
        # duration string ('1.5 hrs', '45 min') rather than a clock time. If
        # _parse_time returned None and we have no explicit duration column, try
        # interpreting the same cell as a duration.
        if end_t is None and end_cell and dur_hrs is None:
            dur_hrs = _parse_duration_hrs(end_cell)

        # Compute window from start/end
        window_hrs = _time_delta_hours(start_t, end_t)

        # If no explicit window but we have duration, use that
        if window_hrs is None and dur_hrs is not None:
            window_hrs = dur_hrs

        # Parse buffer
        buffer_min = _parse_buffer_minutes(_cell(row, "buffer")) if has_buffer else None

        # Parse jobs, timezone, comments
        first_job = _cell(row, "first_job") if "first_job" in df.columns else ""
        last_job  = _cell(row, "last_job")  if "last_job"  in df.columns else ""
        tz        = _cell(row, "timezone")  if "timezone"  in df.columns else ""
        comments  = _cell(row, "comments")  if "comments"  in df.columns else ""

        # Skip completely empty / placeholder rows. A row with only a
        # Row_<idx> autogenerated batch_name and no other signal is noise.
        if not batch_name and not schedule_raw and window_hrs is None and dur_hrs is None:
            continue

        # Determine completeness
        if window_hrs is not None and window_hrs > 0:
            completeness = "complete"
            valid_count += 1
        elif dur_hrs is not None and dur_hrs > 0:
            completeness = "complete"
            valid_count += 1
        elif batch_name or schedule_raw:
            completeness = "partial"
            partial_count += 1
        else:
            continue

        # Determine per-row SLA model
        if has_buffer and buffer_min and buffer_min > 0 and window_hrs:
            row_model = "BUFFERED"
        elif window_hrs is not None:
            row_model = "WINDOW"
        elif dur_hrs is not None:
            row_model = "DURATION"
        elif schedule_raw:
            row_model = "CALENDAR"
        else:
            row_model = "PARTIAL"

        # Build interpretation notes from comments
        interp_notes = ""
        if comments and comments.lower() not in ("nan", "none", ""):
            # Check for business-logic keywords in comments
            if re.search(r"unofficial|no breach|agreed|buffer|optimizer|existing", comments, re.I):
                interp_notes = f"Business context: {comments[:200]}"
            else:
                interp_notes = comments[:200]

        contract = SlaContract(
            batch_name=batch_name or f"Row_{idx}",
            schedule_type=schedule_type,
            schedule_raw=schedule_raw,
            sla_model=row_model,
            sla_start=start_t,
            sla_end=end_t,
            sla_duration_hrs=dur_hrs,
            sla_window_hrs=window_hrs,
            buffer_minutes=buffer_min,
            first_job=first_job,
            last_job=last_job,
            timezone=tz,
            comments=comments,
            interpretation_notes=interp_notes,
            source_row=int(idx) + 2,  # +2 for header row + 0-based
            source_sheet="Sheet1",
            completeness=completeness,
        )
        result.contracts.append(contract)

        # Build schedule_map
        if batch_name:
            result.schedule_map[batch_name] = schedule_type

    result.valid_rows = valid_count
    result.partial_rows = partial_count

    # Build ceilings (widest SLA per schedule type)
    # Use MAX — the global ceiling is for batch window compliance.
    # Per-workflow SLA is enforced separately via workflow_sla_summary.
    for contract in result.contracts:
        if contract.sla_window_hrs and contract.sla_window_hrs > 0:
            sched = contract.schedule_type
            if sched in ("DAILY", "WEEKLY", "MONTHLY"):
                existing = result.ceilings.get(sched)
                if existing is None or contract.sla_window_hrs > existing:
                    result.ceilings[sched] = contract.sla_window_hrs

    # Generate warnings
    if partial_count > 0:
        result.warnings.append({
            "code": "PARTIAL_ROWS",
            "text": f"{partial_count} row(s) have batch names but no numeric SLA — "
                    "these will use assumed defaults.",
            "severity": "warning",
        })

    if valid_count == 0:
        result.warnings.append({
            "code": "NO_VALID_SLA",
            "text": "No valid SLA windows could be extracted from this file. "
                    "All compliance results will use assumed defaults.",
            "severity": "critical",
        })

    # Check for ADHOC entries
    adhoc_count = sum(1 for c in result.contracts if c.schedule_type == "ADHOC")
    if adhoc_count > 0:
        result.warnings.append({
            "code": "ADHOC_ENTRIES",
            "text": f"{adhoc_count} ADHOC entries found — these have no fixed SLA.",
            "severity": "info",
        })

    # Check for missing schedule types in ceilings
    for sched in ("DAILY", "WEEKLY", "MONTHLY"):
        if sched not in result.ceilings:
            from services import pe_config
            defaults = {
                "DAILY": pe_config.SLA_DAILY_HRS,
                "WEEKLY": pe_config.SLA_WEEKLY_HRS,
                "MONTHLY": pe_config.SLA_MONTHLY_HRS,
            }
            result.ceilings[sched] = defaults[sched]
            result.warnings.append({
                "code": f"MISSING_{sched}",
                "text": f"No {sched} SLA found in file — using default {defaults[sched]}h",
                "severity": "info",
            })

    # Dimension 7: surface unknown timezone abbreviations
    try:
        from services.pe_config import resolve_timezone as _rtz
        _tz_unknowns = set()
        for _c in result.contracts:
            if _c.timezone:
                _, _known = _rtz(_c.timezone)
                if not _known:
                    _tz_unknowns.add(_c.timezone.upper())
        if _tz_unknowns:
            result.warnings.append({
                "code": "UNKNOWN_TIMEZONE",
                "text": (
                    f"Unrecognised timezone abbreviation(s): {', '.join(sorted(_tz_unknowns))}. "
                    "Treated as UTC. PE to verify actual timezone before sign-off."
                ),
                "severity": "warning",
            })
    except Exception:
        pass

    logger.info(
        "sla_engine: ingested %s — schema=%s, contracts=%d, valid=%d, partial=%d, ceilings=%s",
        filename, schema_type, len(result.contracts), valid_count, partial_count, result.ceilings,
    )

    return result


# ── SLA Resolution ────────────────────────────────────────────────────────────

def _normalize_batch_name(name: str) -> str:
    """Strip environment prefix (PROD_, TEST_, DEV_, UAT_, SIT_, STG_) from a
    batch name so that PROD_WEEKLY_WF1_REQPL and TEST_WEEKLY_WF1_REQPL both
    resolve to the same canonical suffix WEEKLY_WF1_REQPL.

    Works generically — no hardcoded env list.  Strategy:
      - Split on underscore.
      - If the first segment is a known env token OR is ≤4 chars (typical
        abbreviated env prefix), drop it and keep the rest.
      - If the name has only 1 segment, keep it as-is.
    """
    _ENV_TOKENS = frozenset({
        "PROD", "TEST", "DEV", "UAT", "SIT", "STG", "DR", "PRD",
        "TST", "QA", "PREPROD", "NONPROD", "SANDBOX", "STAGE",
    })
    parts = name.upper().split("_")
    if len(parts) <= 1:
        return name.upper()
    # Drop first segment if it looks like an environment prefix
    if parts[0] in _ENV_TOKENS or len(parts[0]) <= 4:
        return "_".join(parts[1:])
    return "_".join(parts)


def _sla_from_contract(contract: SlaContract) -> float:
    """Extract the usable SLA hours from a contract (window preferred over duration)."""
    return contract.sla_window_hrs or contract.sla_duration_hrs or 0.0


def resolve_sla(
    batch_name: str,
    schedule_hint: str,
    contracts: List[SlaContract],
    ceilings: Dict[str, float],
) -> ResolvedSla:
    """Resolve the correct SLA for a specific batch execution.

    Match priority (high → low confidence):
      1. Exact match         — case-insensitive full name match
      2. Env-prefix stripped — PROD_WEEKLY_WF1 matches TEST_WEEKLY_WF1
      3. Token-set overlap   — WF1_REQPL matches BYC_WF1_REQPL (longest
                               common token sequence wins)
      4. Schedule-type from  — per-customer ceiling from uploaded SLA file
         customer file
      5. System default      — assumed / blocked (no customer data)
    """
    from services import pe_config

    if not batch_name:
        batch_name = ""

    batch_upper  = batch_name.upper()
    batch_norm   = _normalize_batch_name(batch_name)   # env-prefix stripped
    batch_tokens = set(batch_norm.split("_"))          # token set for overlap
    schedule_type = classify_schedule(schedule_hint or batch_name or "")

    # Only consider contracts with a usable SLA value
    valid = [c for c in contracts if _sla_from_contract(c) > 0]

    # ── Pass 1: exact match ──────────────────────────────────────
    for contract in valid:
        if contract.batch_name.upper() == batch_upper:
            return ResolvedSla(
                sla_hrs=_sla_from_contract(contract),
                sla_model=contract.sla_model,
                source="sla_matrix",
                source_detail=f"Exact match '{contract.batch_name}' (row {contract.source_row})",
                schedule_type=contract.schedule_type or schedule_type,
                matched_contract=contract,
                buffer_minutes=contract.buffer_minutes,
                confidence="high",
            )

    # ── Pass 2: env-prefix stripped match ───────────────────────
    for contract in valid:
        contract_norm = _normalize_batch_name(contract.batch_name)
        if contract_norm == batch_norm:
            return ResolvedSla(
                sla_hrs=_sla_from_contract(contract),
                sla_model=contract.sla_model,
                source="sla_matrix",
                source_detail=(
                    f"Env-prefix match '{contract.batch_name}' → '{batch_name}' "
                    f"(suffix='{batch_norm}', row {contract.source_row})"
                ),
                schedule_type=contract.schedule_type or schedule_type,
                matched_contract=contract,
                buffer_minutes=contract.buffer_minutes,
                confidence="high",
            )

    # ── Pass 3: substring / contains match ──────────────────────
    # Guard 1: the lookup name must be at least 8 characters long before doing a
    # substring match.  Short generic schedule-type words like "Daily", "Weekly",
    # "Monthly" (≤7 chars) would otherwise false-positive match against any
    # specific contract name containing that word (e.g. "PROD_WEEKLY_WF1_REQPL").
    # Guard 2: when one name is a pure prefix of the other, require a 50% length
    # ratio (relaxed from 60% — GAP-7: "HALEON_CHEC" vs "HALEON_CHECKLIST_DAILY"
    # has ratio 11/22=0.50 which was blocked by the old 0.6 guard).
    _MIN_SUBSTR_LEN = 8
    if len(batch_upper) >= _MIN_SUBSTR_LEN:
        for contract in valid:
            cn = contract.batch_name.upper()
            if not cn:
                continue
            if cn in batch_upper or batch_upper in cn:
                # Apply length-ratio guard when one is a pure prefix of the other
                longer, shorter = (cn, batch_upper) if len(cn) >= len(batch_upper) else (batch_upper, cn)
                if longer.startswith(shorter) and len(shorter) / len(longer) < 0.50:
                    continue   # prefix too short relative to full contract name — skip
                return ResolvedSla(
                    sla_hrs=_sla_from_contract(contract),
                    sla_model=contract.sla_model,
                    source="sla_matrix",
                    source_detail=(
                        f"Substring match '{contract.batch_name}' ↔ '{batch_name}' "
                        f"(row {contract.source_row})"
                    ),
                    schedule_type=contract.schedule_type or schedule_type,
                    matched_contract=contract,
                    buffer_minutes=contract.buffer_minutes,
                    confidence="medium",
                )

    # ── Pass 4: token-overlap match (≥60% shared tokens, schedule-type must agree) ──
    # Handles: WF1_REQPL in CSV vs BYC_WF1_REQPL in SLA file
    best_overlap: Optional[tuple] = None   # (overlap_ratio, contract)
    for contract in valid:
        ct_tokens = set(_normalize_batch_name(contract.batch_name).split("_"))
        if not batch_tokens or not ct_tokens:
            continue
        # Only consider schedule-consistent contracts
        if (contract.schedule_type not in ("UNKNOWN", "")
                and contract.schedule_type != schedule_type
                and schedule_type not in ("UNKNOWN", "")):
            continue
        shared = batch_tokens & ct_tokens
        # GAP-7: Require ≥2 shared tokens OR exactly 1 shared token that is a
        # long, dominant anchor (≥6 chars AND represents ≥40% of the shorter
        # token set's size).
        # Rationale: "HALEON_CHEC" vs "HALEON_CHECKLIST_DAILY" shares {"HALEON"}
        # (1 token, 6 chars, 100% of the 1-token set) — that IS a valid anchor.
        # Without this relaxation, Pass 3 substring guard blocks it (len ratio 0.5
        # < 0.60) and it falls through to the 6h default.
        _long_anchor = (
            len(shared) == 1
            and len(next(iter(shared))) >= 6
            and len(shared) / max(len(batch_tokens), 1) >= 0.40
        )
        if len(shared) < 2 and not _long_anchor:
            continue
        ratio = len(shared) / max(len(batch_tokens), len(ct_tokens))
        if ratio >= 0.60:
            if best_overlap is None or ratio > best_overlap[0]:
                best_overlap = (ratio, contract)

    if best_overlap is not None:
        _, contract = best_overlap
        return ResolvedSla(
            sla_hrs=_sla_from_contract(contract),
            sla_model=contract.sla_model,
            source="sla_matrix",
            source_detail=(
                f"Token-overlap match ({best_overlap[0]:.0%}) "
                f"'{contract.batch_name}' ↔ '{batch_name}' "
                f"(row {contract.source_row})"
            ),
            schedule_type=contract.schedule_type or schedule_type,
            matched_contract=contract,
            buffer_minutes=contract.buffer_minutes,
            confidence="medium",
        )

    # ── Pass 5: schedule-type ceiling from customer SLA file ────
    if schedule_type in ceilings:
        sla_hrs = ceilings[schedule_type]
        return ResolvedSla(
            sla_hrs=sla_hrs,
            sla_model="WINDOW",
            source="customer_fallback",
            source_detail=(
                f"No batch-name match — using {schedule_type} ceiling "
                f"{sla_hrs:.2f}h from customer SLA file"
            ),
            schedule_type=schedule_type,
            confidence="medium",
        )

    # ── Pass 6: system default (assumed / blocked) ───────────────
    defaults = {
        "DAILY":   pe_config.SLA_DAILY_HRS,
        "WEEKLY":  pe_config.SLA_WEEKLY_HRS,
        "MONTHLY": pe_config.SLA_MONTHLY_HRS,
    }
    if schedule_type in defaults:
        assumed = defaults[schedule_type]
        return ResolvedSla(
            sla_hrs=assumed,
            sla_model="ASSUMED",
            source="assumed",
            source_detail=(
                f"No SLA matrix match — assumed {schedule_type} default {assumed:.1f}h"
            ),
            schedule_type=schedule_type,
            confidence="low",
            blocked=True,
            block_reason=(
                f"Cannot confirm compliance — SLA is assumed ({assumed:.1f}h), "
                "not sourced from a customer SLA matrix."
            ),
        )

    # ── Pass 7: absolute fallback ────────────────────────────────
    fallback_hrs = pe_config.SLA_DAILY_HRS
    return ResolvedSla(
        sla_hrs=fallback_hrs,
        sla_model="ASSUMED",
        source="assumed",
        source_detail=f"Unknown schedule + no SLA matrix — assumed daily default {fallback_hrs:.1f}h",
        schedule_type="UNKNOWN",
        confidence="low",
        blocked=True,
        block_reason=f"Cannot resolve SLA — schedule unrecognised, using daily default {fallback_hrs:.1f}h",
    )


# ── Actual vs SLA comparison ─────────────────────────────────────────────────

def compare_actual(
    resolved: ResolvedSla,
    actual_hrs: float,
    actual_end_time: Optional[dtime] = None,   # Gap 2: clock-time of actual batch end
) -> SlaVerdict:
    """Compare actual execution against resolved SLA.

    Produces both strict (no buffer) and buffered verdicts.

    When ``actual_end_time`` is provided AND the matched SLA contract has an
    absolute ``sla_end`` clock time, the comparison is done on absolute clock
    times in ADDITION to the duration comparison.  A batch that completes
    within its elapsed-duration SLA but AFTER the absolute SLA deadline is
    flagged as BREACH (the more conservative result is used).
    """
    sla_hrs = resolved.sla_hrs
    margin = sla_hrs - actual_hrs

    # ── Gap 2: Absolute-time check when clock-time SLA deadline is known ────
    absolute_breach = False
    if (actual_end_time is not None
            and resolved.matched_contract is not None
            and resolved.matched_contract.sla_end is not None):
        sla_deadline = resolved.matched_contract.sla_end
        base = date(2000, 1, 1)
        # Compute "actual end" relative to "sla_end" on the same calendar base.
        # Overnight batches may cross midnight; use a +1 day offset if needed.
        dt_actual = datetime.combine(base, actual_end_time)
        dt_sla    = datetime.combine(base, sla_deadline)
        if dt_actual > dt_sla + timedelta(hours=12):   # actual is >12h past deadline → midnight wrap
            dt_actual -= timedelta(hours=24)
        elif dt_sla > dt_actual + timedelta(hours=12): # deadline is >12h past actual → next-day deadline
            dt_sla += timedelta(hours=24)
        if dt_actual > dt_sla:
            absolute_breach = True
            abs_overrun_hrs = (dt_actual - dt_sla).total_seconds() / 3600.0
        else:
            abs_overrun_hrs = 0.0
    else:
        abs_overrun_hrs = 0.0

    # Strict status (no buffer) — use the MORE CONSERVATIVE of duration vs absolute-time
    if actual_hrs <= sla_hrs and not absolute_breach:
        strict = "PASS"
    else:
        strict = "BREACH"

    # Buffered status
    buffer_margin_hrs = None
    if resolved.buffer_minutes and resolved.buffer_minutes > 0:
        buffered_sla = sla_hrs + (resolved.buffer_minutes / 60)
        buffer_margin_hrs = buffered_sla - actual_hrs
        buffered = "PASS" if (actual_hrs <= buffered_sla and not absolute_breach) else "BREACH"
    else:
        buffered = strict

    # At-risk detection (within 15% of SLA)
    at_risk_floor = sla_hrs * 0.85
    if strict == "PASS" and actual_hrs >= at_risk_floor:
        at_risk = True
    else:
        at_risk = False

    # Final composite status
    if strict == "PASS" and not at_risk:
        final = "PASS"
    elif strict == "PASS" and at_risk:
        final = "AT_RISK"
    elif strict == "BREACH" and buffered == "PASS":
        final = "PASS_WITH_BUFFER"
    else:
        final = "BREACH"

    # Build explanation
    parts = [f"Actual: {actual_hrs:.2f}h vs SLA: {sla_hrs:.2f}h"]
    if absolute_breach:
        parts.append(f"Absolute deadline BREACHED by {abs_overrun_hrs:.2f}h (batch ended after {resolved.matched_contract.sla_end.isoformat() if resolved.matched_contract and resolved.matched_contract.sla_end else 'SLA end'})")
    elif margin >= 0:
        parts.append(f"Headroom: {margin:.2f}h")
    else:
        parts.append(f"Overrun: {abs(margin):.2f}h")
    if resolved.buffer_minutes:
        parts.append(f"Buffer: {resolved.buffer_minutes:.0f}min → buffered status: {buffered}")
    parts.append(f"Source: {resolved.source_detail}")

    return SlaVerdict(
        status=final,
        strict_status=strict,
        buffered_status=buffered,
        actual_hrs=actual_hrs,
        sla_hrs=sla_hrs,
        margin_hrs=margin,
        buffer_applied=resolved.buffer_minutes is not None and resolved.buffer_minutes > 0,
        buffer_margin_hrs=buffer_margin_hrs,
        explanation=" · ".join(parts),
    )


# ── Batch-level SLA resolution for all jobs ──────────────────────────────────

def resolve_batch_sla_map(
    job_names: List[str],
    sla_result: Optional[SlaIngestResult],
) -> Dict[str, ResolvedSla]:
    """Resolve SLA for every job name. Returns job_name → ResolvedSla."""
    contracts = sla_result.contracts if sla_result else []
    ceilings = sla_result.ceilings if sla_result else {}
    return {
        job: resolve_sla(job, "", contracts, ceilings)
        for job in job_names
    }


def build_sla_traceability(sla_result: Optional[SlaIngestResult]) -> Dict[str, Any]:
    """Build the SLA source traceability payload for the frontend.

    Returns a dict suitable for inclusion in the batch payload.
    """
    if not sla_result:
        from services import pe_config
        return {
            "type": "assumed",
            "label": "Assumed (no SLA file uploaded)",
            "model": "ASSUMED",
            "daily_hrs": pe_config.SLA_DAILY_HRS,
            "weekly_hrs": pe_config.SLA_WEEKLY_HRS,
            "monthly_hrs": pe_config.SLA_MONTHLY_HRS,
            "custom_hrs": pe_config.SLA_CUSTOM_HRS,
            "contracts": [],
            "warnings": [{
                "code": "NO_SLA_FILE",
                "text": "No SLA matrix uploaded — all compliance uses assumed system defaults. "
                        "Upload a customer SLA file for audit-defensible results.",
                "severity": "critical",
            }],
            "schema_type": "none",
            "valid_rows": 0,
            "blocked": True,
            "block_reason": "Compliance is based on assumed defaults — cannot be marked green for audit sign-off",
        }

    contracts_brief = []
    for c in (sla_result.contracts or [])[:20]:
        contracts_brief.append({
            "batch": c.batch_name,
            "schedule": c.schedule_type,
            "schedule_raw": c.schedule_raw,
            "model": c.sla_model,
            "window_hrs": c.sla_window_hrs,
            "duration_hrs": c.sla_duration_hrs,
            "buffer_min": c.buffer_minutes,
            "completeness": c.completeness,
            "row": c.source_row,
            "comments": c.comments[:100] if c.comments else "",
            # Two-tier: which kind of SLA is this contract?
            "sla_source_type": c.sla_source_type,
        })

    return {
        "type": "sla_matrix",
        "label": f"From SLA Matrix ({sla_result.filename})",
        "model": sla_result.schema_type.upper(),
        "daily_hrs": sla_result.ceilings.get("DAILY"),
        "weekly_hrs": sla_result.ceilings.get("WEEKLY"),
        "monthly_hrs": sla_result.ceilings.get("MONTHLY"),
        "custom_hrs": sla_result.ceilings.get("CUSTOM"),
        "contracts": contracts_brief,
        "warnings": sla_result.warnings,
        "schema_type": sla_result.schema_type,
        "detected_model": sla_result.detected_model,
        "valid_rows": sla_result.valid_rows,
        "partial_rows": sla_result.partial_rows,
        "total_rows": sla_result.total_rows,
        "sections_detected": sla_result.sections_detected,
        "blocked": sla_result.valid_rows == 0,
        "block_reason": (
            "SLA file parsed but no valid numeric SLA rules found"
            if sla_result.valid_rows == 0 else ""
        ),
        # Two-tier: is this file a job-level matrix or a SOW schedule?
        "contract_type": sla_result.contract_type,
    }
