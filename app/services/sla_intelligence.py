"""
sla_intelligence — generic, customer-agnostic SLA file analyzer.

Implements the 9-phase pipeline:
    1. File-type detection (TYPE_A / TYPE_B / TYPE_C / TYPE_UNKNOWN)
    2. Schema mapping (fuzzy column → canonical fields)
    3. Timestamp & duration normalization (multi-format, TZ, overnight)
    4. Dynamic SLA buffer calculation (SAFE / RISK / BREACH / UNDEFINED)
    5. Trend & linearity engine (avg/min/max/std, trending, spikes,
       day-of-week, volume correlation)
    6. Type-A ⊕ Type-B join on batch_name ↔ process_name
    7. Comments baseline parser (drift vs. historical) + dependency chain
    8. Schedule overlap detector
    9. Type-C pre-computed summary enrichment + status-mismatch validation

Public API:
    analyze_files(files: list[(filename, raw_bytes)]) -> dict
        Returns the structured JSON described in PHASE 9.

Self-contained: re-uses helpers from sla_engine where useful but does NOT
require any session state. Pure function — same input → same output.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pe_dashboard.sla_intelligence")

# ─────────────────────────────────────────────────────────────────────
# PHASE 2 helpers — column aliases & normalization (extends sla_engine)
# ─────────────────────────────────────────────────────────────────────

# Canonical field → list of fuzzy regex patterns (case-insensitive)
_CANONICAL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("batch_name",   re.compile(r"^(batch[\s_-]?name|batch[\s_-]?type|batch|process(?:[\s_-]?name)?|job[\s_-]?name|window[\s_-]?name|module[\s_-]?name|name)$", re.I)),
    ("schedule",     re.compile(r"^(schedule(?:[\s_-]?type)?|frequency|day|run[\s_-]?date|runs[\s_-]?on)$", re.I)),
    ("start_time",   re.compile(r"^(start[\s_-]?time(?:.*)?|start[\s_-]?date|begin[\s_-]?time|actual[\s_-]?start|run[\s_-]?start|window[\s_-]?start)$", re.I)),
    ("end_time",     re.compile(r"^(end[\s_-]?time(?:.*)?|end[\s_-]?date|current[\s_-]?end[\s_-]?time|actual[\s_-]?end|run[\s_-]?end|finish[\s_-]?time)$", re.I)),
    ("sla_deadline", re.compile(r"^(expected[\s_-]?end[\s_-]?time(?:[\s_/-]?sla)?|sla[\s_-]?deadline|sla[\s_-]?end|sla|target[\s_-]?end|deadline)$", re.I)),
    ("duration",     re.compile(r"^(duration(?:.*)?|total[\s_-]?runtime[\s_-]?sec(?:onds)?|elapsed(?:.*)?|runtime|run[\s_-]?time|elapsed[\s_-]?mins|duration[\s_-]?mins)$", re.I)),
    ("status",       re.compile(r"^(status|breach[\s_-]?type|sla[\s_-]?status|result|outcome|pass[\s_-]?fail)$", re.I)),
    ("comments",     re.compile(r"^(comment(?:s)?|status[\s_-]?message|note(?:s)?|remark(?:s)?|description)$", re.I)),
    ("timezone",     re.compile(r"^(time[\s_-]?zone|tz)$", re.I)),
    ("module",       re.compile(r"^(module|window|batch[\s_-]?group|category|sub[\s_-]?application)$", re.I)),
    ("error_count",  re.compile(r"^(error[\s_-]?count|errors|fail[\s_-]?count)$", re.I)),
    ("long_running", re.compile(r"^(long[\s_-]?running|is[\s_-]?long|long[\s_-]?run[\s_-]?flag)$", re.I)),
    ("volume",       re.compile(r"^(process[\s_-]?volume|volume[\s_-]?target|volume|row[\s_-]?count|records[\s_-]?processed)$", re.I)),
    ("first_job",    re.compile(r"^(first[\s_-]?job(?:[\s_-]?name)?)$", re.I)),
    ("last_job",     re.compile(r"^(last[\s_-]?job(?:[\s_-]?name)?)$", re.I)),
    ("over_by",      re.compile(r"^(over[\s_-]?by(?:[\s_-]?sec)?|over[\s_-]?run|breach[\s_-]?margin)$", re.I)),
    ("buffer_short", re.compile(r"^(buffer[\s_-]?short(?:[\s_-]?by)?|short[\s_-]?by)$", re.I)),
]


def map_columns(raw_headers: List[str]) -> Dict[str, str]:
    """Return {raw_header: canonical_field} for all matched columns."""
    mapping: Dict[str, str] = {}
    for raw in raw_headers:
        clean = re.sub(r"\s+", " ", str(raw).strip())
        # Strip trailing TZ qualifier "(CST)", "(EDT)" etc. for matching
        no_tz = re.sub(r"\s*\([A-Z]{2,5}\)\s*$", "", clean).strip()
        for canonical, pat in _CANONICAL_PATTERNS:
            if pat.match(no_tz):
                mapping[raw] = canonical
                break
    return mapping


# ─────────────────────────────────────────────────────────────────────
# PHASE 1 — File-type detection
# ─────────────────────────────────────────────────────────────────────

# Signal-strength based classifier
_TYPE_A_SIGNALS = {"sla_deadline", "first_job", "last_job", "schedule"}
_TYPE_B_SIGNALS = {"start_time", "end_time", "status", "error_count",
                   "long_running", "volume"}
_TYPE_C_SIGNALS = {"over_by", "buffer_short"}


def detect_file_type(canonical_fields: set, raw_headers: List[str]) -> str:
    """Classify a sheet as TYPE_A / TYPE_B / TYPE_C / TYPE_UNKNOWN.

    Heuristic: count the canonical-field signals that match each profile.
    Whichever profile gets the strongest signal wins; if no signal is
    above 1, return TYPE_UNKNOWN.
    """
    a_score = len(canonical_fields & _TYPE_A_SIGNALS)
    b_score = len(canonical_fields & _TYPE_B_SIGNALS)
    c_score = len(canonical_fields & _TYPE_C_SIGNALS)

    # Type-C is highly specific — even one match beats most ambiguities
    if c_score >= 1 and "status" in canonical_fields:
        return "TYPE_C"

    # Type-A needs sla_deadline OR (first_job AND last_job)
    if "sla_deadline" in canonical_fields and a_score >= 1:
        return "TYPE_A"
    if {"first_job", "last_job"}.issubset(canonical_fields):
        return "TYPE_A"

    # Type-B: requires precise timestamps + status/volume
    if "start_time" in canonical_fields and "end_time" in canonical_fields:
        if b_score >= 2:
            return "TYPE_B"
        # Even without status, if timestamps look precise (Oracle format)
        # we still call it Type B
        return "TYPE_B"

    if a_score + b_score + c_score >= 2:
        # Mixed signal — fall through to the strongest
        winner = max([("TYPE_A", a_score), ("TYPE_B", b_score), ("TYPE_C", c_score)],
                     key=lambda x: x[1])
        if winner[1] > 0:
            return winner[0]

    return "TYPE_UNKNOWN"


# ─────────────────────────────────────────────────────────────────────
# PHASE 2 — Timestamp / duration / TZ normalization
# ─────────────────────────────────────────────────────────────────────

# Oracle: "06-NOV-24 11.00.05.100271000 PM GMT"
_ORACLE_TS_RE = re.compile(
    r"(\d{1,2})[-\s]([A-Z]{3})[-\s](\d{2,4})\s+"
    r"(\d{1,2})\.(\d{2})\.(\d{2})(?:\.\d+)?\s*"
    r"(AM|PM)?\s*([A-Z]{2,5})?",
    re.I,
)

# Generic: "11.18 AM", "5AM CST", "21:00:00", "9:00 PM"
_TIME_TOKEN_RE = re.compile(
    r"(\d{1,2})[:.](\d{1,2})(?:[:.](\d{1,2}))?\s*(AM|PM)?\s*([A-Z]{2,5})?",
    re.I,
)
_HOUR_ONLY_RE = re.compile(r"(\d{1,2})\s*(AM|PM)\s*([A-Z]{2,5})?", re.I)

_MONTH_MAP = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

# Hour offsets relative to UTC (rough — for breach math, not legal).
_TZ_OFFSETS = {
    "UTC": 0, "GMT": 0, "Z": 0,
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "IST": 5.5, "BST": 1, "CET": 1, "CEST": 2,
    "JST": 9, "AEST": 10, "AEDT": 11,
}


@dataclass
class NormalizedTime:
    """A fully normalized time-point."""
    raw: str
    date: Optional[date] = None
    time: Optional[dtime] = None
    tz: str = ""
    utc_seconds: Optional[int] = None  # seconds-of-day in UTC reference

    @property
    def is_valid(self) -> bool:
        return self.time is not None


def normalize_time(raw: Any, default_tz: str = "") -> NormalizedTime:
    """Parse an arbitrary time string into a NormalizedTime."""
    nt = NormalizedTime(raw=str(raw) if raw is not None else "")
    if raw is None:
        return nt
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "nat", "none", ""):
        return nt

    # Try Oracle full timestamp first
    m = _ORACLE_TS_RE.search(s)
    if m:
        try:
            day = int(m.group(1))
            mon = _MONTH_MAP.get(m.group(2).upper())
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            hh = int(m.group(4))
            mm = int(m.group(5))
            ss = int(m.group(6))
            ampm = (m.group(7) or "").upper()
            tz = (m.group(8) or default_tz or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            elif ampm == "AM" and hh == 12:
                hh = 0
            if mon and 1 <= day <= 31 and 0 <= hh <= 23:
                nt.date = date(yr, mon, day)
                nt.time = dtime(hh, mm, ss)
                nt.tz = tz
                nt.utc_seconds = _to_utc_seconds(nt.time, tz)
                return nt
        except Exception:
            pass

    # Try ISO-ish "YYYY-MM-DD HH:MM:SS"
    try:
        if " " in s and "-" in s.split(" ", 1)[0]:
            dt = datetime.fromisoformat(s.split(".")[0].replace("/", "-"))
            nt.date = dt.date()
            nt.time = dt.time()
            nt.tz = default_tz.upper()
            nt.utc_seconds = _to_utc_seconds(nt.time, nt.tz)
            return nt
    except Exception:
        pass

    # Try "HH:MM:SS" / "HH.MM AM" with optional TZ suffix
    m = _TIME_TOKEN_RE.search(s)
    if m:
        try:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ss = int(m.group(3) or 0)
            ampm = (m.group(4) or "").upper()
            tz = (m.group(5) or default_tz or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            elif ampm == "AM" and hh == 12:
                hh = 0
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                nt.time = dtime(hh, mm, ss)
                nt.tz = tz
                nt.utc_seconds = _to_utc_seconds(nt.time, tz)
                return nt
        except Exception:
            pass

    # "5AM CST"
    m = _HOUR_ONLY_RE.search(s)
    if m:
        try:
            hh = int(m.group(1))
            ampm = m.group(2).upper()
            tz = (m.group(3) or default_tz or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            elif ampm == "AM" and hh == 12:
                hh = 0
            if 0 <= hh <= 23:
                nt.time = dtime(hh, 0, 0)
                nt.tz = tz
                nt.utc_seconds = _to_utc_seconds(nt.time, tz)
                return nt
        except Exception:
            pass

    return nt


def _to_utc_seconds(t: Optional[dtime], tz: str) -> Optional[int]:
    """Convert a time-of-day to seconds-since-midnight in UTC (mod 86400)."""
    if t is None:
        return None
    secs = t.hour * 3600 + t.minute * 60 + t.second
    off_hrs = _TZ_OFFSETS.get((tz or "").upper())
    if off_hrs is None:
        return secs  # treat as already UTC if unknown
    secs -= int(off_hrs * 3600)
    secs %= 86400
    return secs


def compute_duration_secs(
    start: NormalizedTime,
    end: NormalizedTime,
) -> Optional[int]:
    """Duration in seconds between two NormalizedTimes. Handles overnight."""
    # Prefer date+time math if both have a date
    if start.date and end.date and start.time and end.time:
        dt_s = datetime.combine(start.date, start.time)
        dt_e = datetime.combine(end.date, end.time)
        delta = (dt_e - dt_s).total_seconds()
        if delta < 0:
            return None  # bad data
        return int(delta)

    # Fall back to UTC-seconds math (overnight wrap)
    if start.utc_seconds is None or end.utc_seconds is None:
        return None
    diff = end.utc_seconds - start.utc_seconds
    if diff < 0:
        diff += 86400  # overnight crossover
    return diff


def humanize_duration(secs: Optional[int]) -> str:
    """'7530' → '2 hrs 5 mins 30 secs'."""
    if secs is None or secs < 0:
        return "Missing"
    hrs = secs // 3600
    mins = (secs % 3600) // 60
    rem = secs % 60
    parts = []
    if hrs:
        parts.append(f"{hrs} hr{'s' if hrs != 1 else ''}")
    if mins:
        parts.append(f"{mins} min{'s' if mins != 1 else ''}")
    if rem or not parts:
        parts.append(f"{rem} sec{'s' if rem != 1 else ''}")
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────
# PHASE 3 — SLA buffer calculation
# ─────────────────────────────────────────────────────────────────────

def classify_buffer(
    sla_deadline_secs: Optional[int],
    actual_end_secs: Optional[int],
    window_secs: Optional[int],
) -> Dict[str, Any]:
    """Return {status, label, buffer_secs}."""
    if sla_deadline_secs is None or actual_end_secs is None:
        return {
            "status": "UNDEFINED",
            "label": "No SLA defined — cannot assess",
            "buffer_secs": None,
        }
    buf = sla_deadline_secs - actual_end_secs
    risk_floor = -int((window_secs or 0) * 0.10) if window_secs else -600
    if buf > 0:
        return {
            "status": "SAFE",
            "label": f"Buffer remaining: {humanize_duration(buf)}",
            "buffer_secs": buf,
        }
    if buf >= risk_floor:
        return {
            "status": "RISK",
            "label": f"Buffer nearly consumed — only {humanize_duration(abs(buf))} headroom",
            "buffer_secs": buf,
        }
    return {
        "status": "BREACH",
        "label": f"SLA exceeded by {humanize_duration(abs(buf))}",
        "buffer_secs": buf,
    }


# ─────────────────────────────────────────────────────────────────────
# PHASE 4 — Trend & linearity engine
# ─────────────────────────────────────────────────────────────────────

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def analyze_trends(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Per-batch trend analysis. Requires ≥3 dated rows.

    `runs` items must contain: run_date (date or YYYY-MM-DD), duration_secs,
    optionally volume.
    """
    if not runs or len(runs) < 3:
        return None
    valid = [r for r in runs if r.get("duration_secs") and r["duration_secs"] > 0]
    if len(valid) < 3:
        return None

    # Parse dates for ordering
    parsed: List[Tuple[date, int, Optional[float]]] = []
    for r in valid:
        d = r.get("run_date")
        if isinstance(d, str):
            try:
                d = datetime.fromisoformat(d).date()
            except Exception:
                continue
        if not isinstance(d, date):
            continue
        parsed.append((d, int(r["duration_secs"]), r.get("volume")))
    if len(parsed) < 3:
        return None
    parsed.sort(key=lambda x: x[0])

    durs = [p[1] for p in parsed]
    n = len(durs)
    avg = sum(durs) / n
    mn = min(durs)
    mx = max(durs)
    var = sum((d - avg) ** 2 for d in durs) / n
    std = var ** 0.5

    # 4B — Trend (latest 3 vs. prior rolling avg)
    latest_3 = durs[-3:]
    prior = durs[:-3] if n > 3 else durs[:-1]
    prior_avg = sum(prior) / len(prior) if prior else avg
    latest_3_avg = sum(latest_3) / 3
    trend = "STABLE"
    trend_label = "Runtime stable"
    if prior_avg > 0:
        ratio = latest_3_avg / prior_avg
        if ratio > 1.15:
            pct = (ratio - 1) * 100
            trend = "TRENDING_UP"
            trend_label = f"Runtime growing — latest 3 avg is {pct:.0f}% above historical avg"
        elif ratio < 0.85:
            pct = (1 - ratio) * 100
            trend = "TRENDING_DOWN"
            trend_label = f"Runtime improving — latest 3 avg is {pct:.0f}% below historical avg"

    # 4C — Spike
    spike = None
    for d, secs, _ in parsed:
        if avg > 0 and secs > 2 * avg:
            spike = {
                "date": d.isoformat(),
                "magnitude": f"{secs / avg:.1f}× the average",
                "label": f"SPIKE DETECTED on {d.isoformat()} — investigate root cause",
            }
            break

    # 4D — Day-of-week pattern
    dow_groups: Dict[int, List[int]] = {}
    for d, secs, _ in parsed:
        dow_groups.setdefault(d.weekday(), []).append(secs)
    dow_pattern: Optional[str] = None
    for dow, secs_list in dow_groups.items():
        if len(secs_list) < 2:
            continue
        dow_avg = sum(secs_list) / len(secs_list)
        if avg > 0 and dow_avg > avg * 1.20:
            pct = (dow_avg / avg - 1) * 100
            dow_pattern = f"{_DAY_NAMES[dow]} consistently runs {pct:.0f}% longer than average"
            break
    weekend_avg = sum(durs[i] for i, p in enumerate(parsed) if p[0].weekday() >= 5) or 0
    weekend_count = sum(1 for p in parsed if p[0].weekday() >= 5)
    if weekend_count >= 2 and avg > 0:
        we_avg = weekend_avg / weekend_count
        if we_avg > avg * 1.20 and not dow_pattern:
            dow_pattern = f"WEEKEND HEAVY — weekend avg {humanize_duration(int(we_avg))} vs overall {humanize_duration(int(avg))}"

    # 4E — Volume correlation
    vol_corr: Optional[Dict[str, Any]] = None
    vols = [p[2] for p in parsed if p[2] is not None]
    if len(vols) == n and n >= 4:
        try:
            v_avg = sum(vols) / n
            num = sum((durs[i] - avg) * (vols[i] - v_avg) for i in range(n))
            den_d = sum((durs[i] - avg) ** 2 for i in range(n)) ** 0.5
            den_v = sum((vols[i] - v_avg) ** 2 for i in range(n)) ** 0.5
            if den_d > 0 and den_v > 0:
                pearson = num / (den_d * den_v)
                if pearson > 0.7:
                    vol_corr = {"pearson": round(pearson, 3),
                                "label": "Runtime scales with data volume"}
                elif pearson < 0.3:
                    vol_corr = {"pearson": round(pearson, 3),
                                "label": "Runtime independent of volume — possible lock/wait or external dependency"}
                else:
                    vol_corr = {"pearson": round(pearson, 3), "label": "Moderate volume correlation"}
        except Exception:
            pass

    return {
        "runs": n,
        "avg_secs": int(avg),
        "min_secs": mn,
        "max_secs": mx,
        "std_secs": int(std),
        "avg_human": humanize_duration(int(avg)),
        "min_human": humanize_duration(mn),
        "max_human": humanize_duration(mx),
        "trend": trend,
        "trend_label": trend_label,
        "spike": spike,
        "day_pattern": dow_pattern,
        "volume_correlation": vol_corr,
    }


# ─────────────────────────────────────────────────────────────────────
# PHASE 6 — Comments baseline parser
# ─────────────────────────────────────────────────────────────────────

# "2019 Run Time : 38 mins", "baseline: 2 hrs 30 mins", "expected 1.5 hours"
_BASELINE_RE = re.compile(
    r"(?P<label>(?:\b\d{4}\b|baseline|expected|previously|target|earlier|prior))"
    r"[\s:.-]+"
    r"(?:run\s*time\s*[:\-]?\s*)?"
    r"(?P<hrs>\d+(?:\.\d+)?)?\s*(?:hrs?|hours?)?\s*"
    r"(?P<mins>\d+(?:\.\d+)?)?\s*(?:mins?|minutes?)",
    re.I,
)
_BASELINE_RE_SIMPLE = re.compile(
    r"(?P<label>\b\d{4}\b|baseline|expected|previously|target)[\s:.-]+"
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>hrs?|hours?|mins?|minutes?|secs?|seconds?)",
    re.I,
)
_DEPENDENCY_RE = re.compile(
    r"(?:integrated\s+with|depends\s+on|after|triggered\s+by|preceded\s+by)\s+"
    r"([A-Za-z0-9_\-\s]+?)(?:\.|,|;|$)",
    re.I,
)


def parse_baseline(comment: str) -> Optional[Dict[str, Any]]:
    """Extract a baseline duration (in seconds) from a free-text comment."""
    if not comment:
        return None
    text = str(comment)

    # Try compound "X hrs Y mins"
    for m in _BASELINE_RE.finditer(text):
        hrs = m.group("hrs")
        mins = m.group("mins")
        if not hrs and not mins:
            continue
        secs = 0
        if hrs:
            secs += int(float(hrs) * 3600)
        if mins:
            secs += int(float(mins) * 60)
        if secs > 0:
            return {
                "label": m.group("label"),
                "secs": secs,
                "human": humanize_duration(secs),
            }

    # Try simple "X unit"
    m = _BASELINE_RE_SIMPLE.search(text)
    if m:
        num = float(m.group("num"))
        unit = m.group("unit").lower()
        secs = 0
        if unit.startswith("h"):
            secs = int(num * 3600)
        elif unit.startswith("m"):
            secs = int(num * 60)
        elif unit.startswith("s"):
            secs = int(num)
        if secs > 0:
            return {
                "label": m.group("label"),
                "secs": secs,
                "human": humanize_duration(secs),
            }

    return None


def classify_drift(baseline_secs: int, current_secs: int) -> Dict[str, Any]:
    """Return {drift_pct, label}."""
    if baseline_secs <= 0:
        return {"drift_pct": 0.0, "label": "Baseline invalid"}
    drift = ((current_secs - baseline_secs) / baseline_secs) * 100
    if drift > 50:
        label = f"CRITICAL — {drift:.0f}% slower, urgent investigation needed"
    elif drift > 20:
        label = f"GROWING — {drift:.0f}% slower than baseline"
    elif drift < -5:
        label = f"IMPROVED — {abs(drift):.0f}% faster than baseline"
    else:
        label = "STABLE — within acceptable range of baseline"
    return {"drift_pct": round(drift, 1), "label": label}


def extract_dependencies(comment: str) -> List[str]:
    """Return a list of upstream batch/process names mentioned in the comment."""
    if not comment:
        return []
    deps = []
    for m in _DEPENDENCY_RE.finditer(str(comment)):
        name = m.group(1).strip()
        if name and len(name) <= 60:
            deps.append(name)
    return deps


# ─────────────────────────────────────────────────────────────────────
# PHASE 7 — Schedule overlap detector
# ─────────────────────────────────────────────────────────────────────

def detect_overlaps(contracts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect time overlaps among contracts that share day & module.

    Each contract dict needs: batch_name, schedule (str), module, start_secs,
    end_secs (actual_end seconds-of-day, UTC), sla_secs (deadline secs).
    """
    out: List[Dict[str, Any]] = []
    if not contracts:
        return out

    # Group by (module, schedule_signature)
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for c in contracts:
        if c.get("start_secs") is None:
            continue
        key = (str(c.get("module", "")).lower(), str(c.get("schedule", "")).lower())
        groups.setdefault(key, []).append(c)

    for key, items in groups.items():
        items.sort(key=lambda c: c.get("start_secs", 0))
        for i in range(len(items) - 1):
            a = items[i]
            b = items[i + 1]
            a_end = a.get("end_secs")
            a_sla = a.get("sla_secs")
            b_start = b.get("start_secs")
            if b_start is None:
                continue
            if a_end is not None and a_end > b_start:
                out.append({
                    "batch_a": a["batch_name"],
                    "batch_b": b["batch_name"],
                    "overlap_secs": a_end - b_start,
                    "shared_days": key[1] or "—",
                    "module": key[0] or "—",
                    "risk": "ACTUAL_OVERLAP",
                    "label": (f"{a['batch_name']} ends at {humanize_duration(a_end)} "
                              f"but {b['batch_name']} starts at {humanize_duration(b_start)} — "
                              f"overlap of {humanize_duration(a_end - b_start)}. "
                              "Risk of resource contention."),
                })
            elif a_sla is not None and a_sla > b_start:
                out.append({
                    "batch_a": a["batch_name"],
                    "batch_b": b["batch_name"],
                    "overlap_secs": a_sla - b_start,
                    "shared_days": key[1] or "—",
                    "module": key[0] or "—",
                    "risk": "POTENTIAL_OVERLAP",
                    "label": (f"If {a['batch_name']} breaches SLA it will collide with "
                              f"{b['batch_name']} (deadline overlaps start by "
                              f"{humanize_duration(a_sla - b_start)})"),
                })
    return out


# ─────────────────────────────────────────────────────────────────────
# Sheet ingestion (raw bytes → sheet records)
# ─────────────────────────────────────────────────────────────────────

@dataclass
class Sheet:
    file: str
    name: str
    df: Any                              # pandas DataFrame
    header_map: Dict[str, str] = field(default_factory=dict)
    canonical_fields: set = field(default_factory=set)
    file_type: str = "TYPE_UNKNOWN"


def _read_sheets(filename: str, raw: bytes) -> List[Tuple[str, Any]]:
    import pandas as pd
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    out: List[Tuple[str, Any]] = []
    try:
        if ext in ("xlsx", "xls"):
            xl = pd.ExcelFile(io.BytesIO(raw), engine="openpyxl")
            for sh in xl.sheet_names:
                try:
                    out.append((sh, xl.parse(sh)))
                except Exception as exc:
                    logger.debug("sheet %s parse failed: %s", sh, exc)
        elif ext == "csv":
            out.append(("csv", pd.read_csv(io.BytesIO(raw))))
    except Exception as exc:
        logger.warning("read_sheets %s failed: %s", filename, exc)
    return out


def _ingest_sheet(filename: str, sheet_name: str, df) -> Sheet:
    df.columns = [str(c).strip() for c in df.columns]
    header_map = map_columns(list(df.columns))
    canonical_fields = set(header_map.values())
    file_type = detect_file_type(canonical_fields, list(df.columns))
    return Sheet(file=filename, name=sheet_name, df=df,
                 header_map=header_map,
                 canonical_fields=canonical_fields,
                 file_type=file_type)


# ─────────────────────────────────────────────────────────────────────
# Per-row extraction helpers
# ─────────────────────────────────────────────────────────────────────

def _row_get(row, header_map: Dict[str, str], canonical: str) -> Any:
    """Return the first non-null value for the canonical field in this row."""
    for raw_col, mapped in header_map.items():
        if mapped == canonical:
            try:
                v = row.get(raw_col)
            except Exception:
                continue
            if v is None:
                continue
            try:
                import pandas as pd
                if pd.isna(v):
                    continue
            except Exception:
                pass
            s = str(v).strip()
            if s and s.lower() not in ("nan", "nat", "none"):
                return v
    return None


def _extract_rows(sheet: Sheet) -> List[Dict[str, Any]]:
    """Walk every row of a sheet → list of canonical-field dicts."""
    out: List[Dict[str, Any]] = []
    hm = sheet.header_map
    if not hm:
        return out
    for idx, row in sheet.df.iterrows():
        rec: Dict[str, Any] = {"_row": int(idx) + 2,
                               "_sheet": sheet.name,
                               "_file": sheet.file,
                               "_file_type": sheet.file_type}
        for canonical in sheet.canonical_fields:
            rec[canonical] = _row_get(row, hm, canonical)
        # Skip totally empty rows
        if not any(rec.get(k) for k in sheet.canonical_fields):
            continue
        out.append(rec)
    return out


def _date_range(rows: List[Dict[str, Any]]) -> Tuple[Optional[date], Optional[date]]:
    dates: List[date] = []
    for r in rows:
        for key in ("start_time", "end_time", "schedule"):
            v = r.get(key)
            if not v:
                continue
            nt = normalize_time(v)
            if nt.date:
                dates.append(nt.date)
                break
    if not dates:
        return None, None
    return min(dates), max(dates)


# ─────────────────────────────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────────────────────────────

def analyze_files(files: List[Tuple[str, bytes]]) -> Dict[str, Any]:
    """Run the full 9-phase pipeline on a list of (filename, bytes) tuples."""
    file_inventory: List[Dict[str, Any]] = []
    sheets: List[Sheet] = []
    all_rows_by_type: Dict[str, List[Dict[str, Any]]] = {
        "TYPE_A": [], "TYPE_B": [], "TYPE_C": [], "TYPE_UNKNOWN": [],
    }
    warnings: List[str] = []

    # ── PHASE 1 + 2 — read every sheet, classify, capture rows ──────
    for filename, raw in files:
        if not raw:
            warnings.append(f"{filename}: empty file")
            continue
        for sh_name, df in _read_sheets(filename, raw):
            if df is None or df.empty:
                continue
            sh = _ingest_sheet(filename, sh_name, df)
            sheets.append(sh)
            rows = _extract_rows(sh)
            tz_set = set()
            for r in rows:
                tz = r.get("timezone")
                if tz:
                    tz_set.add(str(tz).upper().strip())
            d_min, d_max = _date_range(rows)
            file_inventory.append({
                "file": filename,
                "sheet": sh_name,
                "detected_type": sh.file_type,
                "rows": len(rows),
                "date_range": (
                    f"{d_min.isoformat()} → {d_max.isoformat()}"
                    if d_min and d_max else "—"
                ),
                "timezone": ", ".join(sorted(tz_set)) or "—",
                "raw_columns": list(df.columns),
                "matched_columns": sh.header_map,
            })
            if sh.file_type == "TYPE_UNKNOWN":
                warnings.append(
                    f"{filename}/{sh_name}: could not classify — "
                    f"columns: {list(df.columns)[:8]}"
                )
            all_rows_by_type[sh.file_type].extend(rows)

    # ── Build per-batch run series for Type B & Type C ──────────────
    per_batch_runs: Dict[str, List[Dict[str, Any]]] = {}

    def _run_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = rec.get("batch_name")
        if not name:
            return None
        s_nt = normalize_time(rec.get("start_time")) if rec.get("start_time") else NormalizedTime("")
        e_nt = normalize_time(rec.get("end_time")) if rec.get("end_time") else NormalizedTime("")
        # Duration: prefer explicit, else computed
        dur_secs: Optional[int] = None
        raw_dur = rec.get("duration")
        if raw_dur is not None:
            try:
                dv = float(raw_dur)
                # Heuristic: if value looks like seconds use as-is, else mins
                dur_secs = int(dv) if dv > 600 else int(dv * 60)
            except Exception:
                dur_secs = None
        if dur_secs is None:
            dur_secs = compute_duration_secs(s_nt, e_nt)

        run_date = s_nt.date or e_nt.date
        return {
            "batch_name": str(name).strip(),
            "run_date": run_date,
            "start_nt": s_nt,
            "end_nt": e_nt,
            "duration_secs": dur_secs,
            "status": rec.get("status"),
            "volume": _maybe_float(rec.get("volume")),
            "error_count": _maybe_int(rec.get("error_count")),
            "long_running": _truthy(rec.get("long_running")),
            "comments": rec.get("comments") or "",
            "_file": rec.get("_file"),
            "_sheet": rec.get("_sheet"),
            "_row": rec.get("_row"),
            "_file_type": rec.get("_file_type"),
        }

    for r in all_rows_by_type["TYPE_B"] + all_rows_by_type["TYPE_C"]:
        rec = _run_record(r)
        if rec:
            per_batch_runs.setdefault(_norm_key(rec["batch_name"]), []).append(rec)

    # ── PHASE 6 — comments baseline & dependencies (any source) ─────
    baseline_drifts: List[Dict[str, Any]] = []
    dependency_chain: List[str] = []
    contracts_for_join: List[Dict[str, Any]] = []  # Type-A normalized

    for r in all_rows_by_type["TYPE_A"]:
        name = r.get("batch_name") or "—"
        comment = str(r.get("comments") or "")
        baseline = parse_baseline(comment)
        deps = extract_dependencies(comment)
        if deps:
            dependency_chain.append(f"{name}: depends on {', '.join(deps)}")

        s_nt = normalize_time(r.get("start_time")) if r.get("start_time") else NormalizedTime("")
        sla_nt = normalize_time(r.get("sla_deadline")) if r.get("sla_deadline") else NormalizedTime("")
        end_nt = normalize_time(r.get("end_time")) if r.get("end_time") else NormalizedTime("")

        contract = {
            "batch_name": name,
            "schedule": r.get("schedule") or "",
            "module": r.get("module") or "",
            "start_secs": s_nt.utc_seconds,
            "end_secs": end_nt.utc_seconds,
            "sla_secs": sla_nt.utc_seconds,
            "comments": comment,
            "first_job": r.get("first_job"),
            "last_job": r.get("last_job"),
            "_file": r.get("_file"),
            "_sheet": r.get("_sheet"),
            "_row": r.get("_row"),
            "baseline": baseline,
            "dependencies": deps,
        }
        contracts_for_join.append(contract)

        if baseline:
            # If actual end is in the same row we can compute drift now
            window_secs = compute_duration_secs(s_nt, end_nt) if s_nt.is_valid and end_nt.is_valid else None
            current_secs = window_secs
            # Otherwise pull from per_batch_runs
            if current_secs is None:
                runs = per_batch_runs.get(_norm_key(name), [])
                if runs:
                    durs = [x["duration_secs"] for x in runs if x.get("duration_secs")]
                    if durs:
                        current_secs = sum(durs) // len(durs)
            if current_secs:
                drift = classify_drift(baseline["secs"], current_secs)
                baseline_drifts.append({
                    "batch": name,
                    "baseline_label": baseline["label"],
                    "baseline_human": baseline["human"],
                    "current_human": humanize_duration(current_secs),
                    "drift_pct": drift["drift_pct"],
                    "label": drift["label"],
                })

    # ── PHASE 4 — trend per batch (using Type-B / Type-C runs) ──────
    trends: List[Dict[str, Any]] = []
    for key, runs in per_batch_runs.items():
        # Convert run_date into ISO string for analyze_trends contract
        run_inputs = [{
            "run_date": r["run_date"],
            "duration_secs": r["duration_secs"],
            "volume": r["volume"],
        } for r in runs if r.get("duration_secs")]
        t = analyze_trends(run_inputs)
        if t:
            t["batch"] = runs[0]["batch_name"]
            trends.append(t)

    # ── PHASE 5 — Type A ⊕ Type B Join ──────────────────────────────
    join_results: List[Dict[str, Any]] = []
    matched_b_keys: set = set()
    contract_index = {_norm_key(c["batch_name"]): c for c in contracts_for_join}

    for key, runs in per_batch_runs.items():
        # Find best contract match (exact or fuzzy)
        contract = contract_index.get(key) or _fuzzy_pick(key, contract_index)
        if not contract:
            continue
        matched_b_keys.add(key)
        for run in runs:
            # Use Type A's deadline + Type B's actual end
            sla_secs = contract.get("sla_secs")
            actual_end_secs = run["end_nt"].utc_seconds
            window_secs = None
            if contract.get("start_secs") is not None and sla_secs is not None:
                window_secs = sla_secs - contract["start_secs"]
                if window_secs < 0:
                    window_secs += 86400
            verdict = classify_buffer(sla_secs, actual_end_secs, window_secs)
            join_results.append({
                "batch": contract["batch_name"],
                "sla_contract_source": f"{contract['_file']} / {contract['_sheet']} row {contract['_row']}",
                "execution_source": f"{run['_file']} / {run['_sheet']} row {run['_row']}",
                "run_date": run["run_date"].isoformat() if run["run_date"] else "—",
                "actual_end": humanize_duration(actual_end_secs) if actual_end_secs is not None else "—",
                "sla_deadline": humanize_duration(sla_secs) if sla_secs is not None else "—",
                "buffer": verdict["label"],
                "status": verdict["status"],
                "buffer_secs": verdict["buffer_secs"],
            })

    # Type-B without matching Type-A
    for key, runs in per_batch_runs.items():
        if key in matched_b_keys:
            continue
        # Only if we actually have Type-A contracts to compare against
        if contracts_for_join:
            for run in runs:
                join_results.append({
                    "batch": runs[0]["batch_name"],
                    "sla_contract_source": "—",
                    "execution_source": f"{run['_file']} / {run['_sheet']} row {run['_row']}",
                    "run_date": run["run_date"].isoformat() if run["run_date"] else "—",
                    "actual_end": humanize_duration(run["end_nt"].utc_seconds) if run["end_nt"].utc_seconds is not None else "—",
                    "sla_deadline": "—",
                    "buffer": "No SLA contract found — unmonitored job",
                    "status": "UNDEFINED",
                    "buffer_secs": None,
                })

    # Type-A without matching Type-B
    for key, contract in contract_index.items():
        if key not in matched_b_keys and per_batch_runs:
            join_results.append({
                "batch": contract["batch_name"],
                "sla_contract_source": f"{contract['_file']} / {contract['_sheet']} row {contract['_row']}",
                "execution_source": "—",
                "run_date": "—",
                "actual_end": "—",
                "sla_deadline": humanize_duration(contract["sla_secs"]) if contract.get("sla_secs") is not None else "—",
                "buffer": "No execution record found — did this batch run?",
                "status": "UNDEFINED",
                "buffer_secs": None,
            })

    # ── PHASE 7 — Schedule overlaps ─────────────────────────────────
    overlaps = detect_overlaps(contracts_for_join)

    # ── PHASE 3 — Per-row breach classification (Type A + Type C) ───
    breaches: List[Dict[str, Any]] = []
    at_risk: List[Dict[str, Any]] = []
    healthy: List[Dict[str, Any]] = []

    # From Type A rows that have actual_end + sla_deadline
    for c in contracts_for_join:
        if c.get("sla_secs") is None or c.get("end_secs") is None:
            continue
        window = None
        if c.get("start_secs") is not None:
            window = c["sla_secs"] - c["start_secs"]
            if window < 0:
                window += 86400
        v = classify_buffer(c["sla_secs"], c["end_secs"], window)
        item = {
            "batch": c["batch_name"],
            "date": "—",
            "sla_deadline": humanize_duration(c["sla_secs"]),
            "actual_end": humanize_duration(c["end_secs"]),
            "buffer": v["label"],
            "status": v["status"],
            "trend": _lookup_trend(c["batch_name"], trends),
            "drift": _lookup_drift(c["batch_name"], baseline_drifts),
        }
        if v["status"] == "BREACH":
            item["over_by"] = humanize_duration(abs(v["buffer_secs"])) if v["buffer_secs"] else "—"
            item["root_hint"] = _root_hint(item, c, trends)
            breaches.append(item)
        elif v["status"] == "RISK":
            item["next_run_risk"] = "Buffer < 10% of window — will breach if next run grows"
            at_risk.append(item)
        else:
            healthy.append(item)

    # From join_results too (Type B execution rows)
    for jr in join_results:
        if jr["status"] == "BREACH":
            breaches.append({
                "batch": jr["batch"],
                "date": jr["run_date"],
                "sla_deadline": jr["sla_deadline"],
                "actual_end": jr["actual_end"],
                "over_by": humanize_duration(abs(jr["buffer_secs"])) if jr.get("buffer_secs") else "—",
                "root_hint": "See join row — actual execution exceeded contracted SLA",
                "status": "BREACH",
                "trend": _lookup_trend(jr["batch"], trends),
            })
        elif jr["status"] == "RISK":
            at_risk.append({
                "batch": jr["batch"],
                "buffer_remaining": jr["buffer"],
                "trend": _lookup_trend(jr["batch"], trends),
                "next_run_risk": "Headroom thin — next growth will breach",
            })

    # ── PHASE 8 — Type C status mismatch ───────────────────────────
    type_c_mismatches: List[Dict[str, Any]] = []
    for r in all_rows_by_type["TYPE_C"]:
        status = str(r.get("status") or "").upper()
        if "PASS" in status:
            # If runtime > sla_deadline window, flag mismatch
            dur = r.get("duration")
            try:
                dur_secs = int(float(dur)) if dur else None
            except Exception:
                dur_secs = None
            if dur_secs is None:
                continue
            sla_nt = normalize_time(r.get("sla_deadline")) if r.get("sla_deadline") else None
            start_nt = normalize_time(r.get("start_time")) if r.get("start_time") else None
            window = None
            if sla_nt and start_nt and sla_nt.utc_seconds is not None and start_nt.utc_seconds is not None:
                window = sla_nt.utc_seconds - start_nt.utc_seconds
                if window < 0:
                    window += 86400
            if window and dur_secs > window:
                type_c_mismatches.append({
                    "batch": r.get("batch_name"),
                    "row": r.get("_row"),
                    "label": (f"STATUS MISMATCH — runtime {humanize_duration(dur_secs)} "
                              f"exceeds window {humanize_duration(window)} but status=PASS"),
                })

    # ── Anomalies ──────────────────────────────────────────────────
    anomalies: List[Dict[str, Any]] = []
    for r in all_rows_by_type["TYPE_B"]:
        if _truthy(r.get("long_running")):
            anomalies.append({"type": "LONG_RUNNING",
                              "batch": r.get("batch_name"),
                              "label": f"{r.get('batch_name')} flagged LONG_RUNNING"})
        ec = _maybe_int(r.get("error_count"))
        if ec and ec > 0:
            anomalies.append({"type": "ERRORS",
                              "batch": r.get("batch_name"),
                              "label": f"{r.get('batch_name')} reported {ec} errors"})
    for c in contracts_for_join:
        if c.get("sla_secs") is None:
            anomalies.append({"type": "NO_SLA",
                              "batch": c["batch_name"],
                              "label": f"{c['batch_name']} has no SLA defined"})
    for jr in join_results:
        if jr["status"] == "UNDEFINED" and jr["sla_contract_source"] == "—":
            anomalies.append({"type": "ORPHAN_EXECUTION",
                              "batch": jr["batch"],
                              "label": f"{jr['batch']} executed without SLA contract"})
        elif jr["status"] == "UNDEFINED" and jr["execution_source"] == "—":
            anomalies.append({"type": "MISSING_EXECUTION",
                              "batch": jr["batch"],
                              "label": f"{jr['batch']} has SLA contract but no execution record"})
    for m in type_c_mismatches:
        anomalies.append({"type": "STATUS_MISMATCH", "batch": m["batch"], "label": m["label"]})

    # ── Recommendations (priority-ordered) ─────────────────────────
    recommendations = _build_recommendations(
        breaches, at_risk, trends, baseline_drifts, overlaps, anomalies,
    )

    return {
        "file_inventory": file_inventory,
        "sla_breaches": breaches,
        "at_risk": at_risk,
        "healthy": healthy,
        "trends": trends,
        "baseline_drifts": baseline_drifts,
        "dependency_chain": dependency_chain,
        "join_results": join_results,
        "overlaps": overlaps,
        "type_c_mismatches": type_c_mismatches,
        "anomalies": anomalies,
        "recommendations": recommendations,
        "warnings": warnings,
    }


# ─────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────

def _norm_key(name: str) -> str:
    return re.sub(r"[\s_\-]+", "", str(name or "").lower())


def _fuzzy_pick(key: str, index: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Best fuzzy contract match (≥0.80 SequenceMatcher ratio)."""
    if not key or not index:
        return None
    try:
        from difflib import SequenceMatcher
        best = None
        best_score = 0.80
        for k, v in index.items():
            score = SequenceMatcher(None, key, k).ratio()
            if score > best_score:
                best_score = score
                best = v
        return best
    except Exception:
        return None


def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "", "nan") else None
    except Exception:
        return None


def _maybe_int(v) -> Optional[int]:
    try:
        return int(float(v)) if v not in (None, "", "nan") else None
    except Exception:
        return None


def _truthy(v) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("y", "yes", "true", "1", "t")


def _lookup_trend(batch: str, trends: List[Dict[str, Any]]) -> str:
    key = _norm_key(batch)
    for t in trends:
        if _norm_key(t.get("batch", "")) == key:
            return t.get("trend_label", t.get("trend", ""))
    return "—"


def _lookup_drift(batch: str, drifts: List[Dict[str, Any]]) -> str:
    key = _norm_key(batch)
    for d in drifts:
        if _norm_key(d.get("batch", "")) == key:
            return d.get("label", "")
    return "—"


def _root_hint(item: Dict[str, Any], contract: Dict[str, Any],
               trends: List[Dict[str, Any]]) -> str:
    """Generate a short root-cause hint by combining trend + drift signals."""
    parts = []
    t = item.get("trend", "")
    if "growing" in t.lower() or "trending_up" in t.lower():
        parts.append("growth trend confirmed")
    drift = item.get("drift", "")
    if "growing" in drift.lower() or "critical" in drift.lower():
        parts.append("baseline drift")
    if contract.get("comments"):
        if re.search(r"index|stats|partition|archive", contract["comments"], re.I):
            parts.append("config hint in comments")
    return "; ".join(parts) or "investigate dependency / volume"


def _build_recommendations(
    breaches: List[Dict[str, Any]],
    at_risk: List[Dict[str, Any]],
    trends: List[Dict[str, Any]],
    drifts: List[Dict[str, Any]],
    overlaps: List[Dict[str, Any]],
    anomalies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if breaches:
        out.append({"priority": "CRITICAL",
                    "text": f"{len(breaches)} SLA breach(es) detected — open RCA on top breaching batches"})
    crit_drifts = [d for d in drifts if "CRITICAL" in d.get("label", "")]
    if crit_drifts:
        out.append({"priority": "CRITICAL",
                    "text": f"{len(crit_drifts)} batch(es) drifted >50% from historical baseline — refresh stats / rebuild indexes"})
    actual_overlaps = [o for o in overlaps if o["risk"] == "ACTUAL_OVERLAP"]
    if actual_overlaps:
        out.append({"priority": "HIGH",
                    "text": f"{len(actual_overlaps)} schedule overlap(s) — adjust start times to remove contention"})
    trending_up = [t for t in trends if t.get("trend") == "TRENDING_UP"]
    if trending_up:
        out.append({"priority": "HIGH",
                    "text": f"{len(trending_up)} batch(es) trending up — capacity review + archival before they breach"})
    spikes = [t for t in trends if t.get("spike")]
    if spikes:
        out.append({"priority": "MEDIUM",
                    "text": f"{len(spikes)} runtime spike(s) detected — correlate with deployments / data anomalies"})
    if at_risk:
        out.append({"priority": "MEDIUM",
                    "text": f"{len(at_risk)} batch(es) at-risk — proactive tuning before breach"})
    no_sla = [a for a in anomalies if a["type"] == "NO_SLA"]
    if no_sla:
        out.append({"priority": "LOW",
                    "text": f"{len(no_sla)} batch(es) without SLA — define contracts for monitoring"})
    orphans = [a for a in anomalies if a["type"] == "ORPHAN_EXECUTION"]
    if orphans:
        out.append({"priority": "LOW",
                    "text": f"{len(orphans)} unmonitored job(s) executing without SLA contract — register them"})
    return out
