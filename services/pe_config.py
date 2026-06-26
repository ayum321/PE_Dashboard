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
    "weekly":   8.0,
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

# Action-type SLA defaults for UI benchmark (seconds).
# WATCH triggers when current is within 10% of SLA, BREACH when current > SLA.
# Override via pe_config.json: {"benchmark_action_sla": {"Load": 5, "Export": 15, ...}}
BENCHMARK_ACTION_SLA: dict[str, float] = {
    "Load":            3.0,
    "Export":         10.0,
    "Save":            5.0,
    "Import":         15.0,
    "SRE Process Run": 10.0,
    "Other":           0.0,   # 0 = no SLA for "Other"
}

# ── Anomaly detection ─────────────────────────────────────────────────────────
ANOMALY_Z_THRESHOLD: float = 2.0    # z-score cutoff for statistical outliers

# ── Batch runtime comparison — suspect "near-instant collapse" guard ──────────
# In a PROD-vs-TEST batch runtime file, a job whose runtime collapses from a
# multi-minute baseline to a couple of seconds almost never reflects a genuine
# tuning win — far more often the job processed no data, exited early, or was a
# no-op in the test environment. Counting these as "improvements" (and crediting
# their full runtime as time "saved") inflates the upgrade's apparent benefit.
# These two values bound that guard:
#   • a NEW runtime below BATCH_NOWORK_SEC seconds is treated as "did no work"
#   • only flagged as suspect when the OLD baseline was >= BATCH_COLLAPSE_MIN_OLD_SEC
# Both are tunable via pe_config.json ("batch_nowork_sec", "batch_collapse_min_old_sec").
BATCH_NOWORK_SEC: float          = 5.0    # new runtime < this ⇒ effectively no work
BATCH_COLLAPSE_MIN_OLD_SEC: float = 30.0  # only suspect when baseline did real work (>= this)
BATCH_COLLAPSE_RATIO: float       = 0.05  # new <= old×this (>=95% drop) ⇒ implausible win

# ── Suspect-collapse cause classification (Gap 1) ─────────────────────────────
# A flagged collapse is not equally suspicious across job classes. Data-heavy
# load/extract jobs (history transfers, SKU extracts) collapsing to seconds is
# almost always a TEST-environment data-volume artifact (empty/stub data), not a
# tuning win — the single most important class to call out for a PE reviewer.
# Any job name containing one of these substrings (env-prefix-insensitive,
# matched UPPERCASE) is classified DATA_VOLUME_SUSPECT. Tunable via
# pe_config.json ("batch_data_heavy_patterns").
DEFAULT_BATCH_DATA_HEAVY_PATTERNS: list[str] = [
    "HIST_TRANSFER", "HISTTRANSFER", "EXTRACT_SKUPROJ", "EXTRACT_SKUEXCEPTION",
    "EXTRACT_EXCEPTION", "SKUEXCEPTION", "SKUPROJ", "IO_SRE", "EXTRACT_IO",
    "LOAD_", "_LOAD", "STAGING", "INGEST",
]
BATCH_DATA_HEAVY_PATTERNS: list[str] = list(DEFAULT_BATCH_DATA_HEAVY_PATTERNS)

# ── Release→production SLA projection guard ───────────────────────────────────
# When projecting a benchmark (PROD-vs-TEST) runtime regression onto a matched
# Ctrl-M production job's SLA, a percentage drawn from a tiny baseline is not a
# reliable predictor. A 3s→375s job is +12400%, but multiplying that onto a
# 30-minute production job yields an absurd, misleading "will breach SLA" verdict.
# Only regressions whose baseline did real work (>= this many seconds) are
# statistically credible enough to project. Tunable via "batch_project_min_baseline_sec".
BATCH_PROJECT_MIN_BASELINE_SEC: float = 60.0
# A benchmark job and a Ctrl-M job are only the SAME job if their production-side
# runtimes are in the same ballpark. If the benchmark baseline and the Ctrl-M
# peak differ by more than this factor, the token match is a false positive
# (different jobs that merely share a name prefix) and must not drive a projection.
BATCH_PROJECT_MAX_BASELINE_RATIO: float = 10.0

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

# ── Utility job exclusion rules (generic, signal-based) ──────────────────────
# The rules are split into:
#   - STRONG tokens: name match alone is sufficient
#   - RUNTIME-gated patterns: name match plus runtime threshold is required
DEFAULT_STRONG_UTILITY_TOKENS: frozenset[str] = frozenset({
    "file_watcher", "filewatcher", "ctrl_m_file_watcher",
    "ping_job", "heartbeat", "health_check",
    "export_outbound", "outbound_export",
    "move_file_to_outbox", "outbound_file",
})

DEFAULT_RUNTIME_GATED_UTILITY: dict[str, float] = {
    "_fw": 0.05,
    "fw_": 0.05,
    "gather_db_stats": 0.25,
    "update_stats": 0.25,
    "rebuild_index": 0.25,
    "db_stats": 0.25,
    "delete_type": 0.05,
    "purge_": 0.10,
    "truncate_": 0.05,
    "archive_log": 0.05,
    "batch_start": 0.02,
    "batch_end": 0.02,
    "batchstart": 0.02,
    "batchend": 0.02,
    "pre_batch_node": 0.02,
    "post_batch_node": 0.02,
    "qwbatchstart": 0.02,
    "qwbatchend": 0.02,
    "seq_disable_login": 0.05,
    "seq_enable_users": 0.05,
    "disable_users": 0.05,
    "enable_users": 0.05,
    "disable_login": 0.05,
    "enable_login": 0.05,
    "zabbix_monitors": 0.05,
    "export_": 0.01,
    "_export": 0.01,
    "db_backup": 0.25,
    "db_restore": 0.25,
    "db_cleanup": 0.25,
    "backup": 0.10,
}

# Mutable runtime copies used by the app. Legacy UTILITY_JOB_PATTERNS remains
# as a compatibility alias for any code that still expects a flat list.
STRONG_UTILITY_TOKENS: set[str] = set(DEFAULT_STRONG_UTILITY_TOKENS)
RUNTIME_GATED_UTILITY: dict[str, float] = dict(DEFAULT_RUNTIME_GATED_UTILITY)
UTILITY_JOB_PATTERNS: list[str] = sorted(set(STRONG_UTILITY_TOKENS) | set(RUNTIME_GATED_UTILITY))

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

# ── Schedule types EXCLUDED from window compliance denominator ────────────────
# Used by compliance_engine.compute_window_compliance(). These schedule classes
# either have no daily SLA window (CYCLIC/ADHOC) or run on a non-daily cadence
# that the daily-window compliance metric must not penalize.
# NOTE: UNKNOWN is NOT excluded — an unclassified sub_app defaults to a daily
# batch window and must be counted, else compliance collapses to 0 windows.
COMPLIANCE_EXCLUDED_TYPES: set = {
    "CYCLIC", "CYCLIC_INTERVAL", "ADHOC", "CALENDAR_BASED", "OUTBOUND",
    "PIPELINE_STAGE", "MONTHLY", "BIMONTHLY", "QUARTERLY", "ANNUAL",
}

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
    def _f(key: str, default: float) -> float:
        """Safe float read — returns default when stored value is not numeric."""
        v = _cfg(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    global CPU_WARN, CPU_CRIT, MEM_WARN, MEM_CRIT, DISK_WARN, DISK_CRIT
    global BATCH_FAIL_RATE, ZERO_DUR_FLAG
    global SLA_DAILY_HRS, SLA_WEEKLY_HRS, SLA_BIWEEKLY_HRS, SLA_MONTHLY_HRS, SLA_CUSTOM_HRS, SLA_BUFFER_WARN
    global SLA_ATRISK_PCT, SLA_LONGJOB_PCT
    global BENCH_THRESHOLD_PCT, BENCHMARK_ACTION_SLA, ANOMALY_Z_THRESHOLD
    global BATCH_NOWORK_SEC, BATCH_COLLAPSE_MIN_OLD_SEC, BATCH_COLLAPSE_RATIO
    global BATCH_PROJECT_MIN_BASELINE_SEC, BATCH_PROJECT_MAX_BASELINE_RATIO
    global BATCH_DATA_HEAVY_PATTERNS
    global SOW_DFU, SOW_SKU, SOW_ORDERS, SOW_BATCH_JOBS
    global JOB_TYPE_PATTERNS, EXCLUDE_FROM_SLA, ENV_PREFIXES_TO_STRIP, CTRLM_COLUMN_MAP
    global STRONG_UTILITY_TOKENS, RUNTIME_GATED_UTILITY, UTILITY_JOB_PATTERNS
    global SENTINEL_START_PATTERNS, SENTINEL_END_PATTERNS
    global SENTINEL_MIN_WINDOW_HRS, SENTINEL_MAX_WINDOW_HRS, CYCLIC_MAX_RUNTIME_HRS

    CPU_WARN          = _f("cpu_warning",       75.0)
    CPU_CRIT          = _f("cpu_critical",      90.0)
    MEM_WARN          = _f("mem_warning",       70.0)
    MEM_CRIT          = _f("mem_critical",      80.0)
    DISK_WARN         = _f("disk_warning",      70.0)
    DISK_CRIT         = _f("disk_critical",     85.0)
    BATCH_FAIL_RATE   = _f("batch_fail_rate",   5.0)
    ZERO_DUR_FLAG     = bool (_cfg("zero_dur_flag",     True))
    SLA_DAILY_HRS     = _f("daily_sla_hrs",     SLA_DEFAULTS["daily"])
    SLA_WEEKLY_HRS    = _f("weekly_sla_hrs",    SLA_DEFAULTS["weekly"])
    SLA_BIWEEKLY_HRS  = _f("biweekly_sla_hrs",  SLA_DEFAULTS["biweekly"])
    SLA_MONTHLY_HRS   = _f("monthly_sla_hrs",   SLA_DEFAULTS["monthly"])
    SLA_CUSTOM_HRS    = _f("custom_sla_hrs",    SLA_DEFAULTS["custom"])
    SLA_BUFFER_WARN   = _f("sla_buffer_warn",   15.0)
    SLA_ATRISK_PCT    = _f("sla_atrisk_pct",    15.0)
    SLA_LONGJOB_PCT   = _f("sla_longjob_pct",   40.0)
    BENCH_THRESHOLD_PCT = _f("benchmark_threshold", 10.0)
    _bench_actions = _cfg("benchmark_action_sla")
    if isinstance(_bench_actions, dict) and _bench_actions:
        _bam: dict[str, float] = {}
        for k, v in _bench_actions.items():
            try:
                _bam[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        if _bam:
            BENCHMARK_ACTION_SLA = _bam
    ANOMALY_Z_THRESHOLD = _f("anomaly_z_threshold", 2.0)
    BATCH_NOWORK_SEC           = _f("batch_nowork_sec",            5.0)
    BATCH_COLLAPSE_MIN_OLD_SEC = _f("batch_collapse_min_old_sec", 30.0)
    BATCH_COLLAPSE_RATIO       = _f("batch_collapse_ratio",        0.05)
    BATCH_PROJECT_MIN_BASELINE_SEC   = _f("batch_project_min_baseline_sec",   60.0)
    BATCH_PROJECT_MAX_BASELINE_RATIO = _f("batch_project_max_baseline_ratio", 10.0)
    _dhp = _cfg("batch_data_heavy_patterns")
    if isinstance(_dhp, list) and _dhp:
        BATCH_DATA_HEAVY_PATTERNS = [str(p).upper().strip() for p in _dhp if str(p).strip()]
    else:
        BATCH_DATA_HEAVY_PATTERNS = list(DEFAULT_BATCH_DATA_HEAVY_PATTERNS)
    SOW_DFU           = _f("sow_dfu",           499_999.0)
    SOW_SKU           = _f("sow_sku",           80_000.0)
    SOW_ORDERS        = _f("sow_orders",        200_000.0)
    SOW_BATCH_JOBS    = _f("sow_batch_jobs",    450.0)

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
    STRONG_UTILITY_TOKENS = set(DEFAULT_STRONG_UTILITY_TOKENS)
    RUNTIME_GATED_UTILITY = dict(DEFAULT_RUNTIME_GATED_UTILITY)

    def _norm_token(v: Any) -> str:
        return str(v).strip().lower()

    _strong = _cfg("strong_utility_tokens")
    _runtime = _cfg("runtime_gated_utility")
    _legacy = _cfg("utility_job_patterns")

    if isinstance(_strong, (list, set, tuple)) and _strong:
        STRONG_UTILITY_TOKENS = {_norm_token(v) for v in _strong if str(v).strip()}

    if isinstance(_runtime, dict) and _runtime:
        _rt: dict[str, float] = {}
        for k, v in _runtime.items():
            try:
                _rt[_norm_token(k)] = float(v)
            except (TypeError, ValueError):
                continue
        if _rt:
            RUNTIME_GATED_UTILITY = _rt
    elif isinstance(_legacy, list) and _legacy:
        # Legacy compatibility: treat the flat list as an allowlist over the
        # built-in defaults. Unknown tokens are kept as strong tokens so older
        # persisted overrides still have an effect.
        _legacy_norm = {_norm_token(v) for v in _legacy if str(v).strip()}
        if _legacy_norm:
            STRONG_UTILITY_TOKENS = {
                t for t in STRONG_UTILITY_TOKENS
                if t in _legacy_norm
            } | {
                t for t in _legacy_norm
                if t not in DEFAULT_RUNTIME_GATED_UTILITY and t not in DEFAULT_STRONG_UTILITY_TOKENS
            }
            RUNTIME_GATED_UTILITY = {
                pat: thr for pat, thr in RUNTIME_GATED_UTILITY.items()
                if pat in _legacy_norm
            }

    UTILITY_JOB_PATTERNS = sorted(set(STRONG_UTILITY_TOKENS) | set(RUNTIME_GATED_UTILITY))
    _ss = _cfg("sentinel_start_patterns")
    if isinstance(_ss, list) and _ss:
        SENTINEL_START_PATTERNS = _ss
    _se = _cfg("sentinel_end_patterns")
    if isinstance(_se, list) and _se:
        SENTINEL_END_PATTERNS = _se
    SENTINEL_MIN_WINDOW_HRS = _f("sentinel_min_window_hrs", 0.25)
    SENTINEL_MAX_WINDOW_HRS = _f("sentinel_max_window_hrs", 20.0)
    CYCLIC_MAX_RUNTIME_HRS  = _f("cyclic_max_runtime_hrs", 0.25)


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
