"""
Smart File Router — auto-classifies any uploaded file into a PE document type
without requiring the user to specify the type.

Classification:
    "batch"      — Ctrl-M execution history CSV/XLSX (has JOB_NAME / ORDER_ID style cols)
    "resource"   — Zabbix/Azure resource utilization DOCX/PDF
    "benchmark"  — UI performance benchmark XLSX/CSV (Page Load Time, Response Time cols)
    "sla_matrix" — Batch SLA Matrix XLSX (pre-formatted SLA table)
    "awr"        — Oracle AWR HTML/TXT (contains "Elapsed:", "DB Time:")
    "kpi_txt"    — KPI/data-volume report TXT (SKU, DFU count tables)
    "extra"      — Catch-all for unrecognised files

Public API:
    classify(raw_bytes, filename) -> dict  { type, confidence, notes }
"""
from __future__ import annotations

import io
import os
import re
from typing import Any


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return os.path.splitext((filename or "").lower())[1]


def _read_text_sample(raw_bytes: bytes, max_chars: int = 4_000) -> str:
    """Try to decode the first max_chars bytes as UTF-8 text."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes[:max_chars].decode(enc, errors="replace")
        except Exception:
            pass
    return ""


def _read_xlsx_headers(raw_bytes: bytes) -> list[str]:
    """Return the header row of the first sheet (normalised to lowercase)."""
    try:
        import pandas as pd
        df = pd.read_excel(io.BytesIO(raw_bytes), nrows=3, engine="openpyxl")
        headers = [str(c).lower().replace(" ", "_").replace("-", "_") for c in df.columns]
        # Also check first data row in case row 0 is a true header
        return headers
    except Exception:
        return []


def _read_csv_headers(raw_bytes: bytes) -> list[str]:
    try:
        import pandas as pd
        df = pd.read_csv(io.BytesIO(raw_bytes), nrows=3)
        return [str(c).lower().replace(" ", "_").replace("-", "_") for c in df.columns]
    except Exception:
        return []


# ── Classification rules ─────────────────────────────────────────────────────

_BATCH_HEADER_SIGNALS = {
    "job_name", "jobname", "order_id", "orderid", "sub_application",
    "application", "folder", "end_time", "start_time", "run_sec",
    "ended_ok", "run_time_hrs",
}

_BATCH_VALUE_SIGNALS = [
    r"ENDED OK", r"ENDED NOT OK", r"WAIT_HOST", r"ORDER_ID",
    r"Ctrl.?M", r"Control.?M", r"batch\s+job",
]

_BENCHMARK_HEADER_SIGNALS = {
    "page_load", "load_time", "response_time", "login_time",
    "transaction", "txn_name", "avg_time", "p90", "p95", "p99",
    "throughput", "tps", "rps", "vusers", "baseline", "current",
}

_BENCHMARK_VALUE_SIGNALS = [
    r"Page Load", r"Login\s+Time", r"Save Order", r"Checkout",
    r"Response\s+Time", r"Throughput\s*\(", r"SLA.*ms",
]

_RESOURCE_SIGNALS = [
    r"System Status for\s+\S+",
    r"Graphs for\s+\S+",
    r"CPU idle time",
    r"Memory utilization",
    r"Free disk space",
    r"Zabbix",
    r"Resource Utilization",
    r"Azure Monitor",
]

_AWR_SIGNALS = [
    r"Elapsed\s*:", r"DB Time\s*:", r"AWR\s+Report",
    r"WORKLOAD REPOSITORY", r"Top 10 Foreground Events",
    r"Snap Id", r"Buffer Gets",
]

_KPI_SIGNALS = [
    r"DFU", r"SKU", r"SOW", r"S\.Total",
    r"Active.*Inactive", r"Data Volume",
]

_SLA_MATRIX_SIGNALS = {
    "sla", "buffer", "breach", "sla_limit", "sla_hrs",
    "daily_limit", "compliance", "sla_status",
}


def classify(raw_bytes: bytes, filename: str) -> dict[str, Any]:
    """
    Classify uploaded file into a PE document type.

    Returns:
        {
            "type":       str,   # batch | resource | benchmark | sla_matrix | awr | kpi_txt | extra
            "confidence": str,   # high | medium | low
            "notes":      str,   # human-readable reason
        }
    """
    ext = _ext(filename)
    fname_lower = filename.lower()

    # ── Spreadsheet types ─────────────────────────────────────────
    if ext in (".xlsx", ".xls"):
        hdrs = _read_xlsx_headers(raw_bytes)
        hdr_set = set(hdrs)

        # Check for SLA matrix keywords
        if _SLA_MATRIX_SIGNALS & hdr_set:
            return {"type": "sla_matrix", "confidence": "high",
                    "notes": f"SLA/compliance columns: {_SLA_MATRIX_SIGNALS & hdr_set}"}

        # Check for benchmark keywords
        bench_hits = _BENCHMARK_HEADER_SIGNALS & hdr_set
        if bench_hits or any(k in fname_lower for k in ("benchmark", "perf_test", "ui_test", "load_test")):
            return {"type": "benchmark", "confidence": "high" if bench_hits else "medium",
                    "notes": f"UI benchmark columns: {bench_hits}"}

        # Check for Ctrl-M batch keywords
        batch_hits = _BATCH_HEADER_SIGNALS & hdr_set
        if batch_hits:
            return {"type": "batch", "confidence": "high",
                    "notes": f"Ctrl-M columns: {batch_hits}"}

        # Sample first few rows for value signals
        text_sample = _read_text_sample(raw_bytes)
        for pat in _BATCH_VALUE_SIGNALS:
            if re.search(pat, text_sample, re.I):
                return {"type": "batch", "confidence": "medium",
                        "notes": f"Ctrl-M value pattern: {pat}"}

        return {"type": "extra", "confidence": "low", "notes": "Unrecognised XLSX structure"}

    if ext == ".csv":
        hdrs = _read_csv_headers(raw_bytes)
        hdr_set = set(hdrs)
        text_sample = _read_text_sample(raw_bytes, 2_000)

        if _BATCH_HEADER_SIGNALS & hdr_set:
            return {"type": "batch", "confidence": "high",
                    "notes": f"Ctrl-M CSV columns: {_BATCH_HEADER_SIGNALS & hdr_set}"}
        if _BENCHMARK_HEADER_SIGNALS & hdr_set:
            return {"type": "benchmark", "confidence": "high",
                    "notes": "Benchmark CSV columns detected"}
        for pat in _BATCH_VALUE_SIGNALS:
            if re.search(pat, text_sample, re.I):
                return {"type": "batch", "confidence": "medium",
                        "notes": f"Batch value pattern: {pat}"}
        return {"type": "extra", "confidence": "low", "notes": "Unrecognised CSV"}

    # ── PDF / DOCX ────────────────────────────────────────────────
    if ext in (".pdf", ".docx"):
        # Resource-specific filename hints
        for hint in ("resource", "zabbix", "utilization", "utilisation", "azure", "consumption"):
            if hint in fname_lower:
                return {"type": "resource", "confidence": "high",
                        "notes": f"Filename hint: '{hint}'"}

        # Extract text: use python-docx for DOCX (better than raw bytes)
        if ext == ".docx":
            try:
                from docx import Document
                doc = Document(io.BytesIO(raw_bytes))
                text = "\n".join(p.text for p in doc.paragraphs[:200])
            except Exception:
                text = _read_text_sample(raw_bytes)
        elif ext == ".pdf":
            try:
                from services.resource_parser import extract_pdf_text
                text = extract_pdf_text(io.BytesIO(raw_bytes))
            except Exception:
                text = _read_text_sample(raw_bytes)
        else:
            text = _read_text_sample(raw_bytes)

        for pat in _AWR_SIGNALS:
            if re.search(pat, text, re.I):
                return {"type": "awr", "confidence": "high",
                        "notes": f"AWR pattern: {pat}"}

        resource_hits = sum(1 for pat in _RESOURCE_SIGNALS if re.search(pat, text, re.I))
        if resource_hits >= 2:
            return {"type": "resource", "confidence": "high",
                    "notes": f"{resource_hits} resource/Zabbix patterns found"}
        if resource_hits == 1:
            return {"type": "resource", "confidence": "medium",
                    "notes": "1 resource pattern found"}

        for pat in _BENCHMARK_VALUE_SIGNALS:
            if re.search(pat, text, re.I):
                return {"type": "benchmark", "confidence": "medium",
                        "notes": f"Benchmark pattern: {pat}"}

        return {"type": "resource", "confidence": "low",
                "notes": "PDF/DOCX assumed resource report (fallback)"}

    # ── Plain text ────────────────────────────────────────────────
    if ext == ".txt":
        text = _read_text_sample(raw_bytes)

        for pat in _AWR_SIGNALS:
            if re.search(pat, text, re.I):
                return {"type": "awr", "confidence": "high", "notes": f"AWR pattern: {pat}"}

        kpi_hits = sum(1 for pat in _KPI_SIGNALS if re.search(pat, text))
        if kpi_hits >= 2:
            return {"type": "kpi_txt", "confidence": "high",
                    "notes": f"{kpi_hits} KPI/data-volume patterns"}

        for pat in _BATCH_VALUE_SIGNALS:
            if re.search(pat, text, re.I):
                return {"type": "batch", "confidence": "medium",
                        "notes": f"Batch pattern in txt: {pat}"}

        return {"type": "kpi_txt", "confidence": "low", "notes": "Plain text, treated as KPI/extra info"}

    # ── HTML ──────────────────────────────────────────────────────
    if ext in (".html", ".htm"):
        text = _read_text_sample(raw_bytes, 3_000)
        for pat in _AWR_SIGNALS:
            if re.search(pat, text, re.I):
                return {"type": "awr", "confidence": "high", "notes": "AWR HTML report"}
        return {"type": "extra", "confidence": "medium", "notes": "HTML file — stored as extra info"}

    return {"type": "extra", "confidence": "low",
            "notes": f"Unsupported extension '{ext}'"}
