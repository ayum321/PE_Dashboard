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

# ── Utility job patterns (auto-tag for exclusion from SLA analysis) ──────────
# Normalized job name (lowercase, spaces+hyphens→_) is substring-matched.
# "Ctrl-M File Watcher_W" normalises to "ctrl_m_file_watcher_w" → matches "file_watcher".
# Configurable via config_store["utility_job_patterns"]. No customer names here.
UTILITY_JOB_PATTERNS: list = [
    # FileWatcher (Ctrl-M native utility — marks data arrival, not batch logic)
    "file_watcher", "filewatcher", "ctrl_m_file_watcher",
    # DB maintenance (backup, restore, index, stats)
    "db_backup", "database_backup", "dbbackup", "db_maint", "db_maintenance",
    "db_restore", "dbcleanup", "db_cleanup", "purge_db", "truncate_log",
    "archive_log", "archive_logs", "db_stats", "update_stats", "rebuild_index",
    "index_rebuild", "shrink_db",
    # Export / outbound file delivery (Ctrl-M job that pushes a file, not batch calc)
    "sftp_export", "sftp_send", "ftp_export", "outbound_file",
    # Health check / monitoring heartbeat (when combined with high frequency → cyclic)
    "health_check", "ping_job", "heartbeat",
]

# ── Sentinel detection patterns (Stage 4 Level 2 fallback) ───────────────────
# Normalized job name substrings indicating batch WINDOW START (first job) or
# WINDOW END (last job). Used when XLSX sentinel pair is not configured.
# Add customer-specific patterns to config_store["sentinel_start_patterns"].
SENTINEL_START_PATTERNS: list = [
    # Batch open / user disable sequences — broad-to-specific order
    "scpo_batch_start", "batch_start_dummy", "batch_start",
    "jbi000_disableusers", "jbi000_disable",
    "zabbix_monitors_disable", "zabbix_disable",
    "seq_disable_login", "seq_batch_start",
    "on_dp_disable_users", "on_sp_disable_triggers",
    "disable_ref_constraints", "disable_scpomgr_triggers",
    "itp_disable_login", "io_disable_login",
    "disable_users", "disable_login", "disable_monitors",
    "batch_open", "start_batch", "batch_init",
    # FileWatcher in normalized form — Ctrl-M File Watcher_W → ctrl_m_file_watcher_w
    "ctrl_m_file_watcher",
]
SENTINEL_END_PATTERNS: list = [
    # Batch close / user enable sequences — broad-to-specific order
    "scpo_enable_users", "scpo_batch_end", "batch_end",
    "jbi000_enableusers", "jbi000_enable",
    "zabbix_monitors_enable", "zabbix_enable",
    "seq_enable_users", "seq_batch_end",
    "on_dp_enable_users", "on_sp_enable_users",
    "enable_ref_constraints", "enable_scpomgr_triggers",
    "enable_users", "enable_login", "enable_monitors",
    "batch_close", "end_batch", "batch_complete",
]

# ── Sentinel window validity bounds (hours)
SENTINEL_MIN_WINDOW_HRS: float = 0.25   # < 15 min → SUSPECT_TOO_SHORT
SENTINEL_MAX_WINDOW_HRS: float = 20.0   # > 20h   → SUSPECT_TOO_LONG (warn, keep)

# ── Cyclic detection threshold ────────────────────────────────────────────────
# Jobs with avg_runtime_hrs below this threshold are considered cyclic candidates.
# (Combined with frequency guard: max runs/day > 5 AND median > 3)
CYCLIC_MAX_RUNTIME_HRS: float = 0.25   # < 15 minutes = polling/heartbeat, not batch

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
    global UTILITY_JOB_PATTERNS, SENTINEL_START_PATTERNS, SENTINEL_END_PATTERNS
    global SENTINEL_MIN_WINDOW_HRS, SENTINEL_MAX_WINDOW_HRS, CYCLIC_MAX_RUNTIME_HRS

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
    _util = _cfg("utility_job_patterns")
    if isinstance(_util, list) and _util:
        UTILITY_JOB_PATTERNS = _util
    _ss = _cfg("sentinel_start_patterns")
    if isinstance(_ss, list) and _ss:
        SENTINEL_START_PATTERNS = _ss
    _se = _cfg("sentinel_end_patterns")
    if isinstance(_se, list) and _se:
        SENTINEL_END_PATTERNS = _se
    SENTINEL_MIN_WINDOW_HRS = float(_cfg("sentinel_min_window_hrs", 0.25))
    SENTINEL_MAX_WINDOW_HRS = float(_cfg("sentinel_max_window_hrs", 20.0))
    CYCLIC_MAX_RUNTIME_HRS  = float(_cfg("cyclic_max_runtime_hrs", 0.25))


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
