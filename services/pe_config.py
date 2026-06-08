"""
pe_config.py — Single source of truth for all PE Dashboard thresholds and settings.

Adapts the user's pe_config.py blueprint for the FastAPI (non-Streamlit) stack.
Reads from:
  1. .pe_config.json  (persistent config via config_store)
  2. Environment variables (GEMINI_API_KEY / GOOGLE_API_KEY)
  3. Hardcoded defaults (last resort)

Usage:
    from services import pe_config
    if cpu_value > pe_config.CPU_CRIT: ...
    pe_config.reload()  # re-read from disk after settings change
"""
from __future__ import annotations

from typing import Any

# ── Internal import (lazy to avoid circular) ─────────────────────────────────
def _cfg(key: str, default: Any = None) -> Any:
    """Read a value from config_store with fallback."""
    try:
        from services import config_store
        return config_store.get(key, default)
    except Exception:
        return default


# ── Vision provider ───────────────────────────────────────────────────────────
VISION_PROVIDER: str = "gemini"   # "gemini" | "azure" | "local"


# ── CPU thresholds (%) ────────────────────────────────────────────────────────
@property
def _cpu_warn(self) -> float:
    return float(_cfg("cpu_warning",  75.0))


# Use module-level constants (re-read live via reload() below)
CPU_WARN:  float = 75.0   # Warning band
CPU_CRIT:  float = 90.0   # Critical — rule engine fires

# ── Memory thresholds (%) ─────────────────────────────────────────────────────
MEM_WARN:  float = 70.0
MEM_CRIT:  float = 80.0

# ── Disk thresholds (%) ───────────────────────────────────────────────────────
DISK_WARN: float = 70.0
DISK_CRIT: float = 85.0

# ── Batch quality thresholds ──────────────────────────────────────────────────
BATCH_FAIL_RATE:  float = 5.0    # % failure rate above which rule R4 fires
ZERO_DUR_FLAG:    bool  = True   # Flag zero-duration jobs in findings

# ── SLA windows (hours) ──────────────────────────────────────────────
# Generic configurable defaults — NEVER hardcode 5 / 3.5 / 8 anywhere else.
# Override via Settings → config_store keys: daily_sla_hrs, weekly_sla_hrs,
# biweekly_sla_hrs, monthly_sla_hrs, custom_sla_hrs.
SLA_DEFAULTS: dict[str, float] = {
    "daily":    6.0,
    "weekly":   17.0,
    "biweekly": 17.0,
    "monthly":  17.0,
    "custom":   6.0,
}
SLA_DAILY_HRS:    float = SLA_DEFAULTS["daily"]
SLA_WEEKLY_HRS:   float = SLA_DEFAULTS["weekly"]
SLA_BIWEEKLY_HRS: float = SLA_DEFAULTS["biweekly"]
SLA_MONTHLY_HRS:  float = SLA_DEFAULTS["monthly"]
SLA_CUSTOM_HRS:   float = SLA_DEFAULTS["custom"]
SLA_BUFFER_WARN:  float = 15.0   # % buffer below which job is AT_RISK (kept for backward compat)

# ── SLA status classification thresholds ─────────────────────────────────────
# Single canonical source — sla_matrix.py, sla_merger.py, and the UI legend
# all read from here. Override via config_store keys: sla_atrisk_pct, sla_longjob_pct.
# Formula: buffer_pct = (SLA_h − runtime_h) / SLA_h × 100
#   buffer ≤  0%              → BREACH
#   0% < buffer ≤ AT_RISK_PCT  → AT_RISK   (e.g. 15% → runtime uses ≥85% of SLA)
#   AT_RISK < buffer ≤ LONGJOB → LONG_JOB  (e.g. 40% → runtime uses ≥60% of SLA)
#   buffer > LONGJOB_PCT        → OK
SLA_ATRISK_PCT:   float = 15.0   # % buffer threshold → AT_RISK below this
SLA_LONGJOB_PCT:  float = 40.0   # % buffer threshold → LONG_JOB below this

# ── Benchmark ─────────────────────────────────────────────────────────────────
BENCH_THRESHOLD_PCT: float = 10.0   # % degradation → RED

# ── Anomaly detection ─────────────────────────────────────────────────────────
ANOMALY_Z_THRESHOLD: float = 2.0    # z-score cutoff for statistical outliers

# ── Ctrl-M job classification (customer-configurable) ─────────────────────────
# job_type_patterns: batch_type → list of substrings to match in job/workflow name
JOB_TYPE_PATTERNS: dict = {
    "DAILY":     ["_DLY", "DAILY_", "_DAY", "DAY_RUN", "NIGHTLY", "OVERNIGHT", "EVERYDAY"],
    "WEEKLY":    ["_WLY", "WEEKLY_", "WK_", "_WEEK", "_WF", "-WF"],
    "BIWEEKLY":  ["_BIWKLY", "BIWEEKLY_", "_BWLY", "BI_WEEKLY", "BI-WEEKLY"],
    "QUARTERLY": ["QUARTERLY", "QUATERLY", "QTR"],
    "MONTHLY":   ["_MLY", "MONTHLY_", "_MNTH"],
    "CYCLIC":    ["CYCLIC", "_CYC", "CRON_"],
    "OUTBOUND":  ["OUTBOUND", "_OUTBND", "EXPORT_"],
}

# exclude_from_sla: batch types that are silently excluded from SLA matrix analysis
EXCLUDE_FROM_SLA: list = ["CYCLIC", "OUTBOUND"]

# env_prefixes_to_strip: stripped from job/workflow names before matching
ENV_PREFIXES_TO_STRIP: list = ["PROD_", "TEST_", "UAT_", "DEV_", "STG_"]

# ctrlm_column_map: canonical → list of raw Ctrl-M column name variants (lowercase)
CTRLM_COLUMN_MAP: dict = {
    "job_name":    ["jobname", "job_name", "name", "job"],
    "sub_app":     ["subapplication", "sub_application", "subapp", "sub_app"],
    "start_time":  ["starttime", "start_time", "startdate", "start_date"],
    "end_time":    ["endtime", "end_time", "enddate", "end_date"],
    "status":      ["completionstatus", "completion_status", "status"],
    "runtime_sec": ["runtimesec", "runtime_sec", "run_time_sec", "run_sec"],
}

# ── SOW baseline targets ──────────────────────────────────────────────────────
SOW_DFU:           float = 499_999.0
SOW_SKU:           float = 80_000.0
SOW_ORDERS:        float = 200_000.0
SOW_BATCH_JOBS:    float = 450.0


def reload() -> None:
    """
    Re-read all threshold values from config_store (disk).
    Call this after a Settings save to make the new values live immediately.
    """
    global CPU_WARN, CPU_CRIT, MEM_WARN, MEM_CRIT, DISK_WARN, DISK_CRIT
    global BATCH_FAIL_RATE, ZERO_DUR_FLAG
    global SLA_DAILY_HRS, SLA_WEEKLY_HRS, SLA_BIWEEKLY_HRS, SLA_MONTHLY_HRS, SLA_CUSTOM_HRS, SLA_BUFFER_WARN
    global SLA_ATRISK_PCT, SLA_LONGJOB_PCT
    global BENCH_THRESHOLD_PCT, ANOMALY_Z_THRESHOLD
    global SOW_DFU, SOW_SKU, SOW_ORDERS, SOW_BATCH_JOBS
    global JOB_TYPE_PATTERNS, EXCLUDE_FROM_SLA, ENV_PREFIXES_TO_STRIP, CTRLM_COLUMN_MAP

    CPU_WARN          = float(_cfg("cpu_warning",       75.0))
    CPU_CRIT          = float(_cfg("cpu_critical",      90.0))
    MEM_WARN          = float(_cfg("mem_warning",       70.0))
    MEM_CRIT          = float(_cfg("mem_critical",      80.0))
    DISK_WARN         = float(_cfg("disk_warning",      70.0))
    DISK_CRIT         = float(_cfg("disk_critical",     85.0))
    BATCH_FAIL_RATE   = float(_cfg("batch_fail_rate",   5.0))
    ZERO_DUR_FLAG     = bool (_cfg("zero_dur_flag",     True))
    SLA_DAILY_HRS     = float(_cfg("daily_sla_hrs",     SLA_DEFAULTS["daily"]))
    SLA_WEEKLY_HRS    = float(_cfg("weekly_sla_hrs",    SLA_DEFAULTS["weekly"]))
    SLA_BIWEEKLY_HRS  = float(_cfg("biweekly_sla_hrs",  SLA_DEFAULTS["biweekly"]))
    SLA_MONTHLY_HRS   = float(_cfg("monthly_sla_hrs",   SLA_DEFAULTS["monthly"]))
    SLA_CUSTOM_HRS    = float(_cfg("custom_sla_hrs",    SLA_DEFAULTS["custom"]))
    SLA_BUFFER_WARN   = float(_cfg("sla_buffer_warn",   15.0))
    SLA_ATRISK_PCT    = float(_cfg("sla_atrisk_pct",   15.0))
    SLA_LONGJOB_PCT   = float(_cfg("sla_longjob_pct",  40.0))
    BENCH_THRESHOLD_PCT = float(_cfg("benchmark_threshold", 10.0))
    ANOMALY_Z_THRESHOLD = float(_cfg("anomaly_z_threshold", 2.0))
    SOW_DFU           = float(_cfg("sow_dfu",           499_999.0))
    SOW_SKU           = float(_cfg("sow_sku",           80_000.0))
    SOW_ORDERS        = float(_cfg("sow_orders",        200_000.0))
    SOW_BATCH_JOBS    = float(_cfg("sow_batch_jobs",    450.0))

    _pats  = _cfg("job_type_patterns")
    if isinstance(_pats, dict) and _pats:
        JOB_TYPE_PATTERNS = _pats
    _excl = _cfg("exclude_from_sla")
    if isinstance(_excl, list):
        EXCLUDE_FROM_SLA = _excl
    _env  = _cfg("env_prefixes_to_strip")
    if isinstance(_env, list):
        ENV_PREFIXES_TO_STRIP = _env
    _cmap = _cfg("ctrlm_column_map")
    if isinstance(_cmap, dict) and _cmap:
        CTRLM_COLUMN_MAP = _cmap


def status_label(val: float | None, warn: float, crit: float) -> str:
    """Return 'CRITICAL' | 'WARNING' | 'OK' | 'UNKNOWN'."""
    if val is None:
        return "UNKNOWN"
    if val >= crit:
        return "CRITICAL"
    if val >= warn:
        return "WARNING"
    return "OK"


def format_pct(val: float | None, warn: float, crit: float) -> str:
    """Format a percentage with emoji status prefix."""
    icons = {"CRITICAL": "🔴", "WARNING": "🟡", "OK": "🟢", "UNKNOWN": "–"}
    st = status_label(val, warn, crit)
    if val is None:
        return "–"
    return f"{icons[st]} {val:.1f}%"


# ── Initialise from disk on module import ─────────────────────────────────────
try:
    reload()
except Exception:
    pass  # safe — defaults already set above


# ── Canonical grade table — single source of truth ──────────────────────────
# Every module that maps a numeric score to a letter grade MUST use this.
GRADE_TABLE: list[tuple[float, str, str]] = [
    (90, "A", "APPROVED"),
    (80, "B", "APPROVED WITH NOTES"),
    (70, "C", "CONDITIONAL HOLD"),
    (60, "D", "BLOCKED — MINOR"),
    (0,  "F", "BLOCKED — MAJOR"),
]


def score_to_grade(score: float) -> tuple[str, str]:
    """Map a 0–100 score to (letter, label) using the canonical grade table."""
    for threshold, letter, label in GRADE_TABLE:
        if score >= threshold:
            return letter, label
    return "F", "BLOCKED — MAJOR"
