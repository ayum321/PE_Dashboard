"""
sla_parser.py — SLA ceiling extraction from customer SLA XLSX files.

Reads an uploaded SLA Matrix XLSX and derives the SLA window (hours) per
batch type (DAILY / WEEKLY / MONTHLY / CUSTOM).

Works for any customer format — Start Time + Expected End Time columns.
Falls back to pe_config defaults when parsing fails.

Public API:
    extract_sla_from_xlsx(file_bytes) → dict[str, float]
    detect_batch_type(job_name)        → str   (DAILY | WEEKLY | MONTHLY)
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

logger = logging.getLogger("pe_dashboard.sla_parser")


# ── Time parsing helpers ──────────────────────────────────────────────────────

_TIME_FORMATS = [
    "%I:%M %p",   # 9:00 PM
    "%I.%M %p",   # 10.45 AM  (dot-notation — Haleon style)
    "%I.%M%p",    # 10.45AM   (dot-notation no space)
    "%H:%M",      # 21:00
    "%I %p",      # 9 PM
    "%I:%M%p",    # 9:00PM (no space)
    "%H:%M:%S",   # 21:00:00
    "%I:%M:%S %p",# 9:00:00 PM
    "%I%p",       # 5AM (no colon)
    "%I %p",      # 5 AM
]


def _parse_time(raw: str):
    """Parse a time string in various formats. Returns datetime.time or None.

    Handles:
      9:00 PM, 10.45 AM, 21:00, 5AM CST, 5AM IST (timezone suffix stripped)
    """
    from datetime import datetime
    import re as _re
    if not raw or str(raw).lower() in ("nan", "none", ""):
        return None
    raw = str(raw).strip()
    # Strip trailing timezone qualifiers: "5AM CST", "11:00 PM EDT", "6 PM IST"
    raw = _re.sub(r'\s+(?:CST|CDT|EST|EDT|PST|PDT|MST|MDT|IST|GMT|UTC[+-]?\d*)\s*$', '', raw, flags=_re.IGNORECASE).strip()
    # Strip parenthetical notes: "11:00 AM (Sequencing SLA)"
    raw = _re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
    # Strip trailing context text after first time-like token: "7:00 AM (Morning SLA)" → "7:00 AM"
    _tm = _re.match(r'^(\d{1,2}[.:][0-5]\d(?::\d{2})?(?:\s*[AP]M)?)', raw, _re.IGNORECASE)
    if _tm:
        raw = _tm.group(1).strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).time()
        except (ValueError, TypeError):
            continue
    # Try to extract HH:MM from a datetime string like "2024-01-01 21:00:00"
    m = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?', raw, re.IGNORECASE)
    if m:
        try:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ampm = (m.group(4) or "").upper()
            if ampm == "PM" and hh < 12:
                hh += 12
            elif ampm == "AM" and hh == 12:
                hh = 0
            from datetime import time as dtime
            return dtime(hh, mm)
        except Exception:
            pass
    return None


def _time_delta_hours(start, end) -> float | None:
    """Calculate hours between two time objects. Handles overnight crossings."""
    if start is None or end is None:
        return None
    from datetime import datetime, date
    base = date(2000, 1, 1)
    dt_start = datetime.combine(base, start)
    dt_end   = datetime.combine(base, end)
    delta    = (dt_end - dt_start).total_seconds() / 3600.0
    if delta < 0:
        delta += 24.0  # overnight batch
    return round(delta, 2) if delta > 0 else None


# ── Column detection ──────────────────────────────────────────────────────────

_BATCH_NAME_COLS = [
    "BATCH_NAME", "BATCH NAME", "BATCH TYPE", "BATCHNAME",
    "SCHEDULE", "SCHEDULE TYPE", "SCHEDULE_TYPE", "TYPE",
    "WINDOW", "WINDOW_NAME", "WINDOW TYPE",
]
_START_COLS = [
    "START TIME", "START_TIME", "STARTTIME", "START",
    "WINDOW START", "BEGIN", "OPEN",
]
_END_COLS = [
    "EXPECTED END TIME", "EXPECTED_END_TIME", "EXPECTED END", "END TIME",
    "END_TIME", "ENDTIME", "END", "EXPECTED FINISH", "CLOSE", "WINDOW END",
    "WINDOW_END", "DEADLINE",
    # Additional customer formats found in audit:
    "SLA CUTOFF", "SLA_CUTOFF", "CUTOFF",          # Batch_Plan__SLA_Check.csv
    "EXPECTED END TIME/SLA", "EXPECTED END TIME/SLA (2019)",  # HaleonUK, THS IO, Chervon
    "SLA", "SLA TIME", "SLA_TIME",                  # generic
]


def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    norm = {h.upper().strip().replace("-", "_").replace(" ", "_"): h for h in headers}
    for c in candidates:
        key = c.upper().replace(" ", "_")
        if key in norm:
            return norm[key]
    # Partial match fallback
    for c in candidates:
        c_key = c.upper().replace(" ", "").replace("_", "")
        for h_norm, h_orig in norm.items():
            h_key = h_norm.replace("_", "")
            if c_key in h_key or h_key in c_key:
                return h_orig
    return None


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_sla_from_xlsx(file_bytes: bytes) -> dict[str, float]:
    """
    Parse an SLA Matrix XLSX and extract the SLA window (hours) per batch type.

    Delegates to sla_engine.ingest_sla_file() for intelligent schema detection,
    then returns the simplified ceilings dict for backward compatibility.

    Falls back to pe_config defaults when parsing fails.
    """
    try:
        from services.sla_engine import ingest_sla_file
        result = ingest_sla_file(file_bytes, "sla_matrix.xlsx")
        if result.valid_rows > 0 and result.ceilings:
            logger.info("sla_parser: delegated to sla_engine — ceilings=%s", result.ceilings)
            return result.ceilings
        logger.warning("sla_parser: sla_engine found no valid SLA rows — falling back")
    except Exception as exc:
        logger.warning("sla_parser: sla_engine delegation failed — %s", exc)

    return _extract_sla_legacy(file_bytes)


def _extract_sla_legacy(file_bytes: bytes) -> dict[str, float]:
    """Legacy extraction path — direct column scanning."""
    try:
        import pandas as pd
        xls = pd.read_excel(io.BytesIO(file_bytes), sheet_name=0, engine="openpyxl")
        xls.columns = [str(c).strip() for c in xls.columns]
    except Exception as exc:
        logger.warning("sla_parser: failed to read XLSX — %s", exc)
        return _config_fallback()

    headers = list(xls.columns)
    name_col  = _find_col(headers, _BATCH_NAME_COLS)
    start_col = _find_col(headers, _START_COLS)
    end_col   = _find_col(headers, _END_COLS)

    logger.info(
        "sla_parser: detected columns — name=%s start=%s end=%s",
        name_col, start_col, end_col,
    )

    sla_map: dict[str, float] = {}

    if start_col and end_col:
        for _, row in xls.iterrows():
            try:
                start = _parse_time(str(row.get(start_col, "")))
                end_raw = str(row.get(end_col, ""))
                end   = _parse_time(end_raw)
                hrs   = _time_delta_hours(start, end)
                # Generic fallback: some files put a duration string ("1.5 hrs",
                # "45 min") in the SLA column rather than a clock-time value.
                # When _parse_time returns None, try interpreting as a duration.
                if (hrs is None or hrs <= 0) and end is None:
                    from services.sla_engine import _parse_duration_hrs as _dur
                    hrs = _dur(end_raw)
                if hrs is None or hrs <= 0:
                    continue

                # Determine batch type key
                batch_raw = str(row.get(name_col, "") if name_col else "").upper()
                batch_type = _classify_batch_name(batch_raw)

                if batch_type and batch_type not in sla_map:
                    sla_map[batch_type] = hrs
                    logger.info(
                        "sla_parser: %s → %s → %.1f hrs",
                        batch_raw, batch_type, hrs,
                    )
            except Exception as exc:
                logger.debug("sla_parser: row parse error — %s", exc)
                continue

    if not sla_map:
        logger.warning("sla_parser: no SLA windows extracted — using config fallback")
        return _config_fallback()

    # Fill any missing types with defaults
    fallback = _config_fallback()
    for k, v in fallback.items():
        sla_map.setdefault(k, v)

    logger.info("sla_parser: extracted SLA ceilings: %s", sla_map)
    return sla_map


def _classify_batch_name(name: str) -> str | None:
    """Map a raw batch name/type string to DAILY | WEEKLY | MONTHLY | SEQUENCING | CUSTOM."""
    name = name.upper().strip()
    # SEQUENCING checked before DAILY — "Daily Sequencing" contains "DAILY"
    # but is a distinct contractual window, not the main daily batch.
    if "SEQUENC" in name:
        return "SEQUENCING"
    if any(w in name for w in ("DAILY", "DAY", " D ", "NIGHTLY", "OVERNIGHT")):
        return "DAILY"
    if any(w in name for w in ("WEEKLY", "WEEK", " W ", "WK")):
        return "WEEKLY"
    if any(w in name for w in ("MONTHLY", "MONTH", " M ", "MTH")):
        return "MONTHLY"
    if any(w in name for w in ("CUSTOM", "SPECIAL", "ADHOC", "AD HOC")):
        return "CUSTOM"
    return None


def _config_fallback() -> dict[str, float]:
    """Return SLA ceilings from pe_config when XLSX parse fails."""
    try:
        from services.pe_config import (
            SLA_CUSTOM_HRS, SLA_DAILY_HRS, SLA_MONTHLY_HRS, SLA_WEEKLY_HRS,
        )
        return {
            "DAILY":   SLA_DAILY_HRS,
            "WEEKLY":  SLA_WEEKLY_HRS,
            "MONTHLY": SLA_MONTHLY_HRS,
            "CUSTOM":  SLA_CUSTOM_HRS,
        }
    except Exception:
        from services.sla_merger import GLOBAL_DEFAULTS as _gd
        return {
            "DAILY":   _gd.get("DAILY", 6.0),
            "WEEKLY":  _gd.get("WEEKLY", 17.0),
            "MONTHLY": _gd.get("MONTHLY", 17.0),
            "CUSTOM":  6.0,
        }


# ── Resource file mode detector (RULE 4) ─────────────────────────────────────

def detect_resource_mode(file_bytes: bytes, filename: str) -> str:
    """
    Detect the content type of a resource utilization file BEFORE parsing.

    Returns one of:
      IMAGE_DOCX  — DOCX with only embedded chart screenshots, minimal text
      TEXT_DOCX   — DOCX with parseable text tables
      IMAGE_PDF   — PDF with only chart images
      TEXT_PDF    — PDF with parseable text
      CSV         — CSV spreadsheet
      UNKNOWN     — unrecognised format
    """
    fn = (filename or "").lower()
    if fn.endswith(".csv"):
        return "CSV"
    if fn.endswith((".xlsx", ".xls")):
        return "XLSX"

    if fn.endswith(".docx"):
        import zipfile
        text  = _extract_docx_text_quick(file_bytes)
        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                media = [f for f in z.namelist() if "word/media/" in f
                         and f.lower().endswith((".png", ".jpg", ".jpeg"))]
        except Exception:
            media = []
        # Image-only if: has media images AND very little real text
        # BUT: if the text contains server hostnames (letter-prefix + digits),
        # always classify as TEXT so the parser can extract server stubs.
        stripped = text.strip()
        has_hostname = bool(re.search(
            r'\b[a-z]{2,8}\d{3,}[a-z0-9]*(?:\.[a-z0-9.-]+)?\b', stripped, re.I))
        if media and len(stripped) < 500 and not has_hostname:
            return "IMAGE_DOCX"
        return "TEXT_DOCX"

    if fn.endswith(".pdf"):
        text = _extract_pdf_text_quick(file_bytes)
        if len(text.strip()) > 200:
            return "TEXT_PDF"
        return "IMAGE_PDF"

    return "UNKNOWN"


def _extract_docx_text_quick(file_bytes: bytes, max_chars: int = 2000) -> str:
    """Quickly extract text from DOCX for mode detection."""
    try:
        import zipfile
        text_parts = []
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            if "word/document.xml" in z.namelist():
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
                # Strip XML tags
                text_parts.append(re.sub(r"<[^>]+>", " ", xml))
        return " ".join(text_parts)[:max_chars]
    except Exception:
        return ""


def _extract_pdf_text_quick(file_bytes: bytes, max_chars: int = 2000) -> str:
    """Quickly extract text from PDF for mode detection."""
    try:
        import fitz
        doc  = fitz.open(stream=file_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
            if len(text) > max_chars:
                break
        doc.close()
        return text[:max_chars]
    except Exception:
        return ""
