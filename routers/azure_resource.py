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

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services import config_store
from services.azure_monitor import (
    AzureConfigError,
    AzureFetchError,
    _browser_credential,
    _build_credential,
    browser_login,
    clear_browser_credential,
    discover_vms,
    fetch_vm_metrics,
    fetch_vm_timeseries,
    get_browser_credential_info,
    search_vms,
)
from services.resource_calculator import build_resource_payload

router = APIRouter()


def _subscriptions_via_sdk() -> list[Dict[str, Any]]:
    """Subscription discovery using cached browser credential or DefaultAzureCredential."""
    from azure.mgmt.subscription import SubscriptionClient

    cred = _build_credential({})
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


def _resource_groups_via_sdk(subscription_id: str) -> list[Dict[str, Any]]:
    """RG discovery using cached browser credential or DefaultAzureCredential."""
    from azure.mgmt.resource import ResourceManagementClient

    cred = _build_credential({})
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
def azure_whoami() -> Dict[str, Any]:
    """Return the Azure AD identity from the current session.

    Priority: 1) cached browser credential  2) az CLI  3) DefaultAzureCredential
    """
    # ── Check cached browser credential first (no external call) ──
    browser_info = get_browser_credential_info()
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
            ["az", "account", "show", "--output", "json"],
            capture_output=True, text=True, timeout=10,
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

    # ── Fallback: get a token via DefaultAzureCredential + decode JWT ──
    try:
        from azure.identity import DefaultAzureCredential
        import json as _json, base64, logging
        # Suppress the noisy credential chain errors
        logging.getLogger("azure.identity").setLevel(logging.ERROR)
        cred = DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default")
        # Decode JWT payload (middle segment) — no verification needed,
        # we just want the claims (upn, name, tid, etc.)
        payload_b64 = token.token.split(".")[1]
        # Fix padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
        return {
            "logged_in":  True,
            "name":       claims.get("upn") or claims.get("unique_name") or claims.get("preferred_username") or claims.get("sub", ""),
            "display_name": claims.get("name", ""),
            "type":       "user" if claims.get("upn") else "app",
            "tenant_id":  claims.get("tid", ""),
            "tenant_name": claims.get("tenant_display_name", ""),
            "method":     "sdk_token",
        }
    except ImportError:
        return {"logged_in": False, "error": "Azure SDK not installed. Run: pip install azure-identity"}
    except Exception as exc:
        err = str(exc)
        if "CredentialUnavailableError" in err or "DefaultAzureCredential" in err:
            return {"logged_in": False, "error": "Not signed in. Use 'Sign in with Browser' or run 'az login'."}
        return {"logged_in": False, "error": f"Auth check failed: {err[:150]}"}


@router.post("/azure/browser-login")
def azure_browser_login() -> Dict[str, Any]:
    """Launch interactive browser login (Microsoft 'Pick an account' page).

    Opens the user's default browser for Azure AD authentication.
    The credential is cached in-process for subsequent API calls.
    Returns identity info + available subscriptions.
    """
    try:
        info = browser_login()
    except AzureConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    # After browser login, list subscriptions using the new credential
    subs: list[Dict[str, Any]] = []
    try:
        from azure.identity import InteractiveBrowserCredential
        from services.azure_monitor import _browser_credential
        if _browser_credential is not None:
            from azure.mgmt.subscription import SubscriptionClient
            client = SubscriptionClient(_browser_credential)
            for sub in client.subscriptions.list():
                state = str(getattr(sub, "state", "") or "")
                if state.lower() != "enabled":
                    continue
                subs.append({
                    "id": str(getattr(sub, "subscription_id", "") or ""),
                    "name": str(getattr(sub, "display_name", "") or ""),
                    "state": state,
                    "is_default": False,
                    "tenant_id": str(getattr(sub, "tenant_id", "") or ""),
                })
    except Exception as exc:
        info["subscription_error"] = str(exc)[:200]

    info["subscriptions"] = subs
    return info


@router.post("/azure/browser-logout")
def azure_browser_logout() -> Dict[str, Any]:
    """Clear cached browser credential."""
    clear_browser_credential()
    return {"ok": True, "message": "Browser credential cleared."}


@router.get("/azure/auth-status")
def azure_auth_status() -> Dict[str, Any]:
    """Return which auth method is active.

    Only reports 'connected' for EXPLICIT browser sign-in (or restored
    persistent cache).  Passive az-login / DefaultAzureCredential is
    NOT surfaced here — users must click 'Sign in with Browser' so the
    dashboard works identically on every machine.
    """
    browser_info = get_browser_credential_info()
    if browser_info.get("logged_in"):
        return {
            "method": "browser",
            "name": browser_info.get("name", ""),
            "display_name": browser_info.get("display_name", ""),
            "tenant_id": browser_info.get("tenant_id", ""),
        }
    return {"method": "none", "name": ""}


@router.get("/azure/subscriptions")
def azure_subscriptions() -> Dict[str, Any]:
    """List subscriptions using az CLI first, then SDK fallback."""
    try:
        import subprocess, json as _json
        proc = subprocess.run(
            ["az", "account", "list", "--output", "json", "--all"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            subs = _json.loads(proc.stdout)
            return {
                "ok": True,
                "subscriptions": [
                    {
                        "id": s.get("id", ""),
                        "name": s.get("name", ""),
                        "state": s.get("state", ""),
                        "is_default": s.get("isDefault", False),
                        "tenant_id": s.get("tenantId", ""),
                    }
                    for s in subs
                    if s.get("state") == "Enabled"
                ],
            }
        # CLI command failed; continue to SDK fallback below.
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        return {"ok": True, "subscriptions": _subscriptions_via_sdk()}
    except ImportError:
        return {
            "ok": False,
            "error": "Azure SDK not installed. Run: pip install azure-identity azure-mgmt-subscription",
            "subscriptions": [],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "subscriptions": []}


@router.get("/azure/resource-groups")
def azure_resource_groups(subscription_id: str = "") -> Dict[str, Any]:
    """List RGs using az CLI first, then SDK fallback."""
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
            capture_output=True, text=True, timeout=15,
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

    try:
        return {"ok": True, "resource_groups": _resource_groups_via_sdk(sub_id)}
    except ImportError:
        return {
            "ok": False,
            "error": "Azure SDK not installed. Run: pip install azure-identity azure-mgmt-resource",
            "resource_groups": [],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "resource_groups": []}


@router.post("/azure/validate")
def validate_azure(body: AzureValidateRequest) -> Dict[str, Any]:
    """
    Validate Azure connection using the user's az login identity.
    Attempts a lightweight VM list call to confirm auth + RBAC.
    Returns { valid: bool, vm_count_sample?: int, error?: str }
    """
    try:
        from azure.mgmt.compute import ComputeManagementClient
    except ImportError:
        return {
            "valid": False,
            "error": "Azure SDK not installed. Run: pip install azure-monitor-query azure-identity azure-mgmt-compute"
        }

    try:
        cred = _build_credential({})
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
def azure_discover_vms(body: AzureDiscoverRequest) -> Dict[str, Any]:
    """
    Discover VMs in a subscription, classify them as APP/DB/SRE,
    and return the list for user selection before fetching metrics.
    """
    cfg = dict(config_store.get_all())
    if body.subscription_id:
        cfg["azure_subscription_id"] = body.subscription_id.strip()
    rg = (body.resource_group or "").strip() or None

    try:
        vms = discover_vms(cfg, resource_group=rg)
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
def azure_search_vms(body: AzureSearchRequest) -> Dict[str, Any]:
    """
    Search for VMs across all subscriptions using Azure Resource Graph.
    Matches VM name, resource group, or any tag value (CustomerName,
    Application, Environment_Type, etc.).
    """
    q = (body.query or "").strip()
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Search query is required.")

    try:
        credential = _build_credential({})
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
def fetch_azure_resources(body: AzureFetchRequest) -> Dict[str, Any]:
    """
    Fetch VM metrics from Azure Monitor using the user's az login identity,
    then run them through resource_calculator to produce the standard
    Resource Review payload.
    """
    cfg = config_store.get_all()

    # Allow per-request resource group override
    if body.resource_group:
        cfg = dict(cfg)
        cfg["azure_resource_group"] = body.resource_group.strip()

    try:
        servers = fetch_vm_metrics(cfg, hours_back=body.hours_back,
                                   vm_ids=body.vm_ids)
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
async def fetch_azure_resources_stream(body: AzureFetchRequest):
    """
    Same as /azure/fetch-resources but streams SSE progress events.
    
    Events:
      event: progress   data: {"phase":"...", "done":N, "total":N}
      event: result     data: {full payload}
      event: error      data: {"detail":"..."}
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor, as_completed

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
            credential = _bc(cfg)

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


@router.post("/azure/timeseries")
def azure_timeseries(body: TimeseriesRequest) -> Dict[str, Any]:
    """
    Fetch time-series data + automatic spike detection for selected VMs.
    Returns only critical/significant findings — filters out normal and moderate.
    Includes pattern detection (recurring times, cross-VM correlation).
    """
    credential = _build_credential({})

    try:
        raw = fetch_vm_timeseries(credential, body.vm_ids, body.hours_back)
    except AzureConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Time-series fetch failed: {exc}") from exc

    result = raw["vms"]
    patterns = raw.get("patterns", [])
    baseline = raw.get("baseline", {})

    # ── Filter: keep only critical spikes (z ≥ 3σ or absolute breach), drop normal/moderate ──
    for vm_data in result.values():
        filtered_spikes = {}
        for metric, spike_list in vm_data.get("spikes", {}).items():
            critical_only = [s for s in spike_list if s["severity"] in ("critical", "critical_sustained")]
            if critical_only:
                filtered_spikes[metric] = critical_only
        vm_data["spikes"] = filtered_spikes

    # Build fleet heatmap data: for each time slot, aggregate CPU across all VMs
    all_timestamps = set()
    for vm_data in result.values():
        cpu_series = vm_data.get("series", {}).get("Percentage CPU", [])
        for p in cpu_series:
            all_timestamps.add(p["t"])

    sorted_times = sorted(all_timestamps)

    # Build heatmap matrix: rows=VMs, cols=time slots, values=CPU %
    heatmap = {
        "timestamps": sorted_times,
        "vms": [],
    }
    for vm_name, vm_data in result.items():
        cpu_map = {p["t"]: p["v"] for p in vm_data.get("series", {}).get("Percentage CPU", [])}
        row = [cpu_map.get(t) for t in sorted_times]
        heatmap["vms"].append({"name": vm_name, "values": row})

    # Count critical spikes only across fleet
    total_critical = 0
    affected_vms = set()
    for vm_name, vm_data in result.items():
        for metric_spikes in vm_data.get("spikes", {}).values():
            if metric_spikes:
                total_critical += len(metric_spikes)
                affected_vms.add(vm_name)

    return {
        "vms": result,
        "heatmap": heatmap,
        "patterns": patterns,
        "baseline": baseline,
        "summary": {
            "vm_count": len(result),
            "total_critical": total_critical,
            "affected_vms": len(affected_vms),
            "hours_back": body.hours_back,
        },
    }
