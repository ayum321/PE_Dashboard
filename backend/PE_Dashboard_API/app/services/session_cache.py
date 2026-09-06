"""
session_cache — in-memory, process-local cache of the last-uploaded payloads
so cross-pillar engines (SLA matrix adaptive baselines, correlation, etc.)
can reach for them without an extra HTTP round-trip.

Data is also persisted to .pe_cache.json so a server restart (--reload)
does not lose the last batch/resource/SLA payloads that were uploaded.

Public API:
    set(key, value) -> None
    get(key, default=None) -> any
    clear() -> None

    # Structured audit context (single source of truth for all screens)
    ac_set(slot, value) -> None        # write one slot of audit_context
    ac_get(slot, default=None) -> any  # read one slot
    ac_snapshot() -> dict              # full snapshot (all slots + timestamps)
    ac_clear() -> None                 # wipe audit context

Audit context slots (audit_context keys):
    job_runs_df         : list[dict]  – raw classified job runs (E2 output)
    job_summary         : list[dict]  – per-job rollup (peak/avg/buffer/sla)
    daily_window_series : list[dict]  – daily window rollup (E3 output, was: workflow_rollup)
    workflow_sla_summary: list[dict]  – per-workflow SLA resolution (was: resolved_workflow_df)
    sla_resolved        : list[dict]  – per-run SLA resolution result (E1)
    resource_summary    : dict        – resource KPIs + server list (E4)
    volume_vs_sow       : dict        – DFU/SKU actuals vs SOW (E5)
    regression_df       : list[dict]  – statistical outliers / z-score (E6)
    adaptive_sla        : list[dict]  – P95-based per-job SLA suggestions (E7)
    sow_contract        : dict        – parsed SOW contract meta + SLA windows
    uat_df              : list[dict]  – UAT test cases (F)
    batch_kpis          : dict        – headline KPIs from batch processing
    sla_matrix_kpis     : dict        – headline KPIs from SLA matrix
    customer_name       : str         – resolved customer name
    sla_detected_mode   : str         – auto-detected schedule type (DAILY/WEEKLY/MONTHLY)
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {}

# ── Persistence ──────────────────────────────────────────────────────────────
# Keys saved across server restarts.  We exclude large raw data frames.
# last_sow_compare is intentionally excluded — it is engagement-specific
# (linked to the SOW contract) and must not survive a server restart.
_PERSIST_PLAIN_KEYS = {
    "last_batch", "last_resource", "last_sla_matrix",
    "last_red_flags", "last_smart_findings", "last_findings",
    "last_benchmark", "last_benchmark_ui", "last_benchmark_batch",
}
# SOW / engagement-identity slots are intentionally EXCLUDED from this set.
# They must be re-uploaded for each engagement and must never bleed across
# customer sessions via the .pe_cache.json file.
_PERSIST_AC_SLOTS = {
    "batch_kpis", "job_summary", "batch_top_jobs", "sla_job_summary",
    "daily_window_series", "workflow_sla_summary", "sla_detected_mode",
    "sla_resolved",
    "resource_summary", "regression_df", "adaptive_sla", "sla_matrix_kpis",
} - {"customer_name"}
from services.state_paths import get_state_file

_CACHE_FILE: Path = get_state_file(".pe_cache.json")


def _cache_path() -> Path:
    return _CACHE_FILE

# These must be defined before _flush() and _load_from_disk() use them
_AC_KEY    = "__audit_context__"
_AC_TS_KEY = "__audit_context_ts__"


def _flush() -> None:
    """Write persisted keys to disk — snapshot taken while lock is held,
    file I/O happens OUTSIDE the lock to avoid blocking callers."""
    try:
        ctx = _state.get(_AC_KEY, {})
        ts  = _state.get(_AC_TS_KEY, {})
        snapshot = {
            "__plain__": {k: _state[k] for k in _PERSIST_PLAIN_KEYS if k in _state},
            "__ac__":    {k: ctx[k]    for k in _PERSIST_AC_SLOTS   if k in ctx},
            "__ac_ts__": {k: ts[k]     for k in _PERSIST_AC_SLOTS   if k in ts},
        }
    except Exception:
        return
    # Deep-copy the snapshot so we can safely release the lock
    snapshot = copy.deepcopy(snapshot)

    def _write():
        try:
            cache_file = _cache_path()
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, default=str)
        except Exception:
            pass  # never crash the caller due to disk I/O failure

    # Write synchronously so the cache file is consistent before any reload.
    # The .pe_cache.json is small (< 1 MB) so the I/O cost is negligible,
    # and this prevents the race condition where a page-reload reads stale data
    # because the background thread hadn't flushed yet.
    _write()


def _load_from_disk() -> None:
    """Load persisted snapshot on module init."""
    try:
        cache_file = _cache_path()
        if not os.path.exists(cache_file):
            return
        with open(cache_file, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        for k, v in snap.get("__plain__", {}).items():
            # Only restore keys still in the persist set — prevents removed
            # keys (e.g. last_sow_compare) from leaking back via old cache files.
            if k in _PERSIST_PLAIN_KEYS:
                _state[k] = v
        ctx = _state.setdefault(_AC_KEY, {})
        for k, v in snap.get("__ac__", {}).items():
            # Only restore slots that are still in the persist set.
            # This prevents removed / SOW slots from leaking back in.
            if k in _PERSIST_AC_SLOTS:
                ctx[k] = v
        # ── Backward-compat migration: rename old slot keys ──────────────────
        # workflow_rollup      → daily_window_series
        # resolved_workflow_df → workflow_sla_summary
        if "workflow_rollup" in ctx and "daily_window_series" not in ctx:
            ctx["daily_window_series"] = ctx.pop("workflow_rollup")
        elif "workflow_rollup" in ctx:
            ctx.pop("workflow_rollup", None)   # remove stale duplicate
        if "resolved_workflow_df" in ctx and "workflow_sla_summary" not in ctx:
            ctx["workflow_sla_summary"] = ctx.pop("resolved_workflow_df")
        elif "resolved_workflow_df" in ctx:
            ctx.pop("resolved_workflow_df", None)   # remove stale duplicate
        ts_map = _state.setdefault(_AC_TS_KEY, {})
        for k, v in snap.get("__ac_ts__", {}).items():
            ts_map[k] = v
    except Exception:
        pass


# NOTE: Do NOT auto-load from disk at import time.
# The lifespan handler in main.py calls clear() on every startup to ensure
# a clean dashboard.  If we loaded here, there would be a race window where
# stale data is served before clear() runs.
# Callers that need explicit restore (e.g. --reload dev mode) can call
# _load_from_disk() manually after startup.

# ── Structured audit context ─────────────────────────────────────────────────
# One shared dict; each engine writes to its own slot.
# Timestamps track when each slot was last written so the UI can show staleness.
# NOTE: _AC_KEY / _AC_TS_KEY are defined above (before _flush / _load_from_disk)

_AC_SLOTS = {
    "job_runs_df", "job_summary", "batch_top_jobs", "sla_job_summary",
    "daily_window_series", "workflow_sla_summary", "sla_detected_mode",
    "sla_resolved",
    "resource_summary", "volume_vs_sow", "regression_df", "adaptive_sla",
    "sow_contract", "uat_df", "batch_kpis", "sla_matrix_kpis", "customer_name",
}


def ac_set(slot: str, value: Any) -> None:
    """Write one slot of the shared audit context. Thread-safe."""
    with _lock:
        ctx = _state.setdefault(_AC_KEY, {})
        ctx[slot] = value
        ts = _state.setdefault(_AC_TS_KEY, {})
        ts[slot] = time.time()
        if slot in _PERSIST_AC_SLOTS:
            _flush()


def ac_get(slot: str, default: Any = None) -> Any:
    """Read one slot of the shared audit context. Returns deep copy."""
    with _lock:
        val = _state.get(_AC_KEY, {}).get(slot, default)
    if isinstance(val, (dict, list)):
        return copy.deepcopy(val)
    return val


def ac_snapshot() -> dict:
    """Return a full snapshot: {slot: value, ...} plus a '_timestamps' meta key.
    Returns a deep copy — callers may mutate freely without corrupting cache."""
    with _lock:
        ctx = copy.deepcopy(_state.get(_AC_KEY, {}))
        ts  = copy.deepcopy(_state.get(_AC_TS_KEY, {}))
    ctx["_timestamps"] = ts
    return ctx


def ac_clear() -> None:
    """Wipe the entire audit context (called on fresh upload of core data)."""
    with _lock:
        _state.pop(_AC_KEY,    None)
        _state.pop(_AC_TS_KEY, None)


def ac_del(slot: str) -> None:
    """Delete a single slot from the audit context and from the persisted file."""
    with _lock:
        _state.get(_AC_KEY, {}).pop(slot, None)
        _state.get(_AC_TS_KEY, {}).pop(slot, None)
    _flush()


# ── Original key/value API ───────────────────────────────────────────────────

def set(key: str, value: Any) -> None:  # noqa: A001 - mirror dict-style API
    with _lock:
        _state[key] = value
        if key in _PERSIST_PLAIN_KEYS:
            _flush()


def get(key: str, default: Any = None) -> Any:
    """Return a deep copy of dicts/lists to prevent cross-thread mutation."""
    with _lock:
        val = _state.get(key, default)
    if isinstance(val, (dict, list)):
        return copy.deepcopy(val)
    return val


def clear() -> None:
    with _lock:
        _state.clear()
        # Also remove the persisted file so a deliberate reset is honoured
        try:
            if os.path.exists(_CACHE_FILE):
                os.remove(_CACHE_FILE)
        except Exception:
            pass


def ensure_customer(new_customer: Optional[str]) -> bool:
    """Ensure the session cache belongs to the given customer.

    If new_customer is supplied and differs from the active customer_name,
    all stale customer payloads (batch, resource, SLA, SOW, findings) are
    purged from the session cache and audit context so cross-customer stale
    data never leaks into the newly uploaded customer's views.

    Returns True if a customer switch occurred, False otherwise.
    """
    if not new_customer or not str(new_customer).strip():
        return False

    clean_new = str(new_customer).strip()
    try:
        from services.customer_identity import is_valid_customer_name
        if not is_valid_customer_name(clean_new):
            return False
    except Exception:
        pass

    with _lock:
        ctx = _state.get(_AC_KEY, {})
        current = ctx.get("customer_name")
        if not current:
            try:
                from services import config_store
                current = config_store.get("customer_name")
            except Exception:
                current = None

        try:
            from services.customer_identity import is_valid_customer_name
            if current and not is_valid_customer_name(current):
                current = None
                ctx.pop("customer_name", None)
                try:
                    from services import config_store
                    config_store.set("customer_name", "")
                except Exception:
                    pass
        except Exception:
            pass

        import re
        norm_curr = re.sub(r'[^a-zA-Z0-9]', '', str(current or '')).lower()
        norm_new  = re.sub(r'[^a-zA-Z0-9]', '', clean_new).lower()

        if norm_curr and norm_new and norm_curr != norm_new:
            import logging
            logging.getLogger("pe_dashboard.session").info(
                "Customer switch detected: '%s' -> '%s'. Purging stale session cache.",
                current, clean_new,
            )
            # Purge all old customer payloads
            for k in list(_state.keys()):
                if k != _AC_KEY and k != _AC_TS_KEY:
                    _state.pop(k, None)

            # Wipe audit context
            _state.pop(_AC_KEY, None)
            _state.pop(_AC_TS_KEY, None)

            # Re-initialize audit context with new customer
            new_ctx = _state.setdefault(_AC_KEY, {})
            new_ctx["customer_name"] = clean_new
            new_ts = _state.setdefault(_AC_TS_KEY, {})
            new_ts["customer_name"] = time.time()

            try:
                from services import config_store
                config_store.set("customer_name", clean_new)
                config_store.set("sow_baseline", {})
                config_store.set("_sow_sla_windows", {})
                config_store.set("_sow_volume_by_year", {})
                config_store.set("_sow_contract_meta", {})
                config_store.set("_batch_sla_xlsx", {})
            except Exception:
                pass

            _flush()
            return True
        else:
            # Same customer or first customer: record customer_name
            ctx = _state.setdefault(_AC_KEY, {})
            ctx["customer_name"] = clean_new
            ts = _state.setdefault(_AC_TS_KEY, {})
            ts["customer_name"] = time.time()
            try:
                from services import config_store
                config_store.set("customer_name", clean_new)
            except Exception:
                pass
            _flush()
            return False

