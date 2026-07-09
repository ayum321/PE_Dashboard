"""
Config router — persistent dashboard settings.

GET  /api/config          → returns current config (API key masked)
POST /api/config          → update one or more config keys
POST /api/config/test-key → validate a Gemini API key live
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services import config_store

router = APIRouter()


class ConfigPayload(BaseModel):
    gemini_api_key:          Optional[str]   = None
    nvidia_api_key:          Optional[str]   = None
    ai_text_provider:        Optional[str]   = None    # "nvidia" | "gemini"
    ai_text_model:           Optional[str]   = None    # e.g. google/gemma-3-27b-it
    ai_post_upload:          Optional[bool]  = None
    daily_sla_hrs:           Optional[float] = None
    weekly_sla_hrs:          Optional[float] = None
    biweekly_sla_hrs:        Optional[float] = None
    monthly_sla_hrs:         Optional[float] = None
    custom_sla_hrs:          Optional[float] = None
    sla_mode:                Optional[str]   = None
    benchmark_threshold:     Optional[float] = None
    # Azure connection (personal identity via az login)
    azure_subscription_id:   Optional[str]   = None
    azure_resource_group:    Optional[str]   = None
    # Ctrl-M job classification (customer-specific)
    job_type_patterns:       Optional[Dict[str, List[str]]] = None
    exclude_from_sla:        Optional[List[str]]            = None
    env_prefixes_to_strip:   Optional[List[str]]            = None
    ctrlm_column_map:        Optional[Dict[str, List[str]]] = None
    # Per-engagement job exclusions — applied by compute_metrics at analysis time
    exclude_jobs:            Optional[List[str]]            = None


class TestKeyRequest(BaseModel):
    api_key: str


@router.get("/config")
def get_config() -> dict[str, Any]:
    data = config_store.get_all()
    key = data.get("gemini_api_key", "")
    if key and len(key) > 8:
        data["gemini_api_key_masked"] = key[:6] + "••••" + key[-4:]
    else:
        data["gemini_api_key_masked"] = "(not set)"

    nvkey = data.get("nvidia_api_key", "")
    if nvkey and len(nvkey) > 12:
        data["nvidia_api_key_masked"] = nvkey[:8] + "••••" + nvkey[-4:]
    else:
        data["nvidia_api_key_masked"] = "(not set)"

    # Expose the canonical buffer-band thresholds (single source: pe_config) so the
    # frontend gauge, daily-bar colouring, legends and the batch narrative all share
    # the SAME green/amber/red semantics instead of hardcoding 15/40 in three places.
    try:
        from services import pe_config
        data.setdefault("sla_atrisk_pct",  float(pe_config.SLA_ATRISK_PCT))
        data.setdefault("sla_longjob_pct", float(pe_config.SLA_LONGJOB_PCT))
        # AI routers (/api/ai-status, /api/ai/*) are only mounted when the
        # kill-switch is on (main.py). Expose the flag so the frontend can
        # skip calling those endpoints entirely instead of hitting a
        # guaranteed 404 on every page load when AI is disabled.
        data["ai_enabled"] = bool(pe_config.AI_ENABLED)
    except Exception:
        data.setdefault("ai_enabled", False)
    return data


@router.post("/config")
def update_config(payload: ConfigPayload) -> dict[str, Any]:
    updates = payload.model_dump(exclude_none=True)
    for k, v in updates.items():
        config_store.set(k, v)
    return {"status": "ok", "updated": list(updates.keys())}


@router.post("/config/test-key")
def test_gemini_key(body: TestKeyRequest) -> dict[str, Any]:
    """Quick liveness check for a Gemini API key."""
    key = body.api_key.strip()
    if not key:
        return {"valid": False, "error": "Empty key"}
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        models = list(genai.list_models())
        flash = [m.name for m in models if "flash" in m.name.lower()]
        return {"valid": True, "model_count": len(models),
                "recommended": flash[0] if flash else (models[0].name if models else "")}
    except ImportError:
        return {"valid": False, "error": "google-generativeai not installed"}
    except Exception as e:
        err = str(e)
        if "API_KEY" in err.upper() or "INVALID" in err.upper():
            return {"valid": False, "error": "Invalid API key"}
        return {"valid": False, "error": str(e)[:120]}


@router.post("/config/test-nvidia-key")
def test_nvidia_key(body: TestKeyRequest) -> dict[str, Any]:
    """Quick liveness check for an NVIDIA NIM API key."""
    from services.nvidia_llm import test_api_key
    return test_api_key(body.api_key)


class CtrlmProfilePayload(BaseModel):
    """Customer-supplied Ctrl-M classification profile (the config JSON from the SOW wiring diagram)."""
    customer_id:          Optional[str]                    = None
    sla_windows:          Optional[Dict[str, Any]]         = None   # {"DAILY": {"limit_hours": 6.0}, ...}
    job_type_patterns:    Optional[Dict[str, List[str]]]   = None
    ctrlm_column_map:     Optional[Dict[str, List[str]]]   = None
    exclude_from_sla:     Optional[List[str]]              = None
    env_prefixes_to_strip: Optional[List[str]]             = None


@router.post("/config/ctrlm-profile")
def apply_ctrlm_profile(payload: CtrlmProfilePayload) -> dict[str, Any]:
    """
    Apply a customer Ctrl-M classification profile in one shot.

    Accepts the JSON structure from the SOW integration wiring diagram:
      job_type_patterns, ctrlm_column_map, exclude_from_sla, env_prefixes_to_strip.

    Also merges sla_windows into _sow_sla_windows (Tier 2 SOW ceiling) so they
    participate immediately in the 3-tier SLA resolution.
    """
    from services import pe_config

    saved: list[str] = []

    data = payload.model_dump(exclude_none=True)

    # Persist each field to config_store
    for key in ("job_type_patterns", "ctrlm_column_map", "exclude_from_sla", "env_prefixes_to_strip"):
        if key in data:
            config_store.set(key, data[key])
            saved.append(key)

    # Merge sla_windows into _sow_sla_windows (adds SOURCE tag)
    if "sla_windows" in data:
        existing = config_store.get("_sow_sla_windows") or {}
        for btype, entry in data["sla_windows"].items():
            if isinstance(entry, dict):
                existing[btype.upper()] = {**entry, "source": "SOW_EXTRACTED"}
            else:
                existing[btype.upper()] = {"limit_hours": float(entry), "source": "SOW_EXTRACTED"}
        config_store.set("_sow_sla_windows", existing)
        saved.append("sla_windows → _sow_sla_windows")

    # Hot-reload pe_config so the new values take effect immediately
    pe_config.reload()

    return {"status": "ok", "saved": saved, "customer_id": data.get("customer_id")}


# ── Session management ────────────────────────────────────────────────────────

class ClearSessionRequest(BaseModel):
    slots: Optional[List[str]] = None   # if None → clear ALL audit slots + plain keys


@router.post("/clear-session")
def clear_session(body: ClearSessionRequest = ClearSessionRequest()) -> dict:
    """Wipe session data so the next engagement starts fresh.

    With no body → clears everything (audit context + last_* cache + .pe_cache.json).
    With {"slots": ["sow_contract", "volume_vs_sow"]} → clears only those slots.
    This prevents a previous customer's SOW/findings/resource data bleeding into a
    new engagement when the server has not been restarted.
    """
    from services import session_cache

    if body.slots:
        # Selective clear — remove only the requested audit slots
        for slot in body.slots:
            session_cache.ac_del(slot)
        # Also remove matching plain keys (last_sow_compare etc.)
        _plain_map = {
            "sow_contract":   ["last_sow_compare"],
            "resource_summary": ["last_resource"],
            "smart_findings": ["last_smart_findings"],
            "batch_kpis":     ["last_batch"],
            "sla_matrix_kpis": ["last_sla_matrix"],
        }
        for slot in body.slots:
            for plain_key in _plain_map.get(slot, []):
                session_cache.set(plain_key, None)
        return {"status": "ok", "cleared": body.slots}
    else:
        # Full wipe — new engagement
        session_cache.clear()
        # Also wipe all SOW-related keys + customer identity from config_store
        # (these persist in .pe_config.json independently of session_cache)
        from services import config_store as _cs
        _cs.set("customer_name",       "")
        _cs.set("sow_baseline",       {})
        _cs.set("_sow_sla_windows",   {})
        _cs.set("_sow_volume_by_year",{})
        _cs.set("_sow_contract_meta", {})
        # Reset SLA ceilings to defaults to prevent previous customer's
        # SLA matrix from bleeding into the next engagement
        from services import pe_config
        _cs.set("daily_sla_hrs",    pe_config.SLA_DEFAULTS["daily"])
        _cs.set("weekly_sla_hrs",   pe_config.SLA_DEFAULTS["weekly"])
        _cs.set("biweekly_sla_hrs", pe_config.SLA_DEFAULTS["biweekly"])
        _cs.set("monthly_sla_hrs",  pe_config.SLA_DEFAULTS["monthly"])
        _cs.set("custom_sla_hrs",   pe_config.SLA_DEFAULTS["custom"])
        _cs.set("_sla_intelligence", {})
        _cs.set("_sla_source_type",  "")
        _cs.set("_batch_sla_xlsx",   {})
        # Reload pe_config from defaults
        pe_config.reload()
        return {"status": "ok", "cleared": "all"}
