"""
Azure resource router.

POST /api/azure/fetch-resources
    body: { hours_back?: int, resource_group?: str }
    response: same shape as /api/process-resource
              { kpis, anomalies, servers, executive_summary }

GET /api/azure/status
    Returns whether Azure connection is configured (subscription ID set).

GET /api/azure/whoami
    Returns the currently logged-in Azure AD identity (from az login).

POST /api/azure/validate
    Validates Azure connection by attempting a lightweight API call using
    the user's az login identity.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services import config_store
from services.azure_monitor import (
    AzureConfigError,
    AzureFetchError,
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
    search_vms,
)
from services.resource_calculator import build_resource_payload
from services import baseline_store
from services import pe_config
router = APIRouter()


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
            max_age=_PE_SID_MAX_AGE, httponly=True, samesite="lax", path="/",
        )
    return sid

# ── Timeseries result cache ──────────────────────────────────────────────────
# Caches the full processed response for (vm_ids, window) combinations so that
# spike drill-down clicks don't re-fetch from Azure Monitor on every click.
# TTL = 5 minutes. Cache is keyed by a SHA256 of the canonical request params.
_TS_CACHE: Dict[str, Dict[str, Any]] = {}
_TS_CACHE_LOCK = threading.Lock()
_TS_CACHE_TTL = 300  # seconds


def _ts_cache_key(vm_ids: List[str], hours_back: int,
                  start_utc: Optional[str], end_utc: Optional[str]) -> str:
    canonical = json.dumps(
        {"ids": sorted(vm_ids), "h": hours_back, "s": start_utc, "e": end_utc},
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
        _TS_CACHE[key] = {"ts": time.time(), "data": data}

# ── Subscription list cache ──────────────────────────────────────────────────
# Populated once in the background; served instantly on all subsequent calls.
_sub_cache: Dict[str, Any] = {"subs": None, "ts": 0.0, "fetching": False}
_sub_cache_lock = threading.Lock()
_SUB_CACHE_TTL = 600  # 10 minutes


def _reset_sub_cache() -> None:
    """Wipe the subscription cache — called on login/logout so a different
    user never sees the previous user's subscription list."""
    with _sub_cache_lock:
        _sub_cache["subs"] = None
        _sub_cache["ts"] = 0.0
        _sub_cache["fetching"] = False
    # Also drop cached VM inventory so a different user never sees stale VMs
    try:
        clear_vm_inventory_cache()
    except Exception:
        pass


def _populate_sub_cache(session_id=None) -> None:
    """Background worker: fetch all subscriptions and store in _sub_cache.

    Order of preference:
    1. Browser credential SDK (always fresh after browser login — fast, ~1-3s)
    2. az CLI fallback (may be stale or slow — used only when SDK unavailable)
    """
    import subprocess as _sp, json as _json
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

    # ── 2. az CLI fallback (only if SDK returned nothing)
    if not rows:
        try:
            proc = _sp.run(
                ["az", "account", "list", "--output", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                subs = _json.loads(proc.stdout)
                rows = [
                    {"id": s.get("id", ""), "name": s.get("name", ""),
                     "state": s.get("state", ""), "is_default": s.get("isDefault", False),
                     "tenant_id": s.get("tenantId", "")}
                    for s in subs if s.get("state") == "Enabled"
                ]
        except Exception:
            pass

    with _sub_cache_lock:
        _sub_cache["subs"] = rows if rows else None
        _sub_cache["ts"] = time.time()
        _sub_cache["fetching"] = False


def _subscriptions_via_sdk(session_id=None) -> list[Dict[str, Any]]:
    """Subscription discovery using cached browser credential or DefaultAzureCredential."""
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
    """RG discovery using cached browser credential or DefaultAzureCredential."""
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
    """Return the Azure AD identity from the current session.

    Priority: 1) cached browser credential  2) az CLI  3) DefaultAzureCredential
    """
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

    # ── Try az CLI (gives subscription + tenant display name) ──
    try:
        import subprocess, json as _json
        proc = subprocess.run(
            ["az", "account", "show", "--output", "json", "--only-show-errors"],
            capture_output=True, text=True, timeout=4,  # 4s max — expired token hangs otherwise
        )
        if proc.returncode == 0:
            acct = _json.loads(proc.stdout)
            user_info = acct.get("user", {})
            return {
                "logged_in": True,
                "name":      user_info.get("name", ""),
                "type":      user_info.get("type", ""),
                "tenant_id": acct.get("tenantId", ""),
                "tenant_name": acct.get("tenantDisplayName", ""),
                "subscription": acct.get("name", ""),
                "subscription_id": acct.get("id", ""),
                "method":    "az_cli",
            }
    except (FileNotFoundError, Exception):
        pass  # CLI not installed or failed — fall through to SDK

    # If neither browser credential nor az CLI returned a result,
    # the user is not signed in. DefaultAzureCredential is intentionally NOT
    # used here — its ManagedIdentity probe hangs for 30s+ on non-Azure VMs.
    return {"logged_in": False, "error": "Not signed in. Use 'Sign in with Browser' or run 'az login'."}


@router.post("/azure/browser-login")
def azure_browser_login(request: Request, response: Response) -> Dict[str, Any]:
    """Launch interactive browser login (Microsoft 'Pick an account' page).

    Opens the user's default browser for Azure AD authentication.
    The credential is cached in-process (scoped to this session) for subsequent
    API calls. Returns identity info + available subscriptions.
    """
    sid = _session_id(request, response)
    try:
        info = browser_login(sid)
    except AzureConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    # New identity signed in — drop any subscription list cached for a
    # previous user so the dropdown reflects THIS user's access.
    _reset_sub_cache()

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
                        subs = _sub_cache.get("subs") or []
                    if subs:
                        sub_id = subs[0].get("id", "")
                        break
            if sub_id:
                prewarm_vm_inventory(_bc, sub_id, session_id=sid)
        except Exception:
            pass
    threading.Thread(target=_prewarm_after_login, daemon=True).start()

    return info


@router.get("/azure/vm-cache-status")
def azure_vm_cache_status() -> Dict[str, Any]:
    """Return VM inventory pre-warm status (instant — no network call).

    Frontend polls this after login to know when VM search will be fast.
    States: idle | warming | ready | error
    """
    state = get_vm_prewarm_state()
    # Also fold in subscription cache state so one call tells the full picture
    with _sub_cache_lock:
        sub_ready = _sub_cache["subs"] is not None
        sub_count = len(_sub_cache["subs"]) if _sub_cache["subs"] else 0
    state["subs_ready"] = sub_ready
    state["sub_count"] = sub_count
    return state


@router.post("/azure/browser-logout")
def azure_browser_logout(request: Request, response: Response) -> Dict[str, Any]:
    """Clear cached browser credential for this session."""
    sid = _session_id(request, response)
    clear_browser_credential(sid)
    _reset_sub_cache()
    return {"ok": True, "message": "Browser credential cleared."}


@router.get("/azure/auth-status")
def azure_auth_status(request: Request, response: Response) -> Dict[str, Any]:
    """Return which auth method is active — always instant (no network call).

    Checks in this order, all O(1)/disk-read only:
    1. In-memory credential (already loaded this server session)
    2. Saved JSON identity file on disk (survives server restart)
    The actual credential object is restored lazily in the background
    when it is first needed for a real API call.
    """
    sid = _session_id(request, response)
    # 1. In-memory — instant (session-scoped, no network)
    from services.azure_monitor import _get_cred, _get_info, _load_credential_info
    mem_info = dict(_get_info(sid) or {})
    if _get_cred(sid) is not None and mem_info.get("logged_in"):
        return {
            "method": "browser",
            "name": mem_info.get("name", ""),
            "display_name": mem_info.get("display_name", ""),
            "tenant_id": mem_info.get("tenant_id", ""),
        }

    # 2. Disk cache — instant (just reads a small JSON file, no Azure network call)
    disk_info = _load_credential_info(sid)
    if disk_info.get("logged_in"):
        return {
            "method": "browser",
            "name": disk_info.get("name", ""),
            "display_name": disk_info.get("display_name", ""),
            "tenant_id": disk_info.get("tenant_id", ""),
        }

    return {"method": "none", "name": ""}


@router.get("/azure/subscriptions")
def azure_subscriptions(request: Request, response: Response) -> Dict[str, Any]:
    """Return subscription list — instant from cache; populates cache in background on first call."""
    sid = _session_id(request, response)
    with _sub_cache_lock:
        cache_fresh = (
            _sub_cache["subs"] is not None
            and (time.time() - _sub_cache["ts"]) < _SUB_CACHE_TTL
        )
        already_fetching = _sub_cache["fetching"]

    # ── Serve from cache if available ───────────────────────────────────────
    if cache_fresh:
        return {"ok": True, "subscriptions": _sub_cache["subs"]}

    # ── Kick off background fetch if not already running ────────────────────
    if not already_fetching:
        with _sub_cache_lock:
            _sub_cache["fetching"] = True
        threading.Thread(target=_populate_sub_cache, args=(sid,), daemon=True).start()

    # ── Return config-saved subscription immediately (never hangs) ──────────
    cfg = config_store.get_all()
    saved_id = cfg.get("azure_subscription_id", "").strip()
    if saved_id:
        return {
            "ok": True,
            "subscriptions": [{"id": saved_id, "name": saved_id,
                                "state": "Enabled", "is_default": True, "tenant_id": ""}],
            "_cache_warming": True,   # hint to client: full list loading in background
        }

    # Signed in but no saved subscription yet — the background worker is still
    # enumerating. Tell the client to keep polling instead of reporting a false
    # "not signed in" (which would make the dropdown give up prematurely).
    from services.azure_monitor import _get_cred as _az_get_cred, _load_credential_info as _az_disk
    signed_in = (_az_get_cred(sid) is not None) or bool(_az_disk(sid).get("logged_in"))
    if signed_in:
        return {"ok": True, "subscriptions": [], "_cache_warming": True}

    return {"ok": False, "error": "Not signed in — use Sign in with Browser first.", "subscriptions": []}


@router.get("/azure/resource-groups")
def azure_resource_groups(request: Request, response: Response, subscription_id: str = "") -> Dict[str, Any]:
    """List RGs using az CLI first, then SDK fallback."""
    sid = _session_id(request, response)
    sub_id = subscription_id.strip()
    if not sub_id:
        cfg = config_store.get_all()
        sub_id = cfg.get("azure_subscription_id", "").strip()
    if not sub_id:
        return {"ok": False, "error": "No subscription selected", "resource_groups": []}

    try:
        import subprocess, json as _json
        proc = subprocess.run(
            ["az", "group", "list", "--subscription", sub_id, "--output", "json"],
            capture_output=True, text=True, timeout=4,
        )
        if proc.returncode == 0:
            groups = _json.loads(proc.stdout)
            return {
                "ok": True,
                "resource_groups": [
                    {"name": g.get("name", ""), "location": g.get("location", "")}
                    for g in groups
                ],
            }
        # CLI command failed; continue to SDK fallback below.
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # SDK fallback — only when browser credential is active (prevents hanging).
    if get_browser_credential(sid) is not None:
        try:
            return {"ok": True, "resource_groups": _resource_groups_via_sdk(sub_id, sid)}
        except ImportError:
            return {
                "ok": False,
                "error": "Azure SDK not installed. Run: pip install azure-identity azure-mgmt-resource",
                "resource_groups": [],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200], "resource_groups": []}

    return {"ok": False, "error": "Not signed in — use Sign in with Browser first.", "resource_groups": []}


@router.post("/azure/validate")
def validate_azure(body: AzureValidateRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Validate Azure connection using the user's az login identity.
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
            hint = "Azure AD auth failed. Run 'az login' in your terminal and try again."
        elif "CredentialUnavailableError" in err or "DefaultAzureCredential" in err:
            hint = "No Azure session found. Use 'Sign in with Browser' or run 'az login' first."
        return {"valid": False, "error": err[:200], "hint": hint or "Use 'Sign in with Browser' or run 'az login' and ensure you have RBAC access."}


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
    except AzureConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    except AzureFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=str(exc)) from exc

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

    try:
        credential = _build_credential({}, sid)
    except AzureConfigError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=str(exc)) from exc

    try:
        vms = search_vms(credential, q,
                         subscription_ids=body.subscription_ids or None)
    except AzureConfigError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc)) from exc
    except AzureFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=str(exc)) from exc

    counts = {"APP": 0, "DB": 0, "SRE": 0}
    for v in vms:
        counts[v.get("type", "APP")] = counts.get(v.get("type", "APP"), 0) + 1

    return {
        "ok": True,
        "total": len(vms),
        "counts": counts,
        "vms": vms,
        "query": q,
    }


@router.post("/azure/fetch-resources")
def fetch_azure_resources(body: AzureFetchRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Fetch VM metrics from Azure Monitor using the user's az login identity,
    then run them through resource_calculator to produce the standard
    Resource Review payload.
    """
    sid = _session_id(request, response)
    cfg = config_store.get_all()

    # Allow per-request resource group override
    if body.resource_group:
        cfg = dict(cfg)
        cfg["azure_resource_group"] = body.resource_group.strip()

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
    payload["vm_count"] = len(servers)
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
                            "location":    vm.get("location", ""),
                            "vm_size":     vm.get("vm_size", ""),
                            "rg":          vm.get("resource_group", ""),
                            "tags":        vm.get("tags", {}),
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
                            _clients[sub_id] = ComputeManagementClient(credential, sub_id)

                    def _get_vm(item):
                        rid, sub_id, rg_name, vm_name = item
                        vm = _clients[sub_id].virtual_machines.get(rg_name, vm_name)
                        tags = dict(vm.tags) if vm.tags else {}
                        return {
                            "resource_id": vm.id, "name": vm.name,
                            "location": vm.location or "",
                            "vm_size": (vm.hardware_profile.vm_size if vm.hardware_profile else "") or "",
                            "rg": rg_name, "tags": tags,
                        }

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
                vms = _list_vms(credential, sub_id, rg)
                if not vms:
                    yield _sse("error", {"detail": f"No VMs found in subscription {sub_id}"})
                    return

            total = len(vms)
            yield _sse("progress", {"phase": "Resolved VMs", "done": total, "total": total})

            # Phase 2: Metrics query
            t_metrics = time.perf_counter()
            yield _sse("progress", {"phase": "Querying metrics", "done": 0, "total": total})

            servers = _build_server_records(credential, vms, body.hours_back)
            metrics_elapsed = round(time.perf_counter() - t_metrics, 1)

            yield _sse("progress", {"phase": f"Metrics complete ({metrics_elapsed}s)", "done": total, "total": total})

            # Phase 3: Build payload
            yield _sse("progress", {"phase": "Building analysis", "done": total, "total": total})
            payload = build_resource_payload(servers)
            payload["source"] = "azure_monitor"
            payload["hours_back"] = body.hours_back
            payload["vm_count"] = len(servers)

            elapsed = round(time.perf_counter() - t0, 1)
            payload["fetch_time_seconds"] = elapsed

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

class TimeseriesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vm_ids: List[str]
    hours_back: int = Field(default=24, ge=1, le=720)
    start_utc: Optional[str] = None
    end_utc: Optional[str] = None


@router.post("/azure/timeseries")
def azure_timeseries(body: TimeseriesRequest, request: Request, response: Response) -> Dict[str, Any]:
    """
    Fetch time-series data + automatic spike detection for selected VMs.
    Returns only critical/significant findings — filters out normal and moderate.
    Includes pattern detection (recurring times, cross-VM correlation).
    Result is cached for 5 minutes so spike drill-down clicks are instant.
    """
    sid = _session_id(request, response)
    cache_key = _ts_cache_key(body.vm_ids, body.hours_back, body.start_utc, body.end_utc)
    cached = _ts_cache_get(cache_key)
    if cached is not None:
        return cached

    credential = _build_credential({}, sid)

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
                prior = baseline_store.get_prior_baseline(cust, vm_ns, metric)
                hist = baseline_store.historical_baseline(cust, vm_ns, metric)
                if not prior or not hist:
                    continue
                recent = {"mean": float(st.get("mean", 0.0)), "std": float(st.get("std", 0.0))}
                rc = detect_regime_change(recent, prior, k=pe_config.REGIME_DRIFT_Z_THRESHOLD)
                if not rc["detected"]:
                    continue
                arrow = "↑" if rc["direction"] == "up" else "↓"
                patterns.append({
                    "type": "regime_change",
                    "severity": "high",
                    "title": f"Regime shift {arrow} on {vm_name} ({metric})",
                    "description": (
                        f"{metric} mean shifted from μ={rc['mean_prior']}% to "
                        f"μ={rc['mean_recent']}% ({'+' if rc['delta_sigma'] >= 0 else ''}{rc['delta_sigma']}σ) "
                        f"vs the prior {prior['pulls']}-pull baseline."
                    ),
                    "vms": [vm_name], "recurrence_days": None,
                    "delta_sigma": rc["delta_sigma"], "direction": rc["direction"],
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

    response = {
        "vms": result,
        "heatmap": heatmap,
        "patterns": patterns,
        "baseline": baseline,
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
