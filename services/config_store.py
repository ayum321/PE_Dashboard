"""
Persistent configuration store for PE Audit Dashboard.

Stores settings (Gemini API key, SLA thresholds, etc.) in a local JSON
file next to the project root. Survives server restarts.

Public API:
    get(key, default=None)   → any
    set(key, value)          → None
    get_all()                → dict
    get_gemini_key()         → str
    set_gemini_key(key: str) → None
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("pe_dashboard.config_store")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / ".pe_config.json"

# Thread lock — prevents concurrent writes from corrupting the JSON file
_lock = threading.Lock()

# Simple read-through cache — invalidated on every write
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_CACHE_TTL: float = 5.0  # seconds — low enough to pick up Settings saves quickly

_DEFAULTS: dict[str, Any] = {
    # AI — key intentionally blank; user must set it via UI Settings,
    # GOOGLE_API_KEY env var, or GEMINI_API_KEY env var.
    "gemini_api_key":       "",
    # NVIDIA NIM key — used as LLM fallback for resource utilization parsing
    # when regex extraction fails on Zabbix / Azure Monitor reports.
    "nvidia_api_key":       "",
    "vision_provider":      "gemini",      # "gemini" | "azure" | "local"
    # Text-LLM routing — used by services.ai_engine. Provider waterfall is
    # always tried (NIM → Gemini), but ai_text_model picks the preferred
    # NIM model. Defaults to Gemma; falls through to Llama on failure.
    "ai_text_provider":     "nvidia",     # "nvidia" | "gemini"
    "ai_text_model":        "openai/gpt-oss-120b",
    "ai_post_upload":       True,         # generate AI summary after each upload
    # SLA
    "daily_sla_hrs":        6.0,
    "weekly_sla_hrs":      17.0,
    "biweekly_sla_hrs":    17.0,
    "monthly_sla_hrs":     17.0,
    "custom_sla_hrs":       6.0,
    "sla_mode":             "daily",       # daily | weekly | biweekly | monthly | custom
    "sla_buffer_warn":      15.0,          # % buffer → AT_RISK
    # CPU / Memory / Disk thresholds
    "cpu_warning":          75.0,
    "cpu_critical":         90.0,
    "mem_warning":          70.0,
    "mem_critical":         80.0,
    "disk_warning":         70.0,
    "disk_critical":        85.0,
    # Batch quality
    "batch_fail_rate":      5.0,
    "zero_dur_flag":        True,
    # UI Benchmark
    "benchmark_threshold":  10.0,          # % degradation to flag RED
    # SLA classification thresholds
    "sla_atrisk_pct":       15.0,
    "sla_longjob_pct":      40.0,
    "anomaly_z_threshold":  2.0,
    # SOW baseline
    "sow_dfu":              499999.0,
    "sow_sku":              80000.0,
    "sow_orders":           200000.0,
    "sow_batch_jobs":       450.0,
    # Azure Monitor connection (personal identity via az login)
    "azure_subscription_id": "",   # Target Azure subscription
    "azure_resource_group":   "",   # Optional: limit to one RG
}


def _load() -> dict:
    """Return the current config, using the in-memory cache when fresh."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            # Merge any keys added in a newer version
            for k, v in _DEFAULTS.items():
                data.setdefault(k, v)
            # Migrate persisted-but-now-retired NIM models
            _DEAD_NIM = {
                "google/gemma-3-27b-it",
                "meta/llama-3.1-70b-instruct",
                "mistralai/mixtral-8x22b-instruct-v0.1",
            }
            if data.get("ai_text_model") in _DEAD_NIM:
                logger.info(
                    "config_store: migrating retired ai_text_model %s -> %s",
                    data["ai_text_model"], _DEFAULTS["ai_text_model"],
                )
                data["ai_text_model"] = _DEFAULTS["ai_text_model"]
            _cache = data
            _cache_ts = now
            return data
        except Exception as exc:
            logger.warning("config_store: failed to read %s — %s", _CONFIG_PATH, exc)

    result = dict(_DEFAULTS)
    _cache = result
    _cache_ts = now
    return result


def _save(data: dict) -> None:
    """Write config to disk and update the in-memory cache atomically."""
    global _cache, _cache_ts
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _cache = data
        _cache_ts = time.monotonic()
    except Exception as exc:
        logger.error("config_store: failed to write %s — %s", _CONFIG_PATH, exc)


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def set(key: str, value: Any) -> None:
    with _lock:
        data = _load()
        data[key] = value
        _save(data)


def get_all() -> dict:
    with _lock:
        return _load()


def get_gemini_key() -> str:
    key = get("gemini_api_key", "")
    # Also fall back to environment variables
    return (
        str(key).strip()
        or os.environ.get("GOOGLE_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
    )


def set_gemini_key(key: str) -> None:
    set("gemini_api_key", key.strip())


def get_nvidia_key() -> str:
    key = get("nvidia_api_key", "")
    return (
        str(key).strip()
        or os.environ.get("NVIDIA_API_KEY", "")
        or os.environ.get("NIM_API_KEY", "")
    )


def set_nvidia_key(key: str) -> None:
    set("nvidia_api_key", key.strip())


# Bootstrap: write defaults if config file doesn't exist
if not _CONFIG_PATH.exists():
    _save(_DEFAULTS)
