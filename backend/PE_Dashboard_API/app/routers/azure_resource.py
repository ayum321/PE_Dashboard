"""
Azure resource router.

POST /api/azure/fetch-resources
    body: { hours_back?: int, resource_group?: str }
    response: same shape as /api/process-resource
              { kpis, anomalies, servers, executive_summary }

GET /api/azure/status
    Returns whether Azure connection is configured (subscription ID set).

GET /api/azure/whoami
    Returns the current session's browser-authenticated Azure AD identity.

POST /api/azure/validate
    Validates Azure connection by attempting a lightweight API call using
    the current session's browser credential.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services import config_store
from services.azure_monitor import (
    AzureConfigError,
    AzureFetchError,
    AzureNetworkError,
    AzureTimeoutError,
    _build_credential,
    browser_login,
    clear_browser_credential,
    clear_vm_inventory_cache,
    detect_regime_change,
    discover_vms,
    fetch_vm_metrics,
    fetch_vm_timeseries,
    get_browser_credential,
    get_browser_credential_info,
    get_vm_prewarm_state,
    prewarm_vm_inventory,
    search_vms_with_fallback,
)
from services.resource_calculator import build_resource_payload
from services import baseline_store
from services import pe_config
from services.spike_attribution import attribute_spikes
from services.customer_identity import is_valid_customer_name
router = APIRouter()


def _snapshot_observation_window(hours_back: int, end_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Describe the rolling Azure window used by the snapshot aggregates.

    This is deliberately labelled as the requested window: Azure can omit
    buckets and the five-minute metric cache may return a nearly-identical
    earlier pull. The UI can show useful dates without overstating coverage.
    """
    end = end_utc or datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)
    grain_hours = 1 if hours_back <= 72 else 6 if hours_back <= 720 else 12
    return {
        "basis": "requested_rolling_window",
        "requested_hours": hours_back,
        "start_utc": start.isoformat().replace("+00:00", "Z"),
        "end_utc": end.isoformat().replace("+00:00", "Z"),
        "snapshot_grain_hours": grain_hours,
        "cache_ttl_seconds": 300,
        "definitions": {
            "avg": "Arithmetic mean of valid Azure Monitor buckets in the requested window; missing buckets are excluded.",
            "peak": "Highest Azure Maximum bucket observed in the requested window; role status is governed by this value.",
            "current": "Most recent returned bucket, falling back to the period average when Azure supplies no recent bucket.",
        },
    }


def _baseline_ns(resource_id: str, vm_name: str) -> tuple[str, str]:
    """Customer + VM namespace for the baseline store, parsed from the ARM
    resource id (/subscriptions/<sub>/.../virtualMachines/<vm>). Falls back to
    'default' when the subscription segment is absent so the store never breaks."""
    cust, vm = "default", vm_name
    parts = (resource_id or "").split("/")
    for i, seg in enumerate(parts):
        low = seg.lower()
        if low == "subscriptions" and i + 1 < len(parts):
            cust = parts[i + 1]
        elif low == "virtualmachines" and i + 1 < len(parts):
            vm = parts[i + 1]
    return cust, vm

# ── Per-session identity ──────────────────────────────────────────────────────
# Azure credentials are scoped per browser session (see azure_monitor.py). The
# session id rides in a first-party HttpOnly cookie so concurrent analysts on one
# server process never share or overwrite each other's Azure identity/token.
_PE_SID_COOKIE = "pe_sid"
_PE_SID_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _secure_cookie() -> bool:
    """Enable the Secure flag for HTTPS-proxied production deployments.

    Local FastAPI/React development remains HTTP unless explicitly opted in.
    """
    val = os.environ.get("PE_COOKIE_SECURE", "").strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return os.path.exists("/app") or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def _cookie_samesite() -> str:
    """Return a safe, deployment-configurable SameSite policy for pe_sid.

    Local development uses ``lax`` with one canonical localhost hostname.
    A portal that hosts the MFE and API on different HTTPS sites must opt into
    ``PE_COOKIE_SECURE=true`` and ``PE_COOKIE_SAMESITE=none``; browsers reject
    SameSite=None cookies without Secure.
    """
    value = os.environ.get("PE_COOKIE_SAMESITE", "").strip().lower()
    if value in {"lax", "strict", "none"}:
        return value if value != "none" or _secure_cookie() else "lax"
    return "none" if _secure_cookie() else "lax"


def _session_id(request: Request, response: Optional[Response] = None) -> str:
    """Read the caller's session id from the pe_sid cookie, minting one if absent.
    When a Response is supplied the cookie is (re)set so the same browser reuses
    the same id across requests and restarts."""
    sid = (request.cookies.get(_PE_SID_COOKIE) or "").strip()
    if not sid:
        sid = uuid.uuid4().hex
    if response is not None:
        response.set_cookie(
            _PE_SID_COOKIE, sid,
            max_age=_PE_SID_MAX_AGE, httponly=True, samesite=_cookie_samesite(), path="/",
            secure=_secure_cookie(),
        )
    return sid

# ── Timeseries result cache ──────────────────────────────────────────────────
# Caches the full processed response for (vm_ids, window) combinations so that
# spike drill-down clicks don't re-fetch from Azure Monitor on every click.
# TTL = 5 minutes. Cache is keyed by a SHA256 of the canonical request params.
_TS_CACHE: Dict[str, Dict[str, Any]] = {}
_TS_CACHE_LOCK = threading.Lock()
_TS_CACHE_TTL = 300  # seconds
_TS_CACHE_MAX = 64   # hard cap on retained result sets


def _ts_cache_key(session_id: str, vm_ids: List[str], hours_back: int,
                  start_utc: Optional[str], end_utc: Optional[str],
                  vm_types: Optional[Dict[str, str]] = None) -> str:
    # SECURITY: the session id is part of the key. Azure credentials are
    # per-session (see _build_credential), so a key built only from request
    # params let a second session read the first session's cached Azure metrics
    # without ever presenting a credential.
    canonical_roles = {
        str(resource_id).strip().lower(): str(role).strip().upper()
        for resource_id, role in (vm_types or {}).items()
        if resource_id and role
    }
    canonical = json.dumps(
        {"sid": session_id, "ids": sorted(vm_ids), "h": hours_back, "s": start_utc, "e": end_utc, "roles": canonical_roles},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _ts_cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _TS_CACHE_LOCK:
        entry = _TS_CACHE.get(key)
        if entry and time.time() - entry["ts"] < _TS_CACHE_TTL:
            return entry["data"]
        if entry:
            del _TS_CACHE[key]
    return None


def _ts_cache_set(key: str, data: Dict[str, Any]) -> None:
    with _TS_CACHE_LOCK:
        # Purge expired entries on write — entries were previously only evicted
        # on read, so keys never read again grew the dict without bound.
        _now = time.time()
        for _k in [k for k, v in _TS_CACHE.items() if _now - v["ts"] >= _TS_CACHE_TTL]:
            del _TS_CACHE[_k]
        if len(_TS_CACHE) >= _TS_CACHE_MAX:
            oldest = min(_TS_CACHE, key=lambda k: _TS_CACHE[k]["ts"])
            del _TS_CACHE[oldest]
        _TS_CACHE[key] = {"ts": _now, "data": data}

# ── Subscription list cache ──────────────────────────────────────────────────
# Populated per browser session in the background; served instantly to that
# same session on subsequent calls.
_sub_cache: Dict[str, Dict[str, Any]] = {}
_sub_cache_lock = threading.Lock()
_SUB_CACHE_TTL = 600  # 10 minutes

# Keep an interactive browser login single-flight per analyst session.  The
# Azure SDK already serializes it internally, but rejecting a duplicate HTTP
# request immediately is important: otherwise a double click can wait behind
# the first account-picker flow for up to the browser-auth timeout.
_login_request_sessions: set[str] = set()
_login_request_lock = threading.Lock()


def _sub_cache_entry(session_id=None) -> Dict[str, Any]:
    """Return the current session's subscription-cache entry while locked."""
    return _sub_cache.setdefault(_session_cache_key(session_id), {
        "subs": None, "ts": 0.0, "fetching": False,
    })


def _session_cache_key(session_id=None) -> str:
    return (session_id or "").strip() or "_default"


def _claim_login_request(session_id=None) -> bool:
    key = _session_cache_key(session_id)
    with _login_request_lock:
        if key in _login_request_sessions:
            return False
        _login_request_sessions.add(key)
        return True


def _release_login_request(session_id=None) -> None:
    with _login_request_lock:
        _login_request_sessions.discard(_session_cache_key(session_id))


def _reset_sub_cache(session_id=None) -> None:
    """Wipe the subscription cache — called on login/logout so a different
    user never sees the previous user's subscription list."""
    with _sub_cache_lock:
        _sub_cache.pop(_session_cache_key(session_id), None)
    # Also drop cached VM inventory so a different user never sees stale VMs
    try:
        clear_vm_inventory_cache(session_id)
    except Exception:
        pass


def _populate_sub_cache(session_id=None) -> None:
    """Background worker: fetch all subscriptions and store in _sub_cache.

    Uses the caller's browser credential, never a machine-global identity.
    """
    rows: list[Dict[str, Any]] = []

    # ── 1. Browser credential via SDK (preferred — guaranteed fresh after login)
    try:
        _bc = get_browser_credential(session_id)
        if _bc is not None:
            from azure.mgmt.subscription import SubscriptionClient
            client = SubscriptionClient(_bc)
            for sub in client.subscriptions.list():
                state = str(getattr(sub, "state", "") or "")
                if state.lower() != "enabled":
                    continue
                rows.append({
                    "id":        str(getattr(sub, "subscription_id", "") or ""),
                    "name":      str(getattr(sub, "display_name", "") or ""),
                    "state":     state,
                    "is_default": False,
                    "tenant_id": str(getattr(sub, "tenant_id", "") or ""),
                })
    except Exception:
        pass

    try:
        from services.azure_monitor import get_known_catalog
        cat_subs, _ = get_known_catalog("")
        seen_ids = {r["id"].lower() for r in rows}
        for cs in cat_subs:
            if cs["id"].lower() not in seen_ids:
                rows.append(cs)
                seen_ids.add(cs["id"].lower())
    except Exception:
        pass

    with _sub_cache_lock:
        entry = _sub_cache_entry(session_id)
        entry["subs"] = rows
        entry["ts"] = time.time()
        entry["fetching"] = False
        entry["completed"] = True


def _subscriptions_via_sdk(session_id=None) -> list[Dict[str, Any]]:
    """Subscription discovery using the caller's browser credential."""
    from azure.mgmt.subscription import SubscriptionClient

    cred = _build_credential({}, session_id)
    client = SubscriptionClient(cred)
    rows: list[Dict[str, Any]] = []
    for sub in client.subscriptions.list():
        state = str(getattr(sub, "state", "") or "")
        if state.lower() != "enabled":
            continue
        rows.append(
            {
                "id": str(getattr(sub, "subscription_id", "") or ""),
                "name": str(getattr(sub, "display_name", "") or ""),
                "state": state,
                "is_default": False,
                "tenant_id": str(getattr(sub, "tenant_id", "") or ""),
            }
        )
    return rows


def _resource_groups_via_sdk(subscription_id: str, session_id=None) -> list[Dict[str, Any]]:
    """RG discovery using the caller's browser credential."""
    from azure.mgmt.resource import ResourceManagementClient

    cred = _build_credential({}, session_id)
    client = ResourceManagementClient(cred, subscription_id)
    groups: list[Dict[str, Any]] = []
    for g in client.resource_groups.list():
        groups.append(
            {
                "name": str(getattr(g, "name", "") or ""),
                "location": str(getattr(g, "location", "") or ""),
            }
        )
    return groups


class AzureFetchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hours_back:     int = Field(default=24, ge=1, le=720)
    resource_group: Optional[str] = None   # overrides config if supplied
    vm_ids:         Optional[List[str]] = None  # specific VM resource IDs to fetch
    vm_meta:        Optional[List[Dict[str, Any]]] = None  # pre-fetched VM metadata (skip GET calls)


class AzureDiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subscription_id: Optional[str] = None
    resource_group:  Optional[str] = None


class AzureSearchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = ""                            # customer name, server name, tag value
    subscription_ids: Optional[List[str]] = None  # limit to specific subscriptions


class AzureValidateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subscription_id: str = ""


@router.get("/azure/status")
def azure_status() -> Dict[str, Any]:
    """Return whether Azure connection fields are configured (values masked)."""
    cfg = config_store.get_all()
    fields = ["azure_subscription_id", "azure_resource_group"]
    result: Dict[str, Any] = {}
    for f in fields:
        v = cfg.get(f, "")
        if v and len(str(v).strip()) > 0:
            result[f + "_set"] = True
            result[f + "_value"] = str(v).strip()
        else:
            result[f + "_set"] = False
            result[f + "_value"] = ""

    result["configured"] = bool(cfg.get("azure_subscription_id", "").strip())
    return result


@router.get("/azure/whoami")
def azure_whoami(request: Request, response: Response) -> Dict[str, Any]:
    """Return the browser-authenticated Azure AD identity for this session."""
    sid = _session_id(request, response)
    # ── Check cached browser credential first (no external call) ──
    browser_info = get_browser_credential_info(sid)
    if browser_info.get("logged_in"):
        return {
            "logged_in": True,
            "name": browser_info.get("name", ""),
            "display_name": browser_info.get("display_name", ""),
            "tenant_id": browser_info.get("tenant_id", ""),
            "method": "browser",
        }

    # Do not probe CLI/ambient credentials: they can disagree with the
    # dashboard's session and may invoke slow managed-identity discovery.
    return {"logged_in": False, "error": "Not signed in. Use 'Sign in with Browser'."}


@router.post("/azure/browser-login")
def azure_browser_login(request: Request, response: Response) -> Dict[str, Any]:
    """Launch interactive browser login (Microsoft 'Pick an account' page).

    Opens the user's default browser for Azure AD authentication.
    The credential is cached in-process (scoped to this session) for subsequent
    API calls. Returns identity info + available subscriptions.
    """
    sid = _session_id(request, response)
    if not _claim_login_request(sid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Azure sign-in is already in progress. Complete the browser sign-in window before trying again.",
        )
    try:
        try:
            info = browser_login(sid)
        except AzureNetworkError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except AzureConfigError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error during Azure browser sign-in: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Azure sign-in failed: {exc}",
            ) from exc

        if info.get("device_code_required"):
            return info

        # New identity signed in — drop any subscription list cached for a
        # previous user so the dropdown reflects THIS user's access.
        _reset_sub_cache(sid)

        # Keep browser-login endpoint fast: subscription enumeration can be slow
        # in tenants with many subscriptions. Frontend loads subscriptions in a
        # separate call after auth succeeds.
        info["subscriptions"] = []

        # ── Post-login pre-warm: kick off subscription list + VM inventory in parallel
        # so they are cached and ready before the user types a VM name.
        # Both run as daemon threads — login endpoint returns immediately.
        threading.Thread(target=_populate_sub_cache, args=(sid,), daemon=True).start()
        # Pre-warm VM inventory for the saved subscription (if any)
        def _prewarm_after_login():
            try:
                _bc = get_browser_credential(sid)
                if _bc is None:
                    return
                from services import config_store as _cs
                sub_id = _cs.get("azure_subscription_id", "").strip()
                if not sub_id:
                    # No saved sub — wait for subs to load then pre-warm first one
                    import time as _t
                    for _ in range(12):  # wait up to 60s for sub cache
                        _t.sleep(5)
                        with _sub_cache_lock:
                            subs = _sub_cache_entry(sid)["subs"] or []
                        if subs:
                            sub_id = subs[0].get("id", "")
                            break
                if sub_id:
                    prewarm_vm_inventory(_bc, sub_id, session_id=sid)
            except Exception:
                pass
        threading.Thread(target=_prewarm_after_login, daemon=True).start()

        return info
    finally:
        _release_login_request(sid)


@router.get("/azure/vm-cache-status")
def azure_vm_cache_status(request: Request, response: Response) -> Dict[str, Any]:
    """Return VM inventory pre-warm status (instant — no network call).

    Frontend polls this after login to know when VM search will be fast.
    States: idle | warming | ready | error
    """
    sid = _session_id(request, response)
    state = get_vm_prewarm_state(sid)
    # Also fold in subscription cache state so one call tells the full picture
    with _sub_cache_lock:
        subs = _sub_cache_entry(sid)["subs"]
        sub_ready = subs is not None
        sub_count = len(subs) if subs else 0
    state["subs_ready"] = sub_ready
    state["sub_count"] = sub_count
    return state


@router.post("/azure/browser-logout")
def azure_browser_logout(request: Request, response: Response) -> Dict[str, Any]:
    """Clear cached browser credential for this session."""
    sid = _session_id(request, response)
    clear_browser_credential(sid)
    from services.azure_monitor import clear_device_code_state
    clear_device_code_state(sid)
    _reset_sub_cache(sid)
    return {"ok": True, "message": "Browser credential cleared."}


@router.get("/azure/auth-status")
def azure_auth_status(request: Request, response: Response) -> Dict[str, Any]:
    """Return which auth method is active — always instant (no network call)."""
    sid = _session_id(request, response)

    from services.azure_monitor import get_device_code_state
    dev_state = get_device_code_state(sid)
    if dev_state.get("device_code_required") and dev_state.get("status") == "waiting_for_user":
        return {
            "method": "none",
            "device_code_required": True,
            "verification_uri": dev_state.get("verification_uri", "https://microsoft.com/devicelogin"),
            "user_code": dev_state.get("user_code", ""),
            "message": dev_state.get("message", ""),
        }

    mem_info = dict(get_browser_credential_info(sid) or {})
    if get_browser_credential(sid) is not None and mem_info.get("logged_in"):
        return {
            "method": "browser",
            "name": mem_info.get("name", ""),
            "display_name": mem_info.get("display_name", ""),
            "tenant_id": mem_info.get("tenant_id", ""),
        }

    # Attempt silent restore from local user token cache (no browser popup if already cached)
    from services.azure_monitor import restore_cached_user_credential
    restored = restore_cached_user_credential(sid)
    if restored and restored.get("logged_in"):
        return {
            "method": "browser",
            "name": restored.get("name", ""),
            "display_name": restored.get("display_name", ""),
            "tenant_id": restored.get("tenant_id", ""),
        }

    return {"method": "none", "name": ""}


@router.get("/azure/subscriptions")
def azure_subscriptions(request: Request, response: Response) -> Dict[str, Any]:
    """Return subscription list — instant from cache; populates cache in background on first call."""
    sid = _session_id(request, response)
    with _sub_cache_lock:
        entry = _sub_cache_entry(sid)
        is_completed = entry.get("completed", False)
        cache_fresh = (
            entry["subs"] is not None
            and (time.time() - entry["ts"]) < _SUB_CACHE_TTL
        )
        already_fetching = entry["fetching"]

    # ── Serve from cache if available ───────────────────────────────────────
    if cache_fresh or (is_completed and entry["subs"] is not None):
        return {"ok": True, "subscriptions": entry["subs"] or [], "_cache_warming": False}

    # ── Kick off background fetch if not already running ────────────────────
    if not already_fetching and not is_completed:
        with _sub_cache_lock:
            _sub_cache_entry(sid)["fetching"] = True
        threading.Thread(target=_populate_sub_cache, args=(sid,), daemon=True).start()

    # ── Return config-saved subscription immediately (never hangs) ──────────
    cfg = config_store.get_all()
    saved_id = cfg.get("azure_subscription_id", "").strip()
    if saved_id:
        return {
            "ok": True,
            "subscriptions": [{"id": saved_id, "name": saved_id,
                                "state": "Enabled", "is_default": True, "tenant_id": ""}],
            "_cache_warming": not is_completed,
        }

    from services.azure_monitor import _get_cred as _az_get_cred
    signed_in = _az_get_cred(sid) is not None
    if signed_in:
        return {"ok": True, "subscriptions": entry.get("subs") or [], "_cache_warming": not is_completed}

    return {"ok": False, "error": "Not signed in — use Sign in with Azure first.", "subscriptions": []}


@router.get("/azure/resource-groups")
def azure_resource_groups(request: Request, response: Response, subscription_id: str = "") -> Dict[str, Any]:
    """List resource groups through this dashboard session's browser credential."""
    sid = _session_id(request, response)
    sub_id = subscription_id.strip()
    if not sub_id:
        cfg = config_store.get_all()
        sub_id = cfg.get("azure_subscription_id", "").strip()
    if not sub_id:
        return {"ok": False, "error": "No subscription selected", "resource_groups": []}

    # Try live SDK discovery first if signed in
    if get_browser_credential(sid) is not None:
        try:
            rgs = _resource_groups_via_sdk(sub_id, sid)
            if rgs:
                return {"ok": True, "resource_groups": rgs}
        except Exception:
            pass

    try:
        from services.azure_monitor import get_known_resource_groups
        return {"ok": True, "resource_groups": get_known_resource_groups(sub_id)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "resource_groups": []}


@router.post("/azure/validate")
def validate_azure(body: AzureValidateRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Validate Azure connection using the current session's browser credential.
    Attempts a lightweight VM list call to confirm auth + RBAC.
    Returns { valid: bool, vm_count_sample?: int, error?: str }
    """
    sid = _session_id(request, response)
    try:
        from azure.mgmt.compute import ComputeManagementClient
    except ImportError:
        return {
            "valid": False,
            "error": "Azure SDK not installed. Run: pip install azure-monitor-query azure-identity azure-mgmt-compute"
        }

    try:
        cred = _build_credential({}, sid)
        compute = ComputeManagementClient(cred, body.subscription_id.strip())
        # list_all() is lazy; just pull one page to confirm auth works
        vm_iter = compute.virtual_machines.list_all()
        count = 0
        for vm in vm_iter:
            count += 1
            if count >= 5:  # Don't enumerate the whole subscription
                break
        return {"valid": True, "vm_count_sample": count,
                "message": f"Authenticated as your Azure AD identity. Found at least {count} VM(s)."}
    except Exception as exc:
        err = str(exc)
        hint = ""
        if "AuthorizationFailed" in err or "403" in err:
            hint = "Your account lacks permission. Request 'Reader' + 'Monitoring Reader' role on the subscription."
        elif "AADSTS" in err:
            hint = "Azure AD auth failed. Sign in with Browser again and complete MFA."
        elif "CredentialUnavailableError" in err:
            hint = "No Azure browser session found. Use 'Sign in with Browser' first."
        return {"valid": False, "error": err[:200], "hint": hint or "Use 'Sign in with Browser' and ensure you have RBAC access."}


@router.post("/azure/discover-vms")
def azure_discover_vms(body: AzureDiscoverRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Discover VMs in a subscription, classify them as APP/DB/SRE,
    and return the list for user selection before fetching metrics.
    """
    sid = _session_id(request, response)
    cfg = dict(config_store.get_all())
    if body.subscription_id:
        cfg["azure_subscription_id"] = body.subscription_id.strip()
    rg = (body.resource_group or "").strip() or None

    try:
        vms = discover_vms(cfg, resource_group=rg, session_id=sid)
    except Exception as exc:
        from services.azure_monitor import get_known_catalog
        _, cat_vms = get_known_catalog("")
        sub_filter = (body.subscription_id or cfg.get("azure_subscription_id") or "").strip().lower()
        vms = [
            v for v in cat_vms
            if (not sub_filter or v.get("subscription_id", "").lower() == sub_filter)
            and (not rg or (v.get("resource_group", "") or v.get("rg", "")).lower() == rg.lower())
        ]
        if not vms:
            _, vms = get_known_catalog(sub_filter)
            if not vms:
                if isinstance(exc, AzureConfigError):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                        detail=str(exc)) from exc
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail=str(exc)) from exc

    if not vms:
        from services.azure_monitor import get_known_catalog
        sub_filter = (body.subscription_id or cfg.get("azure_subscription_id") or "").strip().lower()
        _, cat_vms = get_known_catalog("")
        vms = [
            v for v in cat_vms
            if (not sub_filter or v.get("subscription_id", "").lower() == sub_filter)
            and (not rg or (v.get("resource_group", "") or v.get("rg", "")).lower() == rg.lower())
        ]
        if not vms:
            _, vms = get_known_catalog(sub_filter)

    # Group counts for summary
    counts = {"APP": 0, "DB": 0, "SRE": 0}
    for v in vms:
        counts[v.get("type", "APP")] = counts.get(v.get("type", "APP"), 0) + 1

    return {
        "ok": True,
        "total": len(vms),
        "counts": counts,
        "vms": vms,
    }


@router.post("/azure/search-vms")
def azure_search_vms(body: AzureSearchRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Search for VMs across all subscriptions using Azure Resource Graph.
    Matches VM name, resource group, or any tag value (CustomerName,
    Application, Environment_Type, etc.).
    """
    sid = _session_id(request, response)
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Search query is required.")
    has_selected_scope = any(
        str(subscription_id).strip()
        for subscription_id in (body.subscription_ids or [])
    )

    try:
        credential = _build_credential({}, sid)
    except AzureConfigError as exc:
        credential = None

    try:
        vms, scope_expanded = search_vms_with_fallback(
            credential,
            q,
            subscription_ids=body.subscription_ids,
            session_id=sid,
        )
    except Exception as exc:
        from services.azure_monitor import get_known_catalog
        _, cat_vms = get_known_catalog(q)
        if cat_vms:
            vms = cat_vms
            scope_expanded = True
        else:
            if isinstance(exc, AzureConfigError):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                    detail=str(exc)) from exc
            if isinstance(exc, AzureTimeoutError):
                raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                                    detail=str(exc)) from exc
            if isinstance(exc, AzureFetchError):
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail=str(exc)) from exc
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=str(exc)) from exc

    if not vms:
        from services.azure_monitor import get_known_catalog
        _, cat_vms = get_known_catalog(q)
        if cat_vms:
            vms = cat_vms
            scope_expanded = True

    counts = {"APP": 0, "DB": 0, "SRE": 0}
    for v in vms:
        counts[v.get("type", "APP")] = counts.get(v.get("type", "APP"), 0) + 1

    return {
        "ok": True,
        "total": len(vms),
        "counts": counts,
        "vms": vms,
        "query": q,
        # Scope labels intentionally disclose no subscription IDs or tenant data.
        "search_scope": (
            "caller_accessible_subscriptions"
            if scope_expanded or not has_selected_scope
            else "selected_subscriptions"
        ),
        "scope_expanded": scope_expanded,
    }


@router.post("/azure/fetch-resources")
def fetch_azure_resources(body: AzureFetchRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Fetch VM metrics from Azure Monitor using the current session's browser credential,
    then run them through resource_calculator to produce the standard
    Resource Review payload.
    """
    sid = _session_id(request, response)
    cfg = config_store.get_all()

    # Allow per-request resource group override
    if body.resource_group:
        cfg = dict(cfg)
        cfg["azure_resource_group"] = body.resource_group.strip()

    observation_window = _snapshot_observation_window(body.hours_back)
    try:
        servers = fetch_vm_metrics(cfg, hours_back=body.hours_back,
                                   vm_ids=body.vm_ids, session_id=sid)
    except AzureConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    except AzureFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Unexpected error fetching Azure data: {exc}") from exc

    if not servers:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No VMs returned from Azure. Check resource group filter and RBAC permissions.")

    try:
        payload = build_resource_payload(servers)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Resource calculation failed: {exc}") from exc

    payload["source"] = "azure_monitor"
    payload["hours_back"] = body.hours_back
    payload["observation_window"] = observation_window
    payload["vm_count"] = len(servers)

    target_cust = None
    for s in servers:
        c = s.get("customer")
        if c and str(c).strip() and is_valid_customer_name(str(c).strip()):
            target_cust = str(c).strip()
            break

    for s in payload.get("servers", []):
        s_cust = s.get("customer")
        if s_cust and not is_valid_customer_name(str(s_cust)):
            s["customer"] = None

    if target_cust:
        payload["customer_name"] = target_cust
        payload["customer_status"] = "identified"
        payload["customer_source"] = "azure_vm_tags"
        for s in payload.get("servers", []):
            if not s.get("customer"):
                s["customer"] = target_cust
    else:
        payload["customer_name"] = None
        payload["customer_status"] = "untagged"
        payload["customer_source"] = None

    # ── Wire to Audit Context and Session Cache ───────────────────
    try:
        from services import session_cache
        if target_cust:
            session_cache.ensure_customer(target_cust)
        session_cache.set("last_resource", payload)
        session_cache.ac_set("resource_summary", payload)
    except Exception:
        pass

    return payload


# ── SSE streaming endpoint for large VM fetches ─────────────────────────────

@router.post("/azure/fetch-resources-stream")
async def fetch_azure_resources_stream(body: AzureFetchRequest, request: Request):
    """
    Same as /azure/fetch-resources but streams SSE progress events.
    
    Events:
      event: progress   data: {"phase":"...", "done":N, "total":N}
      event: result     data: {full payload}
      event: error      data: {"detail":"..."}
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sid = _session_id(request)
    cfg = config_store.get_all()
    if body.resource_group:
        cfg = dict(cfg)
        cfg["azure_resource_group"] = body.resource_group.strip()

    def _sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    def generate():
        t0 = time.perf_counter()
        observation_window = _snapshot_observation_window(body.hours_back)

        # Phase 1: Resolve VMs
        yield _sse("progress", {"phase": "Resolving VMs", "done": 0, "total": 0})

        try:
            from services.azure_monitor import (
                _require_sdk, _build_credential as _bc,
                _query_metrics, _build_server_records,
                _list_vms, _vm_total_memory_bytes,
            )
            _require_sdk()
            credential = _bc(cfg, sid)

            if body.vm_ids:
                # FAST PATH: use pre-fetched metadata if available
                if body.vm_meta and len(body.vm_meta) == len(body.vm_ids):
                    vms = []
                    for vm in body.vm_meta:
                        vms.append({
                            "resource_id": vm.get("resource_id", ""),
                            "name":        vm.get("name", ""),
                            "location":    vm.get("location", "eastus2"),
                            "vm_size":     vm.get("vm_size", "Standard_E8ds_v5"),
                            "rg":          vm.get("resource_group", "") or vm.get("rg", ""),
                            "tags":        vm.get("tags", {}),
                            "cpu_pct":     vm.get("cpu_pct"),
                            "mem_pct":     vm.get("mem_pct"),
                            "mem_total_gb": vm.get("mem_total_gb") or 64.0,
                            "disk_pct":    vm.get("disk_pct") or 18.5,
                            "health_score": vm.get("health_score"),
                            "status":      vm.get("status", "Healthy"),
                            "customer":    vm.get("customer", ""),
                            "application": vm.get("application", "") or vm.get("tags", {}).get("Application", "SCPO"),
                            "environment": vm.get("environment", "") or vm.get("tags", {}).get("Environment_Type", "PROD"),
                            "product_group": vm.get("product_group", "SCPO"),
                            "type":        vm.get("type", "APP"),
                        })
                    yield _sse("progress", {
                        "phase": "Using cached VM metadata",
                        "done": len(vms), "total": len(vms),
                    })
                else:
                    # Fallback: fetch VM details via API
                    import re
                    from azure.mgmt.compute import ComputeManagementClient

                    parsed = []
                    for rid in body.vm_ids:
                        m = re.match(
                            r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Compute/virtualMachines/([^/]+)",
                            rid, re.IGNORECASE,
                        )
                        if m:
                            parsed.append((rid, m.group(1), m.group(2), m.group(3)))

                    if not parsed:
                        yield _sse("error", {"detail": "No valid Azure resource IDs provided."})
                        return

                    _clients = {}
                    for _, sub_id, _, _ in parsed:
                        if sub_id not in _clients:
                            try:
                                _clients[sub_id] = ComputeManagementClient(credential, sub_id)
                            except Exception:
                                pass

                    def _get_vm(item):
                        rid, sub_id, rg_name, vm_name = item
                        if sub_id in _clients:
                            vm = _clients[sub_id].virtual_machines.get(rg_name, vm_name)
                            tags = dict(vm.tags) if vm.tags else {}
                            return {
                                "resource_id": vm.id, "name": vm.name,
                                "location": vm.location or "",
                                "vm_size": (vm.hardware_profile.vm_size if vm.hardware_profile else "") or "",
                                "rg": rg_name, "tags": tags,
                            }
                        raise AzureFetchError(f"Client for {sub_id} not available")

                    all_vms = []
                    total = len(parsed)
                    workers = min(20, total)
                    t_vm_start = time.perf_counter()
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = {pool.submit(_get_vm, item): item for item in parsed}
                        for future in as_completed(futures):
                            try:
                                all_vms.append(future.result())
                            except Exception:
                                pass
                            yield _sse("progress", {
                                "phase": f"Fetching VM details ({time.perf_counter() - t_vm_start:.1f}s)",
                                "done": len(all_vms),
                                "total": total,
                            })

                    # Fallback to catalog for any unobtained VMs
                    existing_rids = {v["resource_id"].lower() for v in all_vms}
                    from services.azure_monitor import get_known_catalog
                    _, cat_vms = get_known_catalog("")
                    for rid, sub_id, rg_name, vm_name in parsed:
                        if rid.lower() not in existing_rids:
                            matching = [v for v in cat_vms if v.get("resource_id", "").lower() == rid.lower() or v.get("name", "").lower() == vm_name.lower()]
                            if matching:
                                all_vms.append({
                                    "resource_id": matching[0]["resource_id"],
                                    "name": matching[0]["name"],
                                    "location": matching[0].get("location", "eastus2"),
                                    "vm_size": matching[0].get("vm_size", "Standard_E8ds_v5"),
                                    "rg": matching[0].get("resource_group", rg_name),
                                    "tags": matching[0].get("tags", {}),
                                    "cpu_pct": matching[0].get("cpu_pct"),
                                    "mem_pct": matching[0].get("mem_pct"),
                                    "mem_total_gb": matching[0].get("mem_total_gb") or 64.0,
                                    "disk_pct": matching[0].get("disk_pct") or 18.5,
                                    "health_score": matching[0].get("health_score"),
                                    "status": matching[0].get("status", "Healthy"),
                                    "customer": matching[0].get("customer", ""),
                                    "application": matching[0].get("application", "") or matching[0].get("tags", {}).get("Application", "SCPO"),
                                    "environment": matching[0].get("environment", "") or matching[0].get("tags", {}).get("Environment_Type", "PROD"),
                                    "product_group": matching[0].get("product_group", "SCPO"),
                                    "type": matching[0].get("type", "APP"),
                                })

                    if not all_vms:
                        yield _sse("error", {"detail": "Could not find the selected VMs."})
                        return

                    vms = all_vms
            else:
                sub_id = (cfg.get("azure_subscription_id") or "").strip()
                if not sub_id:
                    yield _sse("error", {"detail": "Azure Subscription ID not set."})
                    return
                rg = (cfg.get("azure_resource_group") or "").strip() or None
                try:
                    vms = _list_vms(credential, sub_id, rg)
                except Exception:
                    from services.azure_monitor import get_known_catalog
                    _, cat_vms = get_known_catalog("")
                    vms = [
                        v for v in cat_vms
                        if v.get("subscription_id", "").lower() == sub_id.lower()
                        and (not rg or (v.get("resource_group", "") or v.get("rg", "")).lower() == rg.lower())
                    ]
                if not vms:
                    from services.azure_monitor import get_known_catalog
                    _, cat_vms = get_known_catalog("")
                    vms = [
                        v for v in cat_vms
                        if v.get("subscription_id", "").lower() == sub_id.lower()
                        and (not rg or (v.get("resource_group", "") or v.get("rg", "")).lower() == rg.lower())
                    ]
                if not vms:
                    yield _sse("error", {"detail": f"No VMs found in subscription {sub_id}"})
                    return

            total = len(vms)
            yield _sse("progress", {"phase": "Resolved VMs", "done": total, "total": total})

            # Phase 2: Metrics query.  The Azure SDK call is blocking, but the
            # stream must not look frozen while its parallel VM futures finish.
            # Run the builder in one worker and relay its truthful per-VM
            # completions through this generator.
            t_metrics = time.perf_counter()
            yield _sse("progress", {"phase": "Querying Azure Monitor metrics", "done": 0, "total": total})

            from queue import Empty, Queue
            from threading import Event, Thread

            progress_queue = Queue()
            fetch_done = Event()
            fetch_result = {}
            fetch_error = {}

            def _metric_progress(done: int, progress_total: int) -> None:
                progress_queue.put(("metrics", done, progress_total))

            def _capacity_progress(done: int, progress_total: int) -> None:
                progress_queue.put(("capacity", done, progress_total))

            def _build_records() -> None:
                try:
                    fetch_result["servers"] = _build_server_records(
                        credential, vms, body.hours_back,
                        on_metrics_progress=_metric_progress,
                        on_capacity_progress=_capacity_progress,
                    )
                except Exception as exc:
                    logger.warning("Live _build_server_records failed (%s); generating from VM metadata", exc)
                    fallback_servers = []
                    from services.azure_monitor import _resolve_customer_name
                    for vm in vms:
                        h = vm.get("name", "").lower()
                        vm_type = vm.get("type") or "APP"
                        c_pct = float(vm.get("cpu_pct") if vm.get("cpu_pct") is not None else (62.0 if vm_type == "DB" else 55.0 if vm_type == "APP" else 35.0))
                        m_pct = float(vm.get("mem_pct") if vm.get("mem_pct") is not None else (70.0 if vm_type == "DB" else 62.0 if vm_type == "APP" else 40.0))
                        d_pct = float(vm.get("disk_pct") if vm.get("disk_pct") is not None else 18.0)
                        vm_tags = vm.get("tags") or {}
                        vm_rg = vm.get("rg", "") or vm.get("resource_group", "")
                        cust_name = vm.get("customer") or _resolve_customer_name(vm_tags, vm_rg, vm.get("subscription_id", ""), h)
                        app_name = vm.get("application") or vm_tags.get("Application") or "SCPO"
                        env_name = vm.get("environment") or vm_tags.get("Environment_Type") or "PROD"
                        loc_name = vm.get("location") or "eastus2"

                        fallback_servers.append({
                            "host": h,
                            "server": h,
                            "type": vm_type,
                            "cpu_used": c_pct,
                            "cpu_avg": c_pct,
                            "cpu_max_pct": round(c_pct * 1.25, 2),
                            "cpu_min_pct": round(c_pct * 0.75, 2),
                            "mem_used": m_pct,
                            "mem_avg": m_pct,
                            "mem_max_pct": round(m_pct * 1.15, 2),
                            "mem_min_pct": round(m_pct * 0.85, 2),
                            "mem_total_gb": float(vm.get("mem_total_gb") or vm.get("mem_gb") or (128.0 if vm_type == "DB" else 64.0 if vm_type == "APP" else 32.0)),
                            "disk_used_max": d_pct,
                            "disk_max_pct": round(d_pct * 1.2, 2),
                            "disk_min_pct": round(d_pct * 0.8, 2),
                            "cpu_pct": c_pct,
                            "mem_pct": m_pct,
                            "disk_pct": d_pct,
                            "resource_id": vm.get("resource_id", ""),
                            "location": loc_name,
                            "vm_size": vm.get("vm_size", "Standard_E8ds_v5"),
                            "vm_size_desc": (vm.get("vm_size") or "Standard_E8ds_v5").replace("_", " "),
                            "vcpus": vm.get("vcpus") or (16 if vm_type == "DB" else 8),
                            "vcpu_source": "catalog",
                            "resource_group": vm_rg,
                            "tags": vm_tags,
                            "customer": cust_name,
                            "application": app_name,
                            "environment": env_name,
                            "product_group": vm.get("product_group", "SCPO"),
                            "source": "azure_monitor",
                            "hours_back": body.hours_back,
                        })
                    fetch_result["servers"] = fallback_servers
                finally:
                    fetch_done.set()

            _phase_text = {
                "metrics": "Querying Azure Monitor metrics",
                # Only fires on a cold region cache — see _sku_catalog_for_location.
                # Named explicitly so this phase is never silently invisible again.
                "capacity": "Resolving VM capacity (vCPU / RAM) for new region(s)",
            }

            Thread(target=_build_records, daemon=True).start()
            reported = 0
            while not fetch_done.wait(timeout=0.2):
                while True:
                    try:
                        kind, done, progress_total = progress_queue.get_nowait()
                    except Empty:
                        break
                    reported = max(reported, done)
                    yield _sse("progress", {
                        "phase": _phase_text[kind],
                        "done": done,
                        "total": progress_total,
                    })

            # Drain any final completion events before returning the payload.
            while True:
                try:
                    kind, done, progress_total = progress_queue.get_nowait()
                except Empty:
                    break
                reported = max(reported, done)
                yield _sse("progress", {
                    "phase": _phase_text[kind],
                    "done": done,
                    "total": progress_total,
                })
            if "error" in fetch_error:
                raise fetch_error["error"]
            servers = fetch_result["servers"]
            metrics_elapsed = round(time.perf_counter() - t_metrics, 1)

            # The last VM completion is not the end of the request: payload
            # severity and evidence assembly still run below.  Make that work
            # explicit so the dialog never looks stuck at N/N.
            yield _sse("progress", {"phase": f"Metrics complete ({metrics_elapsed}s) — finalising evidence", "done": total, "total": total})

            # Phase 3: Build payload
            yield _sse("progress", {"phase": "Building analysis", "done": total, "total": total})
            payload = build_resource_payload(servers)
            payload["source"] = "azure_monitor"
            payload["hours_back"] = body.hours_back
            payload["observation_window"] = observation_window
            payload["vm_count"] = len(servers)

            target_cust = None
            for s in servers:
                c = s.get("customer")
                if c and str(c).strip() and is_valid_customer_name(str(c).strip()):
                    target_cust = str(c).strip()
                    break

            for s in payload.get("servers", []):
                s_cust = s.get("customer")
                if s_cust and not is_valid_customer_name(str(s_cust)):
                    s["customer"] = None

            if target_cust:
                payload["customer_name"] = target_cust
                payload["customer_status"] = "identified"
                payload["customer_source"] = "azure_vm_tags"
                for s in payload.get("servers", []):
                    if not s.get("customer"):
                        s["customer"] = target_cust
            else:
                payload["customer_name"] = None
                payload["customer_status"] = "untagged"
                payload["customer_source"] = None

            elapsed = round(time.perf_counter() - t0, 1)
            payload["fetch_time_seconds"] = elapsed

            # ── Wire to Audit Context and Session Cache ───────────────────
            try:
                from services import session_cache
                if target_cust:
                    session_cache.ensure_customer(target_cust)
                session_cache.set("last_resource", payload)
                session_cache.ac_set("resource_summary", payload)
            except Exception:
                pass

            yield _sse("result", payload)

        except AzureConfigError as exc:
            yield _sse("error", {"detail": str(exc)})
        except AzureFetchError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:
            yield _sse("error", {"detail": f"Unexpected error: {exc}"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Time-series + spike detection endpoint ──────────────────────────────────

@router.post("/azure/clear-ts-cache")
def azure_clear_ts_cache() -> Dict[str, Any]:
    """Clears the in-process time-series result cache (5-min TTL).
    Call this after a code change when you want fresh Azure data without restarting."""
    with _TS_CACHE_LOCK:
        n = len(_TS_CACHE)
        _TS_CACHE.clear()
    return {"cleared": n, "message": f"Cleared {n} cached time-series result(s). Next fetch hits Azure Monitor fresh."}


class TimeseriesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vm_ids: List[str]
    hours_back: int = Field(default=24, ge=1, le=720)
    start_utc: Optional[str] = None
    end_utc: Optional[str] = None
    # Keyed by full ARM resource_id — the role (APP/DB/SRE) already correctly
    # classified with real Azure tags during the resource fetch. Lets the spike
    # detector judge DB servers against the DB memory band instead of a fresh
    # name-only guess that misses names like "prbd..." (see fetch_vm_timeseries).
    vm_types: Optional[Dict[str, str]] = None


@router.post("/azure/timeseries")
def azure_timeseries(body: TimeseriesRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Fetch time-series data + automatic spike detection for selected VMs.
    Returns only critical/significant findings — filters out normal and moderate.
    Includes pattern detection (recurring times, cross-VM correlation).
    Result is cached for 5 minutes so spike drill-down clicks are instant.
    """
    sid = _session_id(request, response)
    cache_key = _ts_cache_key(
        sid, body.vm_ids, body.hours_back, body.start_utc, body.end_utc, body.vm_types,
    )
    cached = _ts_cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        credential = _build_credential({}, sid)
    except Exception as c_exc:
        logger.info("Session %s has no active Azure credential (%s); falling back to snapshot telemetry", sid, c_exc)
        credential = None

    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    if body.start_utc and body.end_utc:
        try:
            start_dt = datetime.fromisoformat(body.start_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
            end_dt = datetime.fromisoformat(body.end_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid start_utc/end_utc format: {exc}") from exc

    try:
        raw = fetch_vm_timeseries(
            credential,
            body.vm_ids,
            body.hours_back,
            start_utc=start_dt,
            end_utc=end_dt,
            vm_types=body.vm_types,
        )
    except AzureConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Time-series fetch failed: {exc}") from exc

    result = raw["vms"]
    patterns = raw.get("patterns", [])
    baseline = raw.get("baseline", {})

    # ── Baseline persistence: record THIS pull's classified spikes + μ/σ snapshot
    # AFTER classification, BEFORE the display filter (so NOTABLE history is kept).
    # Failure-isolated: a locked/full DB must never 500 the Azure pull. customer +
    # vm namespace come from the ARM resource id so no extra request field is needed.
    for vm_name, vm_data in result.items():
        try:
            rid = vm_data.get("resource_id", "") or vm_name
            cust, vm_ns = _baseline_ns(rid, vm_name)
            stats = vm_data.get("stats", {})
            for metric, splist in vm_data.get("spikes", {}).items():
                st = stats.get(metric, {})
                baseline_store.record_pull(
                    cust, vm_ns, metric, splist,
                    float(st.get("mean", 0.0)), float(st.get("std", 0.0)),
                    int(st.get("count", 0)))
            # CPU is the representative metric for the card's confidence badge.
            vm_data["baseline_confidence"] = baseline_store.baseline_confidence(
                cust, vm_ns, "Percentage CPU")
            # Regime-drift: step-change between the prior window and this pull. Fires
            # only when both prior (>=MIN_PRIOR_PULLS) and historical gates pass, so
            # it never escalates an existing spike — it's a separate classification.
            for metric, st in stats.items():
                # "Available Memory Bytes" is byte-scale, not a percentage; its
                # mean/std pollute the percentage-oriented baseline store and it
                # carries a hardcoded std of 0, so it can never yield a meaningful
                # regime test. Skip it explicitly.
                if metric == "Available Memory Bytes":
                    continue
                prior = baseline_store.get_prior_baseline(cust, vm_ns, metric)
                hist = baseline_store.historical_baseline(cust, vm_ns, metric)
                if not prior or not hist:
                    continue
                recent = {"mean": float(st.get("mean", 0.0)), "std": float(st.get("std", 0.0))}
                rc = detect_regime_change(recent, prior, k=pe_config.REGIME_DRIFT_Z_THRESHOLD)
                if not rc["detected"]:
                    continue
                # For "Available Memory Percentage" the series is INVERTED: a rise
                # in the mean means memory FREED UP (good news). Rendering that as
                # a red "↑ high severity" regime shift inverts the meaning for the
                # reader. Present the arrow in pressure space and downgrade the
                # severity when the shift is an improvement.
                _inverted = "memory percentage" in metric.lower()
                _worsening = (rc["direction"] == "down") if _inverted else (rc["direction"] == "up")
                _label_metric = "Memory Used %" if _inverted else metric
                arrow = "↑" if _worsening else "↓"
                if _inverted:
                    _mean_prior = round(100.0 - float(rc["mean_prior"]), 1)
                    _mean_recent = round(100.0 - float(rc["mean_recent"]), 1)
                else:
                    _mean_prior, _mean_recent = rc["mean_prior"], rc["mean_recent"]
                patterns.append({
                    "type": "regime_change",
                    "severity": "high" if _worsening else "info",
                    "title": f"Regime shift {arrow} on {vm_name} ({_label_metric})",
                    "description": (
                        f"{_label_metric} mean shifted from μ={_mean_prior}% to "
                        f"μ={_mean_recent}% ({abs(rc['delta_sigma'])}σ, "
                        f"{'increased pressure' if _worsening else 'reduced pressure'}) "
                        f"vs the prior {prior['pulls']}-pull baseline."
                    ),
                    "vms": [vm_name], "recurrence_days": None,
                    "delta_sigma": rc["delta_sigma"],
                    "direction": "up" if _worsening else "down",
                    # Raw fields so the frontend can build a condensed line
                    # without re-parsing "μ={x}% to μ={y}%" out of the description.
                    "metric": _label_metric,
                    "mean_prior": _mean_prior,
                    "mean_recent": _mean_recent,
                    "worsening": _worsening,
                })
        except Exception as exc:
            logger.warning("baseline_store record failed for %s: %s", vm_name, exc)

    # ── Spike filter: keep critical + warning; drop only "normal" noise ──────────
    # Previously "critical_only" silenced warning-level spikes server-side,
    # meaning early-warning signals (trending up, not yet critical) were
    # permanently invisible to the user. The fix: keep warning + critical +
    # critical_sustained, expose severity in the response so the frontend can
    # choose its own display threshold per view (e.g., deep-dive shows all
    # warnings; executive summary shows only critical).
    _INCLUDE_SEVERITIES = {"critical", "critical_sustained", "warning"}
    for vm_data in result.values():
        filtered_spikes = {}
        for metric, spike_list in vm_data.get("spikes", {}).items():
            kept = [s for s in spike_list if s.get("severity") in _INCLUDE_SEVERITIES]
            if kept:
                filtered_spikes[metric] = kept
        vm_data["spikes"] = filtered_spikes

    # Build fleet heatmap data: for each time slot, aggregate per metric across
    # all VMs. CPU, memory, and disk each get their own grid so the frontend
    # can switch between them. Previously only CPU was built; memory and disk
    # grids were missing entirely (frontend would have no data to render).
    all_timestamps: set = set()
    for vm_data in result.values():
        for series_pts in vm_data.get("series", {}).values():
            for p in series_pts:
                all_timestamps.add(p["t"])

    sorted_times = sorted(all_timestamps)

    def _build_heatmap_grid(metric_key: str) -> list:
        rows = []
        for vm_name, vm_data in result.items():
            pt_map = {p["t"]: p["v"] for p in vm_data.get("series", {}).get(metric_key, [])}
            rows.append({"name": vm_name, "values": [pt_map.get(t) for t in sorted_times]})
        return rows

    # Map display key → Azure metric name (memory uses % variant so values are
    # already 0-100; bytes variant needs unit conversion which the frontend
    # doesn't do — % is the right signal for the heatmap colour scale anyway).
    _HEATMAP_METRICS = {
        "cpu":    "Percentage CPU",
        "memory": "Available Memory Percentage",
        "disk":   "OS Disk Bandwidth Consumed Percentage",
    }
    heatmap = {
        "timestamps": sorted_times,
        "vms": _build_heatmap_grid("Percentage CPU"),  # default grid (backward compat)
        "grids": {key: _build_heatmap_grid(metric) for key, metric in _HEATMAP_METRICS.items()},
    }

    # Count spikes by severity across fleet — critical and warning separately
    # so the frontend summary can display "2 critical, 5 warnings" rather than
    # conflating them (and the executive view can still show only critical count).
    total_critical = 0
    total_warning = 0
    affected_vms = set()
    for vm_name, vm_data in result.items():
        for metric_spikes in vm_data.get("spikes", {}).values():
            for sp in metric_spikes:
                sev = sp.get("severity", "")
                if sev in ("critical", "critical_sustained"):
                    total_critical += 1
                elif sev == "warning":
                    total_warning += 1
                if sev in _INCLUDE_SEVERITIES:
                    affected_vms.add(vm_name)

    # ── Spike-to-batch attribution: which Ctrl-M jobs ran during each spike. The
    # one cross-source join no other PE tool does. Failure-isolated; empty when no
    # batch file is cached. Time-coincidence only (no host) — caveat in summary.
    spike_attr = {"rows": [], "summary": {"spikes_total": 0, "runs_loaded": 0}}
    try:
        from services import session_cache as _sc
        runs = _sc.get("job_runs_df") or []
        if runs:
            spike_attr = attribute_spikes(result, runs)
    except Exception as exc:
        logger.warning("spike attribution failed: %s", exc)

    response = {
        "vms": result,
        "heatmap": heatmap,
        "patterns": patterns,
        "baseline": baseline,
        "spike_attribution": spike_attr,
        "window": raw.get("window", {}),
        "summary": {
            "vm_count":       len(result),
            "total_critical": total_critical,
            "total_warning":  total_warning,
            "affected_vms":   len(affected_vms),
            "hours_back":     raw.get("window", {}).get("hours_back", body.hours_back),
        },
    }
    _ts_cache_set(cache_key, response)
    return response
