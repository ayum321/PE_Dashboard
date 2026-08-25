"""
Azure Monitor resource fetcher — session-scoped interactive browser identity.

Uses the azure-monitor-query + azure-identity SDK to pull CPU / Memory /
Disk metrics for all VMs in a given subscription + resource group, then
returns records in the same dict shape as resource_parser_generic.py so
they feed directly into resource_calculator.build_resource_payload().

Authentication:
    InteractiveBrowserCredential — the analyst explicitly signs in in their
    browser. Every data pull is tied to that browser session's Azure AD identity.

Public API
----------
    fetch_vm_metrics(config: dict, hours_back: int = 24) -> list[dict]
        Raises AzureConfigError if credentials are missing/invalid.
        Raises AzureFetchError  if the API call fails.

Required config keys (stored in .pe_config.json via Settings):
    azure_subscription_id — Target subscription to query
    azure_resource_group  — (optional) limit to one resource group

Required Azure RBAC:
    Your account must have 'Reader' + 'Monitoring Reader' roles
    on the target subscription.
"""
from __future__ import annotations

# ── Corporate machine fixes — must run BEFORE any azure/msal import ──────────
# Fix 1: Force IPv4 — corporate DNS returns only IPv6 for login.microsoftonline.com
# but IPv6 connectivity to Azure is broken → 83-180s timeouts without this patch.
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if family == 0:
        ipv4 = [r for r in results if r[0] == _socket.AF_INET]
        if ipv4:
            return ipv4
    return results
_socket.getaddrinfo = _ipv4_getaddrinfo

# Fix 2: platform.platform() and platform.uname() hang under corporate group policy
# (both call WMI). azure.identity calls platform.platform() at MODULE LOAD.
# msal/oauth2cli/authcode.py:63 calls platform.uname() for is_wsl detection.
import platform as _platform
_platform.platform = lambda aliased=False, terse=False: "Windows"
# uname_result constructor arity differs across Python versions (3.14 takes 5
# positional args, 'processor' is computed lazily) — probe instead of hardcoding.
_uname_stub = None
for _n in (5, 6):
    try:
        _uname_stub = _platform.uname_result(*(["Windows"] + [""] * (_n - 1)))
        break
    except TypeError:
        continue
if _uname_stub is not None:
    _platform.uname = lambda: _uname_stub

# Fix 3: msal_extensions DPAPI hang — FilePersistenceWithDataProtection → CryptProtectData
# hangs on Python 3.14 free-threaded. We bypass DPAPI with a plain file cache, BUT the
# stock FilePersistence opens the cache in text mode with no encoding → on Windows it uses
# cp1252 and CRASHES with "'charmap' codec can't decode byte 0x9d" when the file holds a
# stale DPAPI/binary blob or any non-cp1252 byte (the error our field users hit after
# signing in). Install a UTF-8, self-healing persistence: writes UTF-8, and on an
# undecodable/corrupt cache it deletes the file and reports "no cache" so MSAL starts
# clean instead of throwing. Done before any azure.identity import so the patch is in place.
try:
    import os as _os_persist
    import msal_extensions as _msal_ext
    from msal_extensions import FilePersistence as _FP
    from msal_extensions.persistence import (
        PersistenceNotFound as _PersistenceNotFound,
        _open as _persist_open,
    )

    class _SafeFilePersistence(_FP):
        """Plain-file token cache that always reads/writes UTF-8 and self-heals a
        corrupt/legacy cache instead of crashing the sign-in."""

        def save(self, content):
            with _os_persist.fdopen(_persist_open(self._location), "w+", encoding="utf-8") as handle:
                handle.write(content)

        def load(self):
            try:
                with open(self._location, "r", encoding="utf-8") as handle:
                    return handle.read()
            except FileNotFoundError:
                raise _PersistenceNotFound(
                    message="Persistence not initialized. You can recover by calling a save() first.",
                    location=self._location,
                )
            except (UnicodeDecodeError, ValueError):
                # Corrupt / legacy-binary cache — delete it and behave as "no cache"
                try:
                    _os_persist.remove(self._location)
                except OSError:
                    pass
                raise _PersistenceNotFound(
                    message="Persistence was corrupt and has been reset. Recover by calling save() first.",
                    location=self._location,
                )

    _msal_ext.FilePersistence = _SafeFilePersistence
    _msal_ext.FilePersistenceWithDataProtection = _SafeFilePersistence
    _msal_ext.PersistedTokenCache               # touch to confirm module loaded
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import sys as _sys
import threading as _threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.spike_schema import make_spike_record
from services.resource_severity import metric_profile, resolve_severity

logger = logging.getLogger("pe_dashboard.azure_monitor")


class AzureConfigError(Exception):
    """Raised when Azure credentials are missing or authentication fails."""


class AzureNetworkError(AzureConfigError):
    """Raised when the corporate network cannot reach the Microsoft login host."""


class AzureFetchError(Exception):
    """Raised when the Azure Monitor API call fails."""


class AzureTimeoutError(AzureFetchError):
    """Raised when a bounded Azure SDK operation exceeds its transport timeout."""


# ── Metric definitions ────────────────────────────────────────────────────────
# Azure Monitor metric names for VM insights.
# Percentage CPU is always available; Memory/Disk require the Azure Monitor
# Agent (AMA) or legacy MMA to be installed on the VM.
# Platform metrics — always available on running VMs
_VM_METRICS_PLATFORM = [
    "Percentage CPU",
    "Available Memory Bytes",
    "Available Memory Percentage",  # Direct % — no SKU lookup needed
]

# Disk metrics — try in order; not all VMs support all of these
_VM_METRICS_DISK = [
    "OS Disk Bandwidth Consumed Percentage",   # Preferred (available on most VMs)
    "Data Disk Bandwidth Consumed Percentage",
]

_VM_METRICS = _VM_METRICS_PLATFORM + _VM_METRICS_DISK

# ── Throughput / availability metrics (Azure Portal "Platform Metrics" parity) ─
# These mirror the panels an Azure/Grafana VM dashboard shows: disk bytes, disk
# ops/sec, network in/out, and the availability indicator.
#
# IMPORTANT: these are NOT percentages. They have no meaningful warn/crit band,
# so they are deliberately CHART-ONLY — excluded from spike classification and
# from findings. Feeding a byte counter through _classify_severity (whose
# fallback band is warn=80/crit=90) would grade every single datapoint
# "critical_sustained" because bytes are numerically enormous. Grading stays on
# the percentage metrics that have real thresholds in pe_config.
_VM_METRICS_THROUGHPUT = [
    "Disk Read Bytes",
    "Disk Write Bytes",
    "Disk Read Operations/Sec",
    "Disk Write Operations/Sec",
    "Network In Total",
    "Network Out Total",
]
_VM_METRICS_AVAILABILITY = [
    "VmAvailabilityMetric",   # "Availability (Preview)" in the Azure portal
]

# Metrics that are displayed but never spike-classified or graded.
_CHART_ONLY_METRICS = set(_VM_METRICS_THROUGHPUT) | set(_VM_METRICS_AVAILABILITY) | {
    "Available Memory Bytes",   # byte-scale twin of Available Memory Percentage
}

# Display units for the chart-only metrics, so the frontend can label axes
# without string-sniffing metric names.
_METRIC_UNITS = {
    "Disk Read Bytes":            "bytes",
    "Disk Write Bytes":           "bytes",
    "Network In Total":           "bytes",
    "Network Out Total":          "bytes",
    "Available Memory Bytes":     "bytes",
    "Disk Read Operations/Sec":   "ops",
    "Disk Write Operations/Sec":  "ops",
    "VmAvailabilityMetric":       "availability",
}

# Query groups. Azure fails the ENTIRE query_resource call if any one metric in
# the list is unsupported for that VM, so metrics are requested in small groups
# and each group is failure-isolated — a VM without AMA still returns CPU, and a
# VM that does not expose the availability metric still returns everything else.
# Keep the platform percentage metrics independently queryable.  Memory
# counters require AMA on many customer VMs, while Percentage CPU is available
# on every running VM.  Keeping them in one Azure query meant a VM without the
# memory counter lost its *CPU* series too, so the Fleet Heatmap rendered a
# misleading empty row instead of the full healthy CPU history.
#
# Each bucket below is deliberately a small failure-isolation boundary.  A
# missing optional metric must never suppress a different metric which the VM
# did emit.  The extra calls are bounded by the selected VM count and are worth
# the auditability gain: an empty heatmap cell now means "metric not emitted",
# never "a sibling metric was unsupported".
_TS_METRIC_GROUPS = [
    ["Percentage CPU"],
    ["Available Memory Percentage"],
    ["Available Memory Bytes"],
    ["OS Disk Bandwidth Consumed Percentage"],
    ["Data Disk Bandwidth Consumed Percentage"],
    _VM_METRICS_THROUGHPUT,
    _VM_METRICS_AVAILABILITY,
]

# Aggregation type for each metric
_METRIC_AGG = {
    "Percentage CPU":                         "Average",
    "Available Memory Bytes":                 "Average",
    "Available Memory Percentage":            "Average",
    "OS Disk Bandwidth Consumed Percentage":  "Average",
    "Data Disk Bandwidth Consumed Percentage":"Average",
}

# Total RAM in bytes — Azure doesn't expose this directly via Monitor.
# We read it from VM size metadata and use it to compute mem_used %.
# If unavailable, memory will be 0.0 (flagged as image_only=False, partial).
_BYTES_PER_GB = 1_073_741_824


def _require_sdk() -> None:
    """Check that azure packages are installed; raise a clear error if not."""
    try:
        import azure.identity          # noqa: F401
        import azure.monitor.query     # noqa: F401
    except ImportError:
        raise AzureConfigError(
            "Azure SDK not installed. Run: pip install azure-monitor-query azure-identity"
        )


# ── Per-session browser credentials (thread-safe registry) ────────────────────
# Credentials are scoped by session id (supplied by the HTTP layer from a
# first-party cookie) so concurrent analysts sharing one server process never
# overwrite or read each other's Azure identity/token. The "_default" bucket
# supports internal single-user callers that do not have an HTTP session. All
# access is guarded by a re-entrant lock.
_cred_lock = _threading.RLock()
_cred_sessions: dict = {}   # sid -> {"cred": <credential|None>, "info": {...}}
_login_locks: dict = {}     # sid -> Lock, serializes interactive browser launches
_DEFAULT_SID = "_default"

# Seconds to wait for the user to finish the interactive browser sign-in before
# giving up. The SDK default is 300s; we cap lower so a stalled loopback redirect
# (corporate proxy / stale tab / wrong browser profile) fails fast with a clear,
# retryable error instead of leaving the request — and the UI — hanging.
_BROWSER_AUTH_TIMEOUT_S = 180

def _sid_norm(session_id=None) -> str:
    sid = (session_id or "").strip()
    return sid or _DEFAULT_SID


# ── Thread-safe registry accessors (no network — pure in-memory state) ────────
def _get_cred(session_id=None):
    with _cred_lock:
        e = _cred_sessions.get(_sid_norm(session_id))
        return e.get("cred") if e else None


def _get_info(session_id=None) -> dict:
    with _cred_lock:
        e = _cred_sessions.get(_sid_norm(session_id))
        return dict(e.get("info") or {}) if e else {}


def _set_session(session_id, cred, info) -> None:
    with _cred_lock:
        _cred_sessions[_sid_norm(session_id)] = {"cred": cred, "info": dict(info or {})}


def _clear_session(session_id=None) -> None:
    with _cred_lock:
        _cred_sessions.pop(_sid_norm(session_id), None)


def _login_lock(session_id=None):
    """Return the per-session lock that makes browser sign-in single-flight."""
    sid = _sid_norm(session_id)
    with _cred_lock:
        return _login_locks.setdefault(sid, _threading.Lock())


def _preflight_auth_network(timeout: float = 6.0) -> None:
    """Quick DNS + TCP check for login.microsoftonline.com (IPv4, port 443).
    Raises AzureConfigError fast if network is down — avoids 180s SDK timeout."""
    import socket as _s
    try:
        addrs = _s.getaddrinfo("login.microsoftonline.com", 443,
                               _s.AF_INET, _s.SOCK_STREAM)
        if not addrs:
            raise AzureNetworkError("DNS lookup for login.microsoftonline.com returned no IPv4 addresses.")
        ip = addrs[0][4][0]
        sock = _s.create_connection((ip, 443), timeout=timeout)
        sock.close()
    except AzureNetworkError:
        raise
    except OSError as exc:
        raise AzureNetworkError(
            f"Cannot reach login.microsoftonline.com — check network/VPN. ({exc})"
        ) from exc


def browser_login(session_id=None) -> dict:
    """Launch interactive browser login and cache the credential for THIS process/session.

    Opens the Microsoft "Pick an account" page in the user's default
    browser. After successful sign-in the credential is held only in process
    under this session id; persistent token caches are intentionally disabled.

    Returns a dict with identity info (name, tenant, etc.).
    """
    _require_sdk()
    from azure.identity import InteractiveBrowserCredential
    import json as _json, base64

    # Fix 5: network preflight — fail fast (6s) instead of 180s timeout
    _preflight_auth_network()

    # Two browser clicks can arrive on separate FastAPI worker threads. Without
    # this single-flight lock both calls open an account-picker window. The
    # follower waits for the bounded first attempt, then reuses its credential.
    with _login_lock(session_id):
        existing = _get_cred(session_id)
        existing_info = _get_info(session_id)
        if existing is not None and existing_info.get("logged_in"):
            return existing_info

        logger.info("Azure auth: launching interactive browser login…")
        try:
            # Bound the interactive wait so a stalled loopback redirect fails
            # fast instead of hanging on the SDK's 300s default. Do not attach a
            # persistent token cache: DPAPI can hang on this supported build.
            cred = InteractiveBrowserCredential(timeout=_BROWSER_AUTH_TIMEOUT_S)
            cred.authenticate(scopes=["https://management.azure.com/.default"])
            token = cred.get_token("https://management.azure.com/.default")
        except Exception as exc:
            raise AzureConfigError(
                f"Browser login failed or was cancelled. Error: {exc}"
            ) from exc

        # Decode JWT to extract identity.
        try:
            payload_b64 = token.token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
            info = {
                "logged_in": True,
                "name":  claims.get("upn") or claims.get("unique_name") or claims.get("preferred_username") or "",
                "display_name": claims.get("name", ""),
                "tenant_id": claims.get("tid", ""),
                "method": "browser",
            }
        except Exception:
            info = {"logged_in": True, "name": "unknown", "method": "browser"}
        _set_session(session_id, cred, info)
        logger.info("Browser login succeeded: %s", info.get("name", "?"))
        return info


def get_browser_credential_info(session_id=None) -> dict:
    """Return this session's cached browser credential identity, or empty dict."""
    return _get_info(session_id)


def get_browser_credential(session_id=None):
    """Return this session's browser credential object, or None."""
    return _get_cred(session_id)


def clear_browser_credential(session_id=None) -> None:
    """Clear this session's in-memory browser credential on sign-out.

    Other sessions are unaffected. Browser credentials deliberately do not
    persist to disk, so a server restart requires an explicit new sign-in.
    """
    _clear_session(session_id)


def _build_credential(cfg: dict, session_id=None):
    """Return the explicitly authenticated browser credential for this session."""
    # Try this session's in-memory credential first
    cred = _get_cred(session_id)
    if cred is not None:
        logger.info("Azure auth: reusing cached browser credential")
        return cred
    raise AzureConfigError(
        "Not authenticated. Go to Settings → Sign in with Browser first."
    )


def _list_vms(credential, subscription_id: str, resource_group: Optional[str]) -> List[dict]:
    """
    List all VMs in the subscription (optionally filtered by resource group).
    Returns list of dicts with keys: resource_id, name, location, vm_size, rg, tags, power_state.
    """
    try:
        from azure.mgmt.compute import ComputeManagementClient
    except ImportError:
        raise AzureConfigError(
            "azure-mgmt-compute not installed. Run: pip install azure-mgmt-compute"
        )

    compute = ComputeManagementClient(credential, subscription_id)
    vms = []
    try:
        if resource_group:
            vm_list = compute.virtual_machines.list(resource_group)
        else:
            vm_list = compute.virtual_machines.list_all()

        for vm in vm_list:
            rg = vm.id.split("/resourceGroups/")[1].split("/")[0] if vm.id else ""
            tags = dict(vm.tags) if vm.tags else {}
            vms.append({
                "resource_id": vm.id,
                "name":        vm.name,
                "location":    vm.location or "",
                "vm_size":     (vm.hardware_profile.vm_size if vm.hardware_profile else "") or "",
                "rg":          rg,
                "tags":        tags,
            })
    except Exception as exc:
        raise AzureFetchError(f"Failed to list VMs: {exc}") from exc

    return vms


# ── Metrics cache: { (resource_id, hours_back) → (timestamp, metrics_dict) }
_metrics_cache: Dict[tuple, tuple] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _query_single_vm_metrics(client, rid, start_time, end_time, granularity):
    """Query metrics for a single VM. Returns (rid, metrics_dict).

    Each metric returns (all from ONE API call — Azure Monitor accepts multiple
    aggregation types per request, so this costs nothing extra over Average-only):
      metric_name          → overall period average (for CPU AVG column)
      metric_name__recent   → last data point / most recent hour (for CPU% column)
      metric_name__max      → true period MAXIMUM — the worst single bucket in the
                              whole window. A job that spikes CPU/mem/disk for 30min
                              out of a 15-day window gets averaged away to nothing;
                              this is what surfaces it so a PE lead can pick "Max"
                              instead of "Avg" and see the real peak, the same
                              Avg/Min/Max/Count choice Azure Metrics Explorer offers.
      metric_name__min      → true period MINIMUM — the best single bucket (for
                              memory, this is the highest-pressure point since the
                              raw metric is 'available %', inverted from used %).

    Strategy: try ALL metrics in a single API call first (fastest).
    If that fails, fall back to platform-only + individual disk queries.
    """
    from azure.monitor.query import MetricAggregationType
    import time as _t
    t0 = _t.perf_counter()
    vm_label = rid.split("/")[-1]
    metrics = {}
    _AGGS = [MetricAggregationType.AVERAGE, MetricAggregationType.MAXIMUM, MetricAggregationType.MINIMUM]

    def _extract(response):
        for m in response.metrics:
            avgs = [dp.average for ts in m.timeseries for dp in ts.data if dp.average is not None]
            maxs = [dp.maximum for ts in m.timeseries for dp in ts.data if dp.maximum is not None]
            mins = [dp.minimum for ts in m.timeseries for dp in ts.data if dp.minimum is not None]
            if avgs:
                metrics[m.name] = sum(avgs) / len(avgs)        # period average
                metrics[m.name + "__recent"] = avgs[-1]         # most recent data point
            if maxs:
                metrics[m.name + "__max"] = max(maxs)            # true period max
            if mins:
                metrics[m.name + "__min"] = min(mins)            # true period min

    # Fast path: all metrics in one call
    try:
        response = client.query_resource(
            resource_uri=rid,
            metric_names=list(_VM_METRICS),
            timespan=(start_time, end_time),
            granularity=granularity,
            aggregations=_AGGS,
        )
        _extract(response)
        logger.info("Metrics for %s (single call, %.1fs): %s", vm_label, _t.perf_counter() - t0, {k: v for k, v in metrics.items() if '__' not in k} or "EMPTY")
        return (rid, metrics)
    except Exception as exc:
        logger.debug("All-in-one metrics failed for %s: %s — falling back", vm_label, exc)

    # Fallback: platform metrics separately
    try:
        response = client.query_resource(
            resource_uri=rid,
            metric_names=list(_VM_METRICS_PLATFORM),
            timespan=(start_time, end_time),
            granularity=granularity,
            aggregations=_AGGS,
        )
        _extract(response)
    except Exception as exc:
        logger.warning("Platform metrics failed for %s: %s", vm_label, exc)

    # Fallback: disk metrics individually
    for disk_metric in _VM_METRICS_DISK:
        try:
            response = client.query_resource(
                resource_uri=rid,
                metric_names=[disk_metric],
                timespan=(start_time, end_time),
                granularity=granularity,
                aggregations=_AGGS,
            )
            _extract(response)
        except Exception:
            pass

    logger.info("Metrics for %s (fallback, %.1fs): %s", vm_label, _t.perf_counter() - t0, {k: v for k, v in metrics.items() if '__' not in k} or "EMPTY")
    return (rid, metrics)


def _query_metrics(
    credential,
    resource_ids: List[str],
    hours_back: int,
) -> Dict[str, Dict[str, float]]:
    """
    Query Azure Monitor for CPU / Memory / Disk metrics — PARALLEL.
    Uses ThreadPoolExecutor to query up to 10 VMs concurrently.
    Results are cached for 5 minutes.
    """
    from azure.monitor.query import MetricsQueryClient
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = MetricsQueryClient(credential)

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours_back)
    granularity = timedelta(hours=1)

    results: Dict[str, Dict[str, float]] = {}
    uncached_rids = []
    now = datetime.now(timezone.utc).timestamp()

    # Check cache first
    for rid in resource_ids:
        cache_key = (rid, hours_back)
        if cache_key in _metrics_cache:
            ts, cached_metrics = _metrics_cache[cache_key]
            if now - ts < _CACHE_TTL_SECONDS:
                results[rid] = cached_metrics
                logger.info("Metrics CACHE HIT for %s", rid.split("/")[-1])
                continue
        uncached_rids.append(rid)

    if not uncached_rids:
        return results

    # Query uncached VMs in parallel (max 20 concurrent)
    logger.info("Querying metrics for %d VMs in parallel (cached: %d)…",
                len(uncached_rids), len(results))

    workers = min(20, len(uncached_rids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_query_single_vm_metrics, client, rid,
                        start_time, end_time, granularity): rid
            for rid in uncached_rids
        }
        for future in as_completed(futures):
            try:
                rid, metrics = future.result()
                results[rid] = metrics
                # Cache the result
                _metrics_cache[(rid, hours_back)] = (now, metrics)
            except Exception as exc:
                rid = futures[future]
                logger.warning("Metrics failed for %s: %s", rid.split("/")[-1], exc)
                results[rid] = {}

    return results


# ── Time-series + spike detection ────────────────────────────────────────────

def _query_single_vm_timeseries(client, rid, start_time, end_time, granularity):
    """Query metrics time-series for a single VM.

    Returns ``(rid, series, true_extremes, series_max)``:
      - ``series``        {metric: [{t, v}]}  — AVERAGE aggregation, the chart line
      - ``true_extremes`` {metric: {true_max, true_min}} — accurate header stats
      - ``series_max``    {metric: [{t, v}]}  — MAXIMUM aggregation, per timestamp,
        retained for the "Average AND Maximum" overlay (graded metrics only)

    Metrics are requested in FAILURE-ISOLATED GROUPS (_TS_METRIC_GROUPS). Azure
    rejects the whole query_resource call if a single requested metric is not
    supported on that VM, so a flat one-shot request meant one unsupported disk
    or availability metric silently returned an EMPTY series dict for the VM —
    the deep dive would render nothing for it with only a log warning.
    """
    from azure.monitor.query import MetricAggregationType
    vm_label = rid.split("/")[-1]
    series = {}
    true_extremes = {}
    series_max = {}

    for group in _TS_METRIC_GROUPS:
        if not group:
            continue
        try:
            # Primary query: Average aggregation for chart line rendering
            response = client.query_resource(
                resource_uri=rid,
                metric_names=list(group),
                timespan=(start_time, end_time),
                granularity=granularity,
                aggregations=[MetricAggregationType.AVERAGE],
            )
            for m in response.metrics:
                points = []
                for ts in m.timeseries:
                    for dp in ts.data:
                        if dp.average is not None:
                            points.append({
                                "t": dp.timestamp.isoformat(),
                                "v": round(dp.average, 4),
                            })
                if points:
                    series[m.name] = points
        except Exception as exc:
            logger.warning("Time-series query failed for %s [%s]: %s",
                           vm_label, ", ".join(group), exc)

        try:
            # Secondary query: Max/Min aggregation for accurate header stats
            extremes_response = client.query_resource(
                resource_uri=rid,
                metric_names=list(group),
                timespan=(start_time, end_time),
                granularity=granularity,
                aggregations=[MetricAggregationType.MAXIMUM, MetricAggregationType.MINIMUM],
            )
            for m in extremes_response.metrics:
                max_val = None
                min_val = None
                # The MAXIMUM aggregate is already per-timestamp. It was being
                # collapsed straight to a scalar, discarding the series — which
                # is exactly the "Average AND Maximum" overlay the Azure
                # Platform Metrics dashboard draws. Retain it for the graded
                # percentage metrics so the chart can show how far the peak
                # inside each bucket ran above the bucket average; a 30-min
                # average of 55% hiding a 98% max is the single most common way
                # a real saturation event disappears from an averaged chart.
                max_points = []
                for ts in m.timeseries:
                    for dp in ts.data:
                        if dp.maximum is not None:
                            max_val = max(max_val, dp.maximum) if max_val is not None else dp.maximum
                            max_points.append({
                                "t": dp.timestamp.isoformat(),
                                "v": round(dp.maximum, 4),
                            })
                        if dp.minimum is not None:
                            min_val = min(min_val, dp.minimum) if min_val is not None else dp.minimum
                true_extremes[m.name] = {
                    "true_max": round(max_val, 4) if max_val is not None else None,
                    "true_min": round(min_val, 4) if min_val is not None else None,
                }
                if max_points and m.name not in _CHART_ONLY_METRICS:
                    series_max[m.name] = max_points
        except Exception as exc:
            logger.warning("Max/Min aggregation query failed for %s [%s]: %s",
                           vm_label, ", ".join(group), exc)

    return (rid, series, true_extremes, series_max)


def _percentile(values: list, pct: float) -> float:
    """Linear-interpolated percentile (numpy 'linear' / R-7 method).

    Replaces the crude ``sorted_v[int(n * pct)]`` index lookup, which for small
    samples collapses to the maximum (e.g. n=10, p95 → index 9 → the max value,
    mislabelled as a percentile). Interpolation gives a true percentile at any n.
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    if lo + 1 >= n:
        return float(s[-1])
    frac = rank - lo
    return float(s[lo] + frac * (s[lo + 1] - s[lo]))


def _metric_elevation(metric_label: str, is_db: bool = False) -> dict:
    """Single source of truth for 'is this metric elevated' bands.

    Reads the canonical CPU/MEM/DISK warn/crit thresholds from ``pe_config``
    (live — picks up Settings overrides after ``pe_config.reload()``), so the
    spike detector, per-VM hot-hours, and fleet hot-hours can no longer drift
    apart with parallel hardcoded tables.

    ``is_db`` selects the Oracle/DB memory band (``DB_MEM_WARN``/``DB_MEM_CRIT``)
    instead of the generic application band.

    Returns warn/crit in USED-% terms (higher = worse). ``invert`` marks metrics
    whose RAW samples are 'available %' (memory) — callers working in that space
    convert via ``100 - used``.
    """
    # Compatibility adapter for existing time-series code. The actual bands,
    # direction and DB expected-range suppression are owned by the shared
    # resolver used by Resource Review rows and fleet cards.
    profile = metric_profile(metric_label, "DB" if is_db else "APP")
    return {
        "metric": profile["metric"], "warn": profile["warn"], "crit": profile["crit"],
        "invert": profile["invert"], "role": profile["server_role"].lower(),
        "direction": profile["direction"], "raw_direction": profile["raw_direction"],
        "expected_min": profile.get("expected_min"), "expected_max": profile.get("expected_max"),
    }


def _abs_breach_cfg(metric_name: str, is_db: bool = False) -> dict | None:
    """Absolute-breach thresholds for the spike detector, derived from the same
    canonical ``pe_config`` bands as ``_metric_elevation`` (single source).

    Memory is detected in 'available %' space (lower = worse), so used→available
    is converted as ``100 - used``. ``min_minutes`` is the spike-duration gate and
    stays metric-specific (orthogonal to the elevation threshold).
    """
    band = _metric_elevation(metric_name, is_db=is_db)
    m = band["metric"]
    if m == "cpu":
        return {"critical": band["crit"], "warning": band["warn"], "min_minutes": 30}
    if m == "mem":
        return {"critical": 100.0 - band["crit"], "warning": 100.0 - band["warn"],
                "min_minutes": 30, "invert": True}
    if m == "disk":
        return {"critical": band["crit"], "warning": band["warn"], "min_minutes": 15}
    return None


def _classify_severity(used_peak: float, dur_min: int, z: float, z_crit: float,
                       band: dict) -> dict:
    """Two-gate severity: a statistical anomaly only escalates to warning/critical
    when its ABSOLUTE value is also operationally material. A z-score spike that
    is statistically unusual for a VM but trivial in absolute terms (e.g. 12% CPU
    on an idle box) is NOTABLE, never WARNING. ``used_peak`` is in used-% space
    (higher = worse), so memory must be pre-converted (100 - available).

    Returns a STRUCTURED dict so it's audit-defensible and machine-readable for
    later export into PE findings — never a freetext-only string:
      severity, reason_code (typed enum), severity_reason (human text),
      confidence, threshold (the band crossed), peak_pct, duration_min, z_score.
    """
    role = str(band.get("role") or "app").upper()
    result = resolve_severity(
        band.get("metric") or "other", used_peak, role,
        anomaly_result={"z": z, "z_critical": z_crit}, duration_min=dur_min,
    )
    confidence = "high" if result["severity"] in ("critical", "critical_sustained") else (
        "medium" if z >= 2.0 else "low"
    )
    pk, du, zr = round(float(used_peak), 1), int(dur_min), round(float(z), 1)
    threshold = result.get("threshold", band["warn"])
    if result["reason_code"] == "expected_range":
        reason = f"{pk:.0f}% is within the expected {result['expected_min']:.0f}–{result['expected_max']:.0f}% {role} range"
    elif result["reason_code"] == "stat_anomaly_immaterial":
        reason = f"statistical anomaly (z={zr}) but {pk:.0f}% < {threshold:.0f}% warn — not operationally material"
    elif result["severity"] == "critical_sustained":
        reason = f"{pk:.0f}% ≥ {threshold:.0f}% crit for {du}min"
    elif result["reason_code"] == "abs_crit_brief":
        reason = f"{pk:.0f}% ≥ {threshold:.0f}% crit but only {du}min — possible artifact"
    else:
        reason = f"{pk:.0f}% {'≥' if result['severity'] != 'notable' else '<'} {threshold:.0f}% {'crit' if result['severity'].startswith('critical') else 'warn'} band"
    return {
        "severity": result["severity"], "reason_code": result["reason_code"],
        "severity_reason": reason, "confidence": confidence, "threshold": threshold,
        "peak_pct": pk, "duration_min": du, "z_score": zr,
    }


def _detect_spikes(series_points: list, threshold_sigma: float = 2.0,
                   metric_name: str = "", is_db: bool = False) -> list:
    """Detect spikes in a time-series using DUAL classifiers:
    
    Classifier 1: Z-score (catches sudden deviations from server's own baseline)
    Classifier 2: Absolute threshold breach (catches sustained chronic conditions)
    
    Severity incorporates duration as a multiplier:
      z≥σ + duration <5min  → WARNING (possible artifact)
      z≥σ + duration 5-30min → CRITICAL
      z≥σ + duration >30min  → CRITICAL_SUSTAINED
    
    Uses metric-specific σ thresholds:
      CPU (high natural variance): z≥2.5
      Memory Available (inverted): z≥2.5 for LOW available
      Disk BW% (near-zero baseline): z≥4.0 to avoid noise
      Default: z≥3.0
    
    Returns list of spike events:
    [{start, end, peak, peak_time, duration_min, severity, z_score, detection}, ...]
    """
    if not series_points or len(series_points) < 3:
        return []

    vals = [p["v"] for p in series_points]
    n = len(vals)
    mean = sum(vals) / n
    variance = sum((v - mean) ** 2 for v in vals) / n
    std = variance ** 0.5

    # Metric-specific z-score thresholds. These now ACTUALLY gate detection
    # (see `eff_sigma` below). Previously `z_critical` was computed here but
    # only passed to _classify_severity for the confidence label, while the
    # detection loop always compared against the default threshold_sigma=2.0 —
    # so the documented per-metric sigmas (and the disk noise suppression they
    # exist for) were never in effect.
    mn = (metric_name or "").lower()
    if "cpu" in mn:
        z_critical = 2.5   # CPU has natural batch variance
    elif "memory" in mn or "mem" in mn:
        z_critical = 2.5   # Memory available dips are significant
    elif "disk" in mn:
        z_critical = 4.0   # Near-zero baseline → high z from tiny changes
    else:
        z_critical = 3.0

    # Detection gate: honour the metric-specific sigma, but never LOOSER than
    # the caller-supplied threshold_sigma (so an explicit stricter request from
    # a caller still wins).
    eff_sigma = max(float(threshold_sigma), z_critical)

    # Absolute thresholds (Classifier 2) — chronic breach detection.
    # Sourced from the canonical pe_config bands via _abs_breach_cfg, so the
    # spike detector, per-VM hot-hours, and fleet hot-hours all read ONE shared
    # threshold set instead of three parallel hardcoded tables.
    abs_cfg = _abs_breach_cfg(metric_name, is_db=is_db)
    band = _metric_elevation(metric_name, is_db=is_db)   # used-% warn/crit for the abs-significance gate

    spikes = []

    # ── Classifier 1: Z-score spike detection ──
    # For "Available Memory %", a SPIKE is a DROP (negative z).
    # For CPU/Disk, a SPIKE is a RISE (positive z).
    #
    # Orientation is read from the band rather than re-derived by substring.
    # `_metric_elevation` already owns this decision and exports it as `invert`,
    # but that flag had no consumer — every call site pattern-matched the metric
    # name again, so there were three independent copies of "is this metric
    # inverted?" that could drift apart. The band is now the single source, and
    # `_abs_breach_cfg` (below) derives its own `invert` from the same place.
    is_inverted_metric = bool(band.get("invert"))
    if std >= 0.001:
        in_spike = False
        spike_start = None
        spike_peak = 0
        spike_peak_time = ""
        spike_z = 0

        for i, p in enumerate(series_points):
            z = (p["v"] - mean) / std
            # For inverted metrics, detect negative z (value dropped below baseline)
            effective_z = -z if is_inverted_metric else z
            if effective_z >= eff_sigma:
                if not in_spike:
                    in_spike = True
                    spike_start = p["t"]
                    spike_peak = p["v"]
                    spike_peak_time = p["t"]
                    spike_z = effective_z
                else:
                    # Track worst point: for inverted metrics, lower = worse
                    if is_inverted_metric:
                        if p["v"] < spike_peak:
                            spike_peak = p["v"]
                            spike_peak_time = p["t"]
                            spike_z = effective_z
                    else:
                        if p["v"] > spike_peak:
                            spike_peak = p["v"]
                            spike_peak_time = p["t"]
                            spike_z = effective_z
            else:
                if in_spike:
                    from datetime import datetime as _dt
                    try:
                        t0 = _dt.fromisoformat(spike_start.replace("Z", "+00:00"))
                        t1 = _dt.fromisoformat(series_points[i - 1]["t"].replace("Z", "+00:00"))
                        dur_min = max(1, round((t1 - t0).total_seconds() / 60))
                    except Exception:
                        dur_min = 0

                    # Two-gate severity: z-score selects the spike, absolute value
                    # sets the label. used-% = peak for CPU/disk, 100-peak for mem.
                    used_peak = (100.0 - spike_peak) if is_inverted_metric else spike_peak
                    sv = _classify_severity(used_peak, dur_min, spike_z, z_critical, band)

                    spikes.append(make_spike_record(
                        start=spike_start, end=series_points[i - 1]["t"],
                        peak=round(spike_peak, 2), peak_time=spike_peak_time,
                        duration_min=dur_min, severity=sv["severity"],
                        reason_code=sv["reason_code"], severity_reason=sv["severity_reason"],
                        confidence=sv["confidence"], detection="z_score",
                        z_score=round(spike_z, 2), mean=round(mean, 2), std=round(std, 2),
                        threshold=sv.get("threshold"), peak_pct=sv.get("peak_pct"),
                    ))
                    in_spike = False

        # Close any open spike at end of series
        if in_spike:
            from datetime import datetime as _dt
            try:
                t0 = _dt.fromisoformat(spike_start.replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(series_points[-1]["t"].replace("Z", "+00:00"))
                dur_min = max(1, round((t1 - t0).total_seconds() / 60))
            except Exception:
                dur_min = 0
            used_peak = (100.0 - spike_peak) if is_inverted_metric else spike_peak
            sv = _classify_severity(used_peak, dur_min, spike_z, z_critical, band)
            spikes.append(make_spike_record(
                start=spike_start, end=series_points[-1]["t"],
                peak=round(spike_peak, 2), peak_time=spike_peak_time,
                duration_min=dur_min, severity=sv["severity"],
                reason_code=sv["reason_code"], severity_reason=sv["severity_reason"],
                confidence=sv["confidence"], detection="z_score",
                z_score=round(spike_z, 2), mean=round(mean, 2), std=round(std, 2),
                threshold=sv.get("threshold"), peak_pct=sv.get("peak_pct"),
            ))

    # ── Classifier 2: Absolute threshold breach detection ──
    # Catches chronically sick servers that z-score misses
    if abs_cfg:
        is_inverted = abs_cfg.get("invert", False)
        crit_thresh = abs_cfg["critical"]
        warn_thresh = abs_cfg["warning"]
        min_dur = abs_cfg["min_minutes"]

        in_breach = False
        breach_start = None
        breach_peak = 0
        breach_peak_time = ""
        breach_severity = "warning"

        for i, p in enumerate(series_points):
            # For inverted metrics (memory available), BELOW threshold = breach
            is_critical = (p["v"] <= crit_thresh) if is_inverted else (p["v"] >= crit_thresh)
            is_warning = (p["v"] <= warn_thresh) if is_inverted else (p["v"] >= warn_thresh)

            if is_warning:
                if not in_breach:
                    in_breach = True
                    breach_start = p["t"]
                    breach_peak = p["v"]
                    breach_peak_time = p["t"]
                    breach_severity = "critical" if is_critical else "warning"
                else:
                    # Track worst point (lowest for inverted, highest for normal)
                    if is_inverted:
                        if p["v"] < breach_peak:
                            breach_peak = p["v"]
                            breach_peak_time = p["t"]
                    else:
                        if p["v"] > breach_peak:
                            breach_peak = p["v"]
                            breach_peak_time = p["t"]
                    if is_critical:
                        breach_severity = "critical"
            else:
                if in_breach:
                    from datetime import datetime as _dt
                    try:
                        t0 = _dt.fromisoformat(breach_start.replace("Z", "+00:00"))
                        t1 = _dt.fromisoformat(series_points[i - 1]["t"].replace("Z", "+00:00"))
                        dur_min = max(1, round((t1 - t0).total_seconds() / 60))
                    except Exception:
                        dur_min = 0

                    if dur_min >= min_dur:
                        # Check overlap with z-score spikes — don't double-count
                        overlaps = any(
                            s["start"] <= breach_start and s["end"] >= series_points[i-1]["t"]
                            for s in spikes
                        )
                        if not overlaps:
                            sev = "critical_sustained" if dur_min > 60 else breach_severity
                            used_pk = (100.0 - breach_peak) if is_inverted else breach_peak
                            # z_score must be SEVERITY-oriented (higher = worse) to
                            # match classifier-1's `effective_z`. For an inverted
                            # metric the breach peak is the LOWEST value, so the raw
                            # z is negative; negate it. Consumers rank with
                            # max(events, key=z_score) and gate on `z >= 3.0`, both
                            # of which were unreachable for memory before this.
                            _raw_z = ((breach_peak - mean) / std) if std > 0.001 else 0.0
                            _sev_z = -_raw_z if is_inverted else _raw_z
                            spikes.append(make_spike_record(
                                start=breach_start, end=series_points[i - 1]["t"],
                                peak=round(breach_peak, 2), peak_time=breach_peak_time,
                                duration_min=dur_min, severity=sev,
                                reason_code="abs_sustained" if dur_min > 60 else "abs_breach",
                                severity_reason=f"sustained absolute breach {dur_min}min ≥ {min_dur}min",
                                confidence="high", detection="absolute_threshold",
                                z_score=round(_sev_z, 2),
                                mean=round(mean, 2), std=round(std, 2),
                                threshold=crit_thresh if breach_severity == "critical" else warn_thresh,
                                peak_pct=round(used_pk, 1),
                            ))
                    in_breach = False

        # Close open breach at end of series
        if in_breach:
            from datetime import datetime as _dt
            try:
                t0 = _dt.fromisoformat(breach_start.replace("Z", "+00:00"))
                t1 = _dt.fromisoformat(series_points[-1]["t"].replace("Z", "+00:00"))
                dur_min = max(1, round((t1 - t0).total_seconds() / 60))
            except Exception:
                dur_min = 0
            if dur_min >= min_dur:
                overlaps = any(
                    s["start"] <= breach_start and s["end"] >= series_points[-1]["t"]
                    for s in spikes
                )
                if not overlaps:
                    sev = "critical_sustained" if dur_min > 60 else breach_severity
                    used_pk = (100.0 - breach_peak) if is_inverted else breach_peak
                    # Severity-oriented z (see the mid-loop close above).
                    _raw_z = ((breach_peak - mean) / std) if std > 0.001 else 0.0
                    _sev_z = -_raw_z if is_inverted else _raw_z
                    spikes.append(make_spike_record(
                        start=breach_start, end=series_points[-1]["t"],
                        peak=round(breach_peak, 2), peak_time=breach_peak_time,
                        duration_min=dur_min, severity=sev,
                        reason_code="abs_sustained" if dur_min > 60 else "abs_breach",
                        severity_reason=f"sustained absolute breach {dur_min}min ≥ {min_dur}min",
                        confidence="high", detection="absolute_threshold",
                        z_score=round(_sev_z, 2),
                        mean=round(mean, 2), std=round(std, 2),
                        threshold=crit_thresh if breach_severity == "critical" else warn_thresh,
                        peak_pct=round(used_pk, 1),
                    ))

    # Expected-range events can be statistically unusual, but they are not
    # actionable anomalies. Filtering them here keeps the detector output in
    # lockstep with the fleet cards rather than relying on every renderer to
    # remember that special case.
    return [spike for spike in spikes if spike.get("severity") != "healthy"]


def detect_regime_change(recent_baseline: dict, prior_baseline: dict,
                         k: float = 2.0) -> dict:
    """Pure two-window step-change test. Compares recent vs prior pooled (mean,std);
    flags a regime shift when the gap exceeds k pooled σ. No DB, no side effects.
    Returns {detected, delta_sigma, direction, mean_recent, mean_prior}."""
    mr, sr = float(recent_baseline["mean"]), float(recent_baseline["std"])
    mp, sp = float(prior_baseline["mean"]), float(prior_baseline["std"])
    pooled = ((sr * sr + sp * sp) / 2.0) ** 0.5
    delta_sigma = round((mr - mp) / pooled, 2) if pooled else 0.0
    return {
        "detected": abs(delta_sigma) >= k,
        "delta_sigma": delta_sigma,
        "direction": "up" if mr >= mp else "down",
        "mean_recent": round(mr, 1),
        "mean_prior": round(mp, 1),
    }


# ── Waveform (signal shape) classification ───────────────────────────────────
# Catalogue of shapes the deep-dive "Signal Pattern Analysis" panel renders.
# Keys MUST stay in sync with the secondary-shape catalogue in static/app.js.
_WAVEFORM_CATALOG = {
    "sawtooth": {
        "label": "Cyclic Load", "icon": "⚡",
        "meaning": "Regular rise-and-fall cycles — the signature of scheduled batch work rather than organic user load.",
        "action": "Map the cycle period against the Ctrl-M schedule; stagger overlapping jobs if peaks collide.",
    },
    "diurnal": {
        "label": "Daily Cycle", "icon": "🌓",
        "meaning": "A repeating ~24h pattern — load tracks the business day or a nightly batch window.",
        "action": "Size capacity for the daily peak, not the daily mean. Confirm the peak window is the intended one.",
    },
    "trending_up": {
        "label": "Trending ↑", "icon": "📈",
        "meaning": "The baseline itself is climbing across the observation window — this is growth, not a spike.",
        "action": "Identify the growth driver (data volume, new jobs, plan regression) before it reaches the critical band.",
    },
    "random_spikes": {
        "label": "Irregular Spikes", "icon": "🎯",
        "meaning": "Sharp excursions with no repeating period — ad-hoc jobs, retries, or an unpredictable workload.",
        "action": "Correlate individual spikes with batch runs; an unattributed spike usually means an unscheduled process.",
    },
    "plateau": {
        "label": "Sustained Load", "icon": "▬",
        "meaning": "Consistently elevated with little variation — the resource is held at a high level, not spiking to it.",
        "action": "This is a sizing problem, not a scheduling one. Schedule changes will not move a plateau.",
    },
    "change_point": {
        "label": "Regime Shift", "icon": "⚠️",
        "meaning": "The signal stepped to a new level part-way through the window and stayed there.",
        "action": "Find what changed at the step: a deployment, config change, data load, or new job.",
    },
    "weekend_dip": {
        "label": "Weekday-Driven", "icon": "📅",
        "meaning": "Clearly lower at weekends — load is driven by weekday business or weekday batch.",
        "action": "Weekday peaks govern sizing. Weekend headroom is not spare capacity for weekday work.",
    },
    "flat_low": {
        "label": "Flat / Low", "icon": "✅",
        "meaning": "Stable and well within band across the whole window.",
        "action": "No action. Candidate for right-sizing review if consistently this low.",
    },
}


def _linreg_slope(vals: list) -> float:
    """Least-squares slope over evenly-indexed samples. 0.0 when undefined."""
    n = len(vals)
    if n < 3:
        return 0.0
    mx = (n - 1) / 2.0
    my = sum(vals) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    if sxx <= 0:
        return 0.0
    sxy = sum((i - mx) * (v - my) for i, v in enumerate(vals))
    return sxy / sxx


def _autocorr(vals: list, lag: int) -> float:
    """Pearson autocorrelation at a given lag. 0.0 when undefined."""
    n = len(vals)
    if lag <= 0 or n <= lag + 2:
        return 0.0
    a = vals[:-lag]
    b = vals[lag:]
    m = len(a)
    ma = sum(a) / m
    mb = sum(b) / m
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((x - mb) ** 2 for x in b)
    if saa <= 1e-9 or sbb <= 1e-9:
        return 0.0
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return sab / ((saa * sbb) ** 0.5)


def _classify_waveform(points: list, metric_name: str, is_db: bool,
                       grain_minutes: float) -> Optional[Dict[str, Any]]:
    """Classify the SHAPE of a metric's time-series.

    Feeds the deep dive's "Signal Pattern Analysis" panel, which previously had
    no producer at all — the entire section was unreachable UI.

    Everything here is computed in USED-% space (higher = worse) so that a
    memory series (delivered by Azure as *available* %) is judged with the same
    comparisons as CPU and disk. The inversion happens exactly once, on entry.

    Returns None for metrics that are not percentages, or when there is too
    little data to say anything defensible.
    """
    if metric_name in _CHART_ONLY_METRICS:
        return None
    if not points or len(points) < 8:
        return None

    band = _metric_elevation(metric_name, is_db=is_db)
    inverted = bool(band.get("invert"))
    warn, crit = float(band["warn"]), float(band["crit"])

    parsed = []
    for p in points:
        try:
            t = datetime.fromisoformat(str(p["t"]).replace("Z", "+00:00"))
        except Exception:
            continue
        v = p.get("v")
        if v is None:
            continue
        parsed.append({"t": t, "v": (100.0 - float(v)) if inverted else float(v)})
    if len(parsed) < 8:
        return None
    parsed.sort(key=lambda d: d["t"])

    vals = [d["v"] for d in parsed]
    n = len(vals)
    mean_v = sum(vals) / n
    std_v = (sum((v - mean_v) ** 2 for v in vals) / n) ** 0.5
    peak_v = max(vals)
    cv = (std_v / mean_v) if mean_v > 0.5 else 0.0

    # Samples per hour / per day at this grain — drives the periodicity lags.
    gm = max(1.0, float(grain_minutes))
    per_hour = 60.0 / gm
    lag_day = int(round(24 * per_hour))

    # ── Feature extraction ────────────────────────────────────────────────
    # Peaks: local maxima that clear one sigma above the mean.
    thr_peak = mean_v + std_v
    peak_count = 0
    for i in range(1, n - 1):
        if vals[i] > thr_peak and vals[i] >= vals[i - 1] and vals[i] > vals[i + 1]:
            peak_count += 1

    slope = _linreg_slope(vals)                  # units per sample
    slope_per_day = slope * per_hour * 24.0

    # Periodicity MUST be measured on DETRENDED residuals. A pure linear ramp
    # has autocorrelation ≈ 1 at every lag, so a trending signal would otherwise
    # be misread as "diurnal" (verified: a 20→85 ramp classified as diurnal
    # before this). Removing the least-squares trend first isolates the cyclic
    # component from the growth component.
    _mx = (n - 1) / 2.0
    _my = mean_v
    resid = [v - (_my + slope * (i - _mx)) for i, v in enumerate(vals)]

    ac_day = _autocorr(resid, lag_day) if n > lag_day + 2 else 0.0

    # Best short-period autocorrelation (2h..12h) → cyclic/batch signature.
    ac_short, ac_short_lag = 0.0, 0
    for hrs in (2, 3, 4, 6, 8, 12):
        lag = int(round(hrs * per_hour))
        if lag < 2 or n <= lag + 2:
            continue
        a = _autocorr(resid, lag)
        if a > ac_short:
            ac_short, ac_short_lag = a, hrs

    # Change point: largest split-half mean gap in pooled-sigma units.
    cp_idx, cp_delta, before_mean, after_mean = None, 0.0, None, None
    if n >= 16:
        for i in range(max(4, n // 8), n - max(4, n // 8)):
            a, b = vals[:i], vals[i:]
            ma, mb = sum(a) / len(a), sum(b) / len(b)
            va = sum((x - ma) ** 2 for x in a) / len(a)
            vb = sum((x - mb) ** 2 for x in b) / len(b)
            pooled = ((va + vb) / 2.0) ** 0.5
            if pooled <= 0.001:
                continue
            d = abs(mb - ma) / pooled
            if d > cp_delta:
                cp_idx, cp_delta, before_mean, after_mean = i, d, round(ma, 1), round(mb, 1)

    # Weekday vs weekend
    wd = [d["v"] for d in parsed if d["t"].weekday() < 5]
    we = [d["v"] for d in parsed if d["t"].weekday() >= 5]
    wd_avg = (sum(wd) / len(wd)) if wd else None
    we_avg = (sum(we) / len(we)) if we else None

    # Time above the warn band
    above = sum(1 for v in vals if v >= warn)
    duration_above_hrs = round(above * gm / 60.0, 1)
    recurrence_days = len({d["t"].date() for d in parsed if d["v"] >= warn})

    # ── Shape decision (ordered by specificity) ───────────────────────────
    shapes = []
    if cp_idx is not None and cp_delta >= 2.5 and abs((after_mean or 0) - (before_mean or 0)) >= 5:
        shapes.append(("change_point", min(0.95, 0.45 + cp_delta / 12.0)))
    # A short cycle (e.g. 6h batch) also correlates at 24h because 24 is a
    # multiple of it. When both fire, the SHORTER period is the more specific —
    # and more actionable — explanation, so it must outrank "diurnal".
    _short_wins = ac_short >= 0.40 and ac_short >= (ac_day - 0.05)
    if ac_day >= 0.45 and n > lag_day + 2 and not _short_wins:
        shapes.append(("diurnal", min(0.95, 0.40 + ac_day * 0.55)))
    if ac_short >= 0.40 and cv >= 0.20:
        shapes.append(("sawtooth", min(0.95, 0.35 + ac_short * 0.55)))
    if slope_per_day >= 1.0 and abs(slope_per_day) * (n * gm / 1440.0) >= 4:
        shapes.append(("trending_up", min(0.92, 0.40 + min(slope_per_day, 10.0) / 20.0)))
    if wd_avg is not None and we_avg is not None and we and wd and (wd_avg - we_avg) >= max(8.0, 0.25 * wd_avg):
        shapes.append(("weekend_dip", min(0.90, 0.45 + (wd_avg - we_avg) / 60.0)))
    if mean_v >= warn and cv <= 0.18:
        shapes.append(("plateau", min(0.95, 0.55 + (mean_v - warn) / 40.0)))
    if peak_count >= 3 and cv >= 0.30:
        shapes.append(("random_spikes", min(0.85, 0.35 + peak_count / 30.0)))
    if not shapes:
        if mean_v < warn * 0.6 and cv <= 0.25:
            shapes.append(("flat_low", 0.75))
        else:
            shapes.append(("random_spikes", 0.45))

    shapes.sort(key=lambda s: -s[1])
    shape, confidence = shapes[0]
    secondary_shape = shapes[1][0] if len(shapes) > 1 else None

    # ── Risk: SHAPE modulates, ABSOLUTE LEVEL decides. A cyclic pattern that
    # never leaves the healthy band is not a risk; a plateau inside the critical
    # band is. This mirrors the two-gate rule used by _classify_severity.
    if peak_v >= crit and duration_above_hrs >= 1:
        risk = "critical" if shape in ("plateau", "trending_up", "change_point") else "high"
    elif peak_v >= crit:
        risk = "high"
    elif mean_v >= warn or peak_v >= warn:
        risk = "high" if shape in ("plateau", "trending_up") else "medium"
    elif shape == "flat_low":
        risk = "none"
    else:
        risk = "low"

    conf_label = ("observed" if confidence >= 0.75
                  else "inferred" if confidence >= 0.55 else "weak-signal")

    meta = _WAVEFORM_CATALOG[shape]
    detail_note = ""
    if shape == "sawtooth" and ac_short_lag:
        detail_note = f" Dominant cycle ≈ {ac_short_lag}h."
    elif shape == "trending_up":
        detail_note = f" Rising ≈ {slope_per_day:.1f}pp/day."
    elif shape == "change_point" and before_mean is not None:
        detail_note = f" Level stepped {before_mean:.0f}% → {after_mean:.0f}%."
    elif shape == "weekend_dip" and wd_avg is not None:
        detail_note = f" Weekday avg {wd_avg:.0f}% vs weekend {we_avg:.0f}%."

    return {
        "shape": shape,
        "secondary_shape": secondary_shape,
        "label": meta["label"],
        "icon": meta["icon"],
        "meaning": meta["meaning"] + detail_note,
        "action": meta["action"],
        "risk": risk,
        "confidence": round(confidence, 2),
        "confidence_label": conf_label,
        "recurrence_days": recurrence_days,
        "duration_above_threshold_hrs": duration_above_hrs,
        "details": {
            "peak_used_pct": round(peak_v, 1),
            "mean_used_pct": round(mean_v, 1),
            "headroom_pct": round(max(0.0, 100.0 - peak_v), 1),
            "peak_count": peak_count,
            "cv": round(cv, 3),
            "slope_pp_per_day": round(slope_per_day, 2),
            "autocorr_24h": round(ac_day, 2),
            "autocorr_short": round(ac_short, 2),
            "cycle_hours": ac_short_lag or None,
            "change_point_idx": cp_idx if (shape == "change_point") else None,
            "before_mean": before_mean if (shape == "change_point") else None,
            "after_mean": after_mean if (shape == "change_point") else None,
            "warn_band": warn,
            "crit_band": crit,
            "band_role": band.get("role", "app"),
            "samples": n,
        },
    }


def _detect_patterns(all_vm_spikes: Dict[str, Dict[str, list]], hours_back: int = 24) -> list:
    """Detect recurring and cross-VM patterns from spike data.

    Looks for:
    - Recurring time-of-day spikes on the same VM
    - Correlated spikes across multiple VMs at the same time
    - Sustained high-utilization periods

    Returns list of pattern objects with type, description, severity, affected VMs.
    """
    from datetime import datetime as _dt
    from collections import defaultdict
    from services import pe_config

    patterns = []
    days_observed = max(1.0, hours_back / 24.0)
    min_occ = pe_config.PATTERN_MIN_OCCURRENCES
    min_ratio = pe_config.PATTERN_MIN_RATIO
    # ── 1. Time-of-day clustering: spikes at similar hours across days ──
    for vm_name, metric_spikes in all_vm_spikes.items():
        hour_hits = defaultdict(list)   # hour -> list of spike events
        for metric, spikes in metric_spikes.items():
            for s in spikes:
                try:
                    t = _dt.fromisoformat(s["peak_time"].replace("Z", "+00:00"))
                    hour_hits[t.hour].append({**s, "metric": metric, "vm": vm_name, "_day": t.date()})
                except Exception:
                    pass
        for hour, events in hour_hits.items():
            # Recurrence evidence floor: count DISTINCT days the hour spiked, not
            # raw events (multiple metrics on one day are one occurrence). Fire
            # only on ≥ min_occ distinct days AND ≥ min_ratio of days observed —
            # both gates, so a sparse weekly pattern fires but a 2-day fluke does
            # not. Ratio is surfaced so a PE lead can judge confidence directly.
            day_count = len({e["_day"] for e in events})
            # `day_count` counts DISTINCT UTC CALENDAR days, but `days_observed`
            # is a duration in days — a 24h window straddling UTC midnight spans
            # 2 calendar days, giving day_count=2 / days_observed=1.0 and a title
            # reading "(2/1 days, 200%)". Use the calendar-day span the window
            # can actually cover as the denominator and clamp the ratio at 1.0.
            days_span = max(days_observed, float(day_count))
            ratio = min(1.0, day_count / days_span)
            if day_count >= min_occ and ratio >= min_ratio:
                metrics_hit = list({e["metric"] for e in events})
                worst = max(events, key=lambda e: e["z_score"])
                pct = round(ratio * 100)
                patterns.append({
                    "type": "recurring_time",
                    "severity": "critical" if worst["z_score"] >= 3.0 else "high",
                    "title": f"Recurring spikes at ~{hour:02d}:00 on {vm_name} ({day_count}/{round(days_span)} days, {pct}%)",
                    "description": (
                        f"Spikes recurred on {day_count} distinct days "
                        f"({pct}% of {round(days_span)} days observed) around {hour:02d}:00 UTC "
                        f"across {', '.join(metrics_hit)}. "
                        f"Peak {worst['peak']}% (z={worst['z_score']}). "
                        f"Indicates a scheduled job or periodic load trigger."
                    ),
                    "vms": [vm_name],
                    "hour": hour,
                    "count": len(events),
                    "recurrence_days": day_count,
                    "recurrence_ratio": round(ratio, 2),
                    "peak_z": worst["z_score"],
                    "peak": worst["peak"],
                    # Context for a small absolute peak (e.g. 0.01% on a metric
                    # whose own baseline is ~0.002%). Without this a PE lead
                    # reasonably reads "peak 0.01%" as a data error rather than
                    # a legitimate statistical outlier for THIS VM's own scale.
                    "peak_mean": worst.get("mean"),
                    "peak_detection": worst.get("detection"),
                    "metrics": metrics_hit,
                })

    # ── 2. Cross-VM correlation: spikes on different VMs within ±15 min ──
    all_spike_events = []
    for vm_name, metric_spikes in all_vm_spikes.items():
        for metric, spikes in metric_spikes.items():
            for s in spikes:
                try:
                    t = _dt.fromisoformat(s["peak_time"].replace("Z", "+00:00"))
                    all_spike_events.append({
                        "vm": vm_name, "metric": metric,
                        "ts": t, "spike": s,
                    })
                except Exception:
                    pass

    if len(all_spike_events) >= 2:
        all_spike_events.sort(key=lambda e: e["ts"])
        # Chain clustering: group spikes where each is within 15 min of the
        # PREVIOUS clustered event (not the anchor). Anchor-relative windows
        # truncate slow-rolling incidents — e.g. A→B→C spiking 10 min apart
        # span 20 min end-to-end, so C would fall outside a 15-min window from
        # anchor A even though it is only 10 min from B. Chaining follows the
        # rolling edge; the distinct-VM gate below still requires ≥2 VMs.
        clusters = []
        used = set()
        for i, ev in enumerate(all_spike_events):
            if i in used:
                continue
            cluster = [ev]
            used.add(i)
            last_ts = ev["ts"]
            for j in range(i + 1, len(all_spike_events)):
                if j in used:
                    continue
                delta = (all_spike_events[j]["ts"] - last_ts).total_seconds()
                if delta <= 900:  # within 15 min of the previous clustered event
                    cluster.append(all_spike_events[j])
                    used.add(j)
                    last_ts = all_spike_events[j]["ts"]
                else:
                    # events are time-sorted → every later one is even farther
                    break
            if len(cluster) >= 2:
                vms_in_cluster = list({c["vm"] for c in cluster})
                if len(vms_in_cluster) >= 2:
                    clusters.append(cluster)

        for cluster in clusters:
            vms_hit = list({c["vm"] for c in cluster})
            metrics_hit = list({c["metric"] for c in cluster})
            worst = max(cluster, key=lambda c: c["spike"]["z_score"])
            time_str = cluster[0]["ts"].strftime("%H:%M")
            patterns.append({
                "type": "cross_vm_correlation",
                "severity": "critical",
                "title": f"Correlated spikes across {len(vms_hit)} VMs at ~{time_str}",
                "description": (
                    f"{', '.join(vms_hit)} all spiked within a 15-min window around {time_str} UTC. "
                    f"Metrics: {', '.join(metrics_hit)}. "
                    f"Peak {worst['spike']['peak']}% on {worst['vm']} (z={worst['spike']['z_score']}). "
                    f"Suggests shared infrastructure pressure or coordinated workload."
                ),
                "vms": vms_hit,
                "count": len(cluster),
                "peak_z": worst["spike"]["z_score"],
                # Raw fields so the frontend can build a condensed line
                # without re-parsing "~{time_str}" out of the title string.
                "time_utc": time_str,
                "metrics": metrics_hit,
                "worst_vm": worst["vm"],
                "worst_peak": worst["spike"]["peak"],
            })

    # ── 3. Sustained high utilization (mean above threshold) ──
    for vm_name, metric_spikes in all_vm_spikes.items():
        # We get stats passed separately, but we can flag VMs with many spikes
        for metric, spikes in metric_spikes.items():
            # `critical_sustained` is a DISTINCT severity string emitted by
            # _classify_severity — matching only "critical" meant a VM whose
            # spikes were ALL sustained-critical scored critical_count == 0 and
            # still fired via the duration arm, rendering the self-contradictory
            # "0 critical spikes totaling 240 min".
            critical_count = sum(
                1 for s in spikes if s["severity"] in ("critical", "critical_sustained")
            )
            total_dur = sum(s.get("duration_min", 0) for s in spikes)
            if critical_count >= 3 or total_dur >= 60:
                # The two gates are DIFFERENT claims and must not share one
                # severity/wording. "critical_count >= 3" means several
                # individually-critical readings; "total_dur >= 60" alone can
                # fire from WARNING-severity spikes that never crossed the
                # critical line but added up over time. Labeling the
                # duration-only case "critical" and then saying "0 critical
                # spikes totaling N min" is a direct on-screen contradiction —
                # this was shipping to customers unfixed despite the comment
                # above already describing the undercounting half of the bug.
                if critical_count >= 3:
                    severity = "critical"
                    reason = f"{critical_count} critical spikes totaling {total_dur} min of elevated {metric}."
                else:
                    severity = "high"
                    reason = (
                        f"No single reading crossed the critical threshold, but {metric} stayed "
                        f"elevated for {total_dur} min of cumulative exposure across the window "
                        f"({critical_count} critical reading{'s' if critical_count != 1 else ''})."
                        if critical_count == 0 else
                        f"{critical_count} critical reading{'s' if critical_count != 1 else ''} plus "
                        f"sustained exposure totaling {total_dur} min of elevated {metric}."
                    )
                patterns.append({
                    "type": "sustained_pressure",
                    "severity": severity,
                    "title": f"Sustained {metric} pressure on {vm_name}",
                    "description": (
                        f"{reason} This VM is under persistent load and may require capacity investigation."
                    ),
                    "vms": [vm_name],
                    "count": critical_count,
                    "total_duration_min": total_dur,
                    # Raw field so the frontend can build a condensed line
                    # without regex-parsing "Sustained {x} pressure" out of title.
                    "metric": metric,
                })

    # Sort by severity (critical first) then by peak z-score descending
    sev_order = {"critical": 0, "high": 1}
    patterns.sort(key=lambda p: (sev_order.get(p["severity"], 9), -p.get("peak_z", 0)))
    return patterns


def fetch_vm_timeseries(credential, resource_ids: List[str],
                        hours_back: int,
                        start_utc: Optional[datetime] = None,
                        end_utc: Optional[datetime] = None,
                        vm_types: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Fetch time-series data + spike detection for a list of VMs.
    
    When start_utc/end_utc are provided they override hours_back for the
    query window (used for custom time-range deep dives from the UI).

    ``vm_types``, keyed by full ARM resource_id, carries the role
    (APP/DB/SRE) already correctly classified during the resource fetch
    (which has real Azure tags available). Without it this function falls
    back to a name-only guess that misses any VM whose name doesn't contain
    the literal substring "db" (e.g. "prbd..." has "bd", not "db") — which
    silently strips DB servers of their more lenient memory band and makes
    the spike detector flag their expected 80-92% SGA/PGA allocation as a
    critical incident, contradicting the DB-expected badge shown elsewhere.
    
    Returns {
      vm_name: {
        series: {metric_name: [{t, v}, ...]},
        spikes: {metric_name: [spike_event, ...]},
        stats: {metric_name: {mean, max, min, std, p95}},
      }
    }
    """
    from azure.monitor.query import MetricsQueryClient
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _t

    client = MetricsQueryClient(credential)
    if start_utc and end_utc:
        end_time = end_utc
        start_time = start_utc
        # Derive effective hours_back for granularity selection
        hours_back = max(1, int((end_time - start_time).total_seconds() / 3600))
    else:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=hours_back)

    # Use finer granularity for shorter time ranges; coarsen for long windows.
    # Azure Monitor only accepts specific ISO-8601 granularities:
    #   PT1M, PT5M, PT15M, PT30M, PT1H, PT6H, PT12H, P1D
    # PT4H is NOT valid — avoid it.  PT1H works for up to 93-day retention (all
    # standard VM metrics) so use it all the way to 30d (720h = 720 pts/VM, fine).
    if hours_back <= 1:
        granularity = timedelta(minutes=1)
    elif hours_back <= 6:
        granularity = timedelta(minutes=5)
    elif hours_back <= 24:
        granularity = timedelta(minutes=15)
    elif hours_back <= 720:
        granularity = timedelta(hours=1)   # PT1H — valid, 360–720 pts; works for 15d & 30d
    else:
        granularity = timedelta(hours=6)   # 60-day+ → PT6H (valid) = 240 pts

    if not resource_ids:
        return {"vms": {}, "patterns": [], "baseline": {}}

    t0 = _t.perf_counter()
    # ARM resource IDs are case-insensitive.  Normalize both sides before
    # looking up the tag-aware role supplied by the Resource fetch, otherwise
    # an innocuous casing difference falls back to hostname guessing (which
    # misses names such as prbd...) and grades expected DB SGA/PGA memory as an
    # application incident.
    _vm_types_by_resource_id = {
        str(resource_id).strip().lower(): str(role).strip().upper()
        for resource_id, role in (vm_types or {}).items()
        if resource_id and role
    }
    result = {}
    workers = min(20, len(resource_ids))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_query_single_vm_timeseries, client, rid,
                        start_time, end_time, granularity): rid
            for rid in resource_ids
        }
        for future in as_completed(futures):
            try:
                rid, series, true_extremes, series_max = future.result()
                vm_name = rid.split("/")[-1].lower()

                # Role drives which memory band this VM is judged against.
                # Oracle/DB VMs hold 80-92% memory by design (SGA/PGA), so the
                # generic 70/80 app band marks them critical forever — which
                # contradicts the "DB expected band" legend on the same screen.
                _rg = ""
                try:
                    _parts = rid.split("/")
                    if "resourceGroups" in _parts:
                        _rg = _parts[_parts.index("resourceGroups") + 1]
                except Exception:
                    _rg = ""
                # Prefer the caller-supplied role (tag-aware, computed during the
                # resource fetch) over a fresh name-only guess — see docstring.
                _vm_role = (
                    _vm_types_by_resource_id.get(rid.strip().lower())
                    or str(_infer_server_type(vm_name, None, _rg) or "APP").upper()
                )
                _vm_is_db = (_vm_role == "DB")

                # Compute stats and spikes per metric
                stats = {}
                spikes = {}
                for metric_name, points in series.items():
                    # Chart-only metrics (byte counters, ops/sec, availability)
                    # are NOT percentages and have no warn/crit band. Running
                    # them through _detect_spikes would grade every datapoint
                    # "critical_sustained", because _classify_severity's fallback
                    # band is warn=80/crit=90 and a byte value is numerically
                    # enormous. Keep descriptive stats for the chart header/axis,
                    # skip classification entirely.
                    if metric_name in _CHART_ONLY_METRICS:
                        vals = [p["v"] for p in points]
                        if vals:
                            _u = _METRIC_UNITS.get(metric_name, "raw")
                            _mean = sum(vals) / len(vals)
                            _var = sum((v - _mean) ** 2 for v in vals) / len(vals)
                            _ex = true_extremes.get(metric_name, {})
                            stats[metric_name] = {
                                "mean": round(_mean, 2),
                                "max": round(_ex.get("true_max") if _ex.get("true_max") is not None else max(vals), 2),
                                "min": round(_ex.get("true_min") if _ex.get("true_min") is not None else min(vals), 2),
                                "std": round(_var ** 0.5, 2),
                                "p5": round(_percentile(vals, 5), 2),
                                "p95": round(_percentile(vals, 95), 2),
                                "count": len(vals),
                                "unit": _u,
                                "chart_only": True,
                            }
                        continue

                    vals = [p["v"] for p in points]
                    if vals:
                        mean_v = sum(vals) / len(vals)
                        var_v = sum((v - mean_v) ** 2 for v in vals) / len(vals)

                        # Use true Max/Min from Azure when available, fall back to avg-based
                        extremes = true_extremes.get(metric_name, {})
                        true_max = extremes.get("true_max")
                        true_min = extremes.get("true_min")

                        # Outlier filter for max: if true_max is >2x the P95 and only
                        # appears in a single data point, flag it as potentially anomalous
                        max_val = true_max if true_max is not None else max(vals)
                        # Use the SAME interpolated percentile helper the baseline
                        # analysis uses. The previous `sorted_vals[int(n*0.95)]`
                        # index lookup collapsed to the MAXIMUM for n<=20 and p5 to
                        # the MINIMUM for n<20, so on short windows this card's
                        # "P95" was really the max — and disagreed with the p95 the
                        # baseline block reported for the very same VM.
                        p95_val = _percentile(vals, 95)
                        p5_val  = _percentile(vals, 5)
                        # max_anomalous compares a MAXIMUM-aggregation value against
                        # AVERAGE-derived stats, so any bursty VM trips it by
                        # construction at 1h grain. Require a meaningful absolute
                        # baseline too, so an idle-disk p95 of 0 can't reduce the
                        # test to "max_val > 0".
                        max_anomalous = (
                            p95_val > 1.0
                            and (max_val > p95_val * 2)
                            and (max_val > mean_v + 4 * (var_v ** 0.5))
                        )

                        # Outlier filter for min: flag when min is far below the
                        # mean (>3σ) and appears in fewer than 2 consecutive data points.
                        # This catches single-point collection artifacts (e.g. mem avail
                        # dropping to 0% for one sample then recovering).
                        min_val = true_min if true_min is not None else min(vals)
                        std_v = var_v ** 0.5
                        min_anomalous = False
                        if std_v > 0 and min_val < (mean_v - 3 * std_v) and len(vals) >= 5:
                            # Count max consecutive occurrences at or near the min
                            min_streak = 0
                            max_streak = 0
                            threshold = min_val * 1.1 + 0.5  # small tolerance
                            for v in vals:
                                if v <= threshold:
                                    min_streak += 1
                                    max_streak = max(max_streak, min_streak)
                                else:
                                    min_streak = 0
                            min_anomalous = max_streak < 2

                        stats[metric_name] = {
                            "mean": round(mean_v, 2),
                            "max": round(max_val, 2),
                            "min": round(min_val, 2),
                            "p5": round(p5_val, 2),
                            "std": round(var_v ** 0.5, 2),
                            "p95": round(p95_val, 2),
                            "count": len(vals),
                            "max_anomalous": max_anomalous,
                            "min_anomalous": min_anomalous,
                        }
                    spikes[metric_name] = _detect_spikes(
                        points, metric_name=metric_name, is_db=_vm_is_db)

                # ── Waveform (signal shape) classification ──
                # Produces the payload the deep dive's "Signal Pattern Analysis"
                # panel consumes. Nothing produced it before, so that whole
                # section — including its DB-band rewrite — was unreachable.
                _grain_min = granularity.total_seconds() / 60.0
                waveforms = {}
                for metric_name, points in series.items():
                    wf = _classify_waveform(points, metric_name, _vm_is_db, _grain_min)
                    if wf:
                        waveforms[metric_name] = wf

                # Concurrent pressure: two or more graded metrics simultaneously
                # at medium+ risk means the VM is under combined load, which is a
                # different remediation than a single hot metric.
                _pressured = [m for m, w in waveforms.items()
                              if w["risk"] in ("medium", "high", "critical")]
                if len(_pressured) >= 2:
                    _short = [m.replace("Percentage ", "").replace(" Consumed Percentage", "")
                              for m in _pressured]
                    for m in _pressured:
                        waveforms[m]["concurrent_pressure"] = True
                        waveforms[m]["concurrent_metrics"] = _short

                result[vm_name] = {
                    "resource_id": rid,
                    "series": series,
                    # Per-timestamp MAXIMUM, for the Avg+Max overlay. Averages
                    # hide intra-bucket peaks; showing both is what makes a
                    # 30-min average of 55% legible as a 98% momentary peak.
                    "series_max": series_max,
                    "spikes": spikes,
                    "stats": stats,
                    "waveforms": waveforms,
                    # Surfaced so the frontend legend and the findings layer can
                    # state WHICH memory band a VM was judged against.
                    "role": "DB" if _vm_is_db else _vm_role,
                }
            except Exception as exc:
                rid = futures[future]
                logger.warning("Time-series failed for %s: %s", rid.split("/")[-1], exc)

    # ── Pattern detection across all VMs ──
    all_vm_spikes = {vm: data.get("spikes", {}) for vm, data in result.items()}
    patterns = _detect_patterns(all_vm_spikes, hours_back)

    logger.info("Time-series fetch for %d VMs took %.1fs, %d patterns detected",
                len(resource_ids), _t.perf_counter() - t0, len(patterns))

    # ── Baseline analysis (15+ day context) ──
    baseline = _compute_baseline_analysis(result, hours_back)

    # Build a human-readable grain label for the chart subtitle
    total_secs = int(granularity.total_seconds())
    if total_secs < 3600:
        _grain_label = f"{total_secs // 60}min avg"
    elif total_secs == 3600:
        _grain_label = "1h avg"
    else:
        _grain_label = f"{total_secs // 3600}h avg"

    # Max datapoints observed on any single VM/metric — the honest "how much
    # telemetry did we actually get" number for the window badge (a nominal
    # 720-point window can easily return far fewer).
    _dp = 0
    for _vd in result.values():
        for _pts in (_vd.get("series") or {}).values():
            if len(_pts) > _dp:
                _dp = len(_pts)

    return {
        "vms": result,
        "patterns": patterns,
        "baseline": baseline,
        "window": {
            "hours_back": hours_back,
            "grain": _grain_label,
            "timezone": "UTC",
            # start_utc/end_utc/data_points are consumed by the frontend window
            # badge and the "STILL ACTIVE" open-incident marker. They were never
            # emitted, so the badge always fell through to the preset branch
            # (printing "Last 7 days" during a custom range) and the active
            # marker could never render.
            "start_utc": start_time.isoformat().replace("+00:00", "Z"),
            "end_utc": end_time.isoformat().replace("+00:00", "Z"),
            "is_custom": bool(start_utc and end_utc),
            "data_points": _dp,
        },
    }


def _compute_baseline_analysis(vm_data: Dict[str, Any], hours_back: int) -> Dict[str, Any]:
    """Compute 15-day baseline intelligence from time-series data.

    Produces per-VM and fleet-wide analysis:
    - Daily statistical baselines (mean, p95, stddev per day)
    - Time-of-day heat profiles (which hours are consistently hot)
    - Multi-day spike recurrence (same time window spiking on N days)
    - Weekday vs weekend divergence
    - Trend acceleration (is the metric getting worse over the observation period?)
    - Sustained chronic pressure windows (e.g. 2-6 AM consistently >80% CPU)

    Only meaningful when hours_back >= 48 (2+ days of data).
    Returns empty dict for short time ranges.
    """
    from datetime import datetime as _dt
    from collections import defaultdict
    from services import pe_config

    days_observed = hours_back / 24.0
    if days_observed < 2:
        return {}

    analysis: Dict[str, Any] = {
        "days_observed": round(days_observed, 1),
        "hours_back": hours_back,
        "sufficient_baseline": days_observed >= 15,
        "per_vm": {},
        "fleet": {},
    }

    # Key metrics to analyze.
    # Data Disk BW was queried, charted, spike-detected and persisted but was
    # missing here, so it was invisible to every baseline-derived finding
    # (DD5-DD10) — a data disk trending to saturation produced no finding.
    _ANALYSIS_METRICS = [
        "Percentage CPU",
        "Available Memory Percentage",
        "OS Disk Bandwidth Consumed Percentage",
        "Data Disk Bandwidth Consumed Percentage",
    ]

    fleet_daily_profiles: Dict[str, Dict[int, list]] = defaultdict(lambda: defaultdict(list))
    fleet_day_avgs: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for vm_name, vm_info in vm_data.items():
        series = vm_info.get("series", {})
        vm_analysis: Dict[str, Any] = {}

        for metric_name in _ANALYSIS_METRICS:
            points = series.get(metric_name, [])
            if len(points) < 10:
                continue

            # Parse timestamps
            parsed = []
            for p in points:
                try:
                    t = _dt.fromisoformat(p["t"].replace("Z", "+00:00"))
                    parsed.append({"t": t, "v": p["v"]})
                except Exception:
                    continue

            if len(parsed) < 10:
                continue

            # For "Available Memory Percentage", invert to "Memory Used %"
            is_mem_avail = "Available Memory" in metric_name
            if is_mem_avail:
                for pp in parsed:
                    pp["v"] = 100.0 - pp["v"]

            display_name = "Memory Used %" if is_mem_avail else metric_name

            # ── Group by date (YYYY-MM-DD) ──
            by_date: Dict[str, list] = defaultdict(list)
            for pp in parsed:
                date_key = pp["t"].strftime("%Y-%m-%d")
                by_date[date_key].append(pp["v"])

            # ── Group by hour-of-day ──
            by_hour: Dict[int, list] = defaultdict(list)
            for pp in parsed:
                by_hour[pp["t"].hour].append(pp["v"])

            # ── Group by weekday (0=Mon, 6=Sun) ──
            by_weekday: Dict[int, list] = defaultdict(list)
            for pp in parsed:
                by_weekday[pp["t"].weekday()].append(pp["v"])

            # ── Daily baselines ──
            daily_stats = []
            for date_str in sorted(by_date.keys()):
                vals = by_date[date_str]
                n = len(vals)
                if n == 0:
                    continue
                mean = sum(vals) / n
                variance = sum((v - mean) ** 2 for v in vals) / n
                std = variance ** 0.5
                p95 = _percentile(vals, 95.0)
                daily_stats.append({
                    "date": date_str,
                    "mean": round(mean, 2),
                    "max": round(max(vals), 2),
                    "min": round(min(vals), 2),
                    "p95": round(p95, 2),
                    "std": round(std, 2),
                    "samples": n,
                })
                fleet_day_avgs[display_name][date_str].append(mean)

            # ── Hourly heat profile ──
            hourly_profile = {}
            for hour in range(24):
                vals = by_hour.get(hour, [])
                if vals:
                    mean = sum(vals) / len(vals)
                    hourly_profile[hour] = round(mean, 2)
                    fleet_daily_profiles[display_name][hour].append(mean)

            # ── Hot hours: consistently above threshold across multiple days ──
            hot_hours = []
            threshold = _metric_elevation(display_name)["warn"]
            for hour in range(24):
                vals = by_hour.get(hour, [])
                if len(vals) >= max(2, int(days_observed * 0.3)):
                    above = sum(1 for v in vals if v >= threshold)
                    ratio = above / len(vals) if vals else 0
                    if ratio >= 0.4:
                        hot_hours.append({
                            "hour": hour,
                            "avg": round(sum(vals) / len(vals), 1),
                            "breach_ratio": round(ratio, 2),
                            "samples": len(vals),
                        })

            # ── Weekday vs weekend divergence ──
            weekday_vals = []
            weekend_vals = []
            for wd, vals in by_weekday.items():
                if wd < 5:
                    weekday_vals.extend(vals)
                else:
                    weekend_vals.extend(vals)

            weekday_avg = round(sum(weekday_vals) / len(weekday_vals), 2) if weekday_vals else 0
            weekend_avg = round(sum(weekend_vals) / len(weekend_vals), 2) if weekend_vals else 0
            divergence = round(abs(weekday_avg - weekend_avg), 2)

            # ── Trend acceleration: compare first half vs second half ──
            # Split by CLOCK-TIME midpoint, not list-index midpoint. Index
            # splitting assumes uniform sampling density; a real-world telemetry
            # gap (VM created mid-window, missing hours) shifts the index midpoint
            # away from the true time midpoint and makes "first vs second half" an
            # unfair comparison. Fall back to index split only for a degenerate
            # (zero-width) window where a time split leaves one side empty.
            all_vals = [pp["v"] for pp in parsed]
            _times = [pp["t"] for pp in parsed]
            _t_start, _t_end = min(_times), max(_times)
            _t_mid = _t_start + (_t_end - _t_start) / 2
            first_half = [pp["v"] for pp in parsed if pp["t"] < _t_mid]
            second_half = [pp["v"] for pp in parsed if pp["t"] >= _t_mid]
            if not first_half or not second_half:
                mid = len(all_vals) // 2
                first_half = all_vals[:mid] if mid > 0 else all_vals
                second_half = all_vals[mid:] if mid > 0 else all_vals
            first_avg = sum(first_half) / len(first_half) if first_half else 0
            second_avg = sum(second_half) / len(second_half) if second_half else 0
            trend_delta = round(second_avg - first_avg, 2)
            if abs(first_avg) > 0.01:
                trend_pct = round((trend_delta / first_avg) * 100, 1)
            else:
                trend_pct = 0.0

            trend_dir = "rising" if trend_delta > 2 else "falling" if trend_delta < -2 else "stable"

            # ── Time-to-breach projection (predict_linear) ──
            # Project hours until the metric crosses its WARN threshold via a
            # least-squares linear fit. Only emit when the trend is RISING and the
            # fit is trustworthy (R² ≥ PREDICT_MIN_R2) — below that the slope is
            # noise and would manufacture false urgency on a flat-but-jittery VM.
            hours_to_warn = None
            trend_r2 = None
            if len(parsed) >= 10:
                _t0 = _times[0]
                xs = [(pp["t"] - _t0).total_seconds() / 3600.0 for pp in parsed]
                ys = all_vals
                n = len(xs)
                mx = sum(xs) / n
                my = sum(ys) / n
                sxx = sum((x - mx) ** 2 for x in xs)
                sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                syy = sum((y - my) ** 2 for y in ys)
                slope = sxy / sxx if sxx > 0 else 0.0
                trend_r2 = round((sxy * sxy) / (sxx * syy), 2) if sxx > 0 and syy > 0 else 0.0
                warn = _metric_elevation(display_name)["warn"]
                current = max(parsed, key=lambda pp: pp["t"])["v"]
                if (trend_dir == "rising" and trend_r2 >= pe_config.PREDICT_MIN_R2
                        and slope > 0 and current < warn):
                    hours_to_warn = round((warn - current) / slope, 1)

            # ── Sustained chronic pressure: consecutive hours above threshold ──
            chronic_windows = []
            if daily_stats:
                for ds in daily_stats:
                    if ds["p95"] >= threshold:
                        chronic_windows.append(ds["date"])

            # ── Multi-day spike recurrence at same hour ──
            # `metric_name` IS already the raw Azure metric key here (only
            # `display_name` was rebound), so the old `if is_mem_avail:` re-lookup
            # re-assigned the identical value. Removing it makes it explicit that
            # these spike peaks are still in AVAILABLE-% space — which is why
            # worst_peak below must be inverted before it joins this used-% dict.
            recurring_spikes = []
            spikes_data = vm_info.get("spikes", {}).get(metric_name, [])
            spike_hours: Dict[int, list] = defaultdict(list)
            for s in spikes_data:
                try:
                    pt = _dt.fromisoformat(s["peak_time"].replace("Z", "+00:00"))
                    spike_hours[pt.hour].append({
                        "date": pt.strftime("%Y-%m-%d"),
                        "day_name": pt.strftime("%a"),
                        "peak": s["peak"],
                        "duration_min": s.get("duration_min", 0),
                    })
                except Exception:
                    continue

            for hour, events in spike_hours.items():
                unique_days = set(e["date"] for e in events)
                if len(unique_days) >= 2:
                    # worst_peak MUST be in the same USED-% space as every other
                    # field in this dict (overall_mean, hot_hours, daily_stats).
                    # e["peak"] comes from the raw spike record, which for memory
                    # is AVAILABLE % — so the worst (most pressured) sample is the
                    # MINIMUM available, not the maximum. Taking max() here picked
                    # the LEAST severe sample and emitted it as an available-%
                    # number inside a used-% dict, which routers/findings.py then
                    # rendered as "Memory spike ... peak 88% — CRITICAL" for what
                    # was actually an idle VM.
                    if is_mem_avail:
                        _worst_peak = 100.0 - min(e["peak"] for e in events)
                    else:
                        _worst_peak = max(e["peak"] for e in events)
                    recurring_spikes.append({
                        "hour": hour,
                        "day_count": len(unique_days),
                        "days": sorted(unique_days),
                        "day_names": sorted(set(e["day_name"] for e in events)),
                        "worst_peak": round(_worst_peak, 1),
                        "avg_duration_min": round(
                            sum(e["duration_min"] for e in events) / len(events), 1
                        ),
                    })

            vm_analysis[display_name] = {
                "daily_stats": daily_stats,
                "hourly_profile": hourly_profile,
                "hot_hours": hot_hours,
                "weekday_avg": weekday_avg,
                "weekend_avg": weekend_avg,
                "weekday_weekend_divergence": divergence,
                "trend_direction": trend_dir,
                "trend_delta": trend_delta,
                "trend_pct": trend_pct,
                "trend_r2": trend_r2,
                "hours_to_warn": hours_to_warn,
                "chronic_pressure_days": chronic_windows,
                "recurring_spikes": recurring_spikes,
                "overall_mean": round(sum(all_vals) / len(all_vals), 2),
                "overall_p95": round(_percentile(all_vals, 95.0), 2) if all_vals else 0,
                "overall_max": round(max(all_vals), 2) if all_vals else 0,
            }

        if vm_analysis:
            analysis["per_vm"][vm_name] = vm_analysis

    # ── Fleet-wide aggregation ──
    fleet_summary: Dict[str, Any] = {}
    for metric_name, day_avgs in fleet_day_avgs.items():
        all_daily_means = []
        for date_str in sorted(day_avgs.keys()):
            vm_means = day_avgs[date_str]
            fleet_mean = sum(vm_means) / len(vm_means)
            all_daily_means.append({"date": date_str, "fleet_avg": round(fleet_mean, 2)})

        # Fleet trend
        if len(all_daily_means) >= 2:
            first_val = all_daily_means[0]["fleet_avg"]
            last_val = all_daily_means[-1]["fleet_avg"]
            fleet_trend = round(last_val - first_val, 2)
        else:
            fleet_trend = 0

        # Fleet hourly profile
        fleet_hourly = {}
        for hour, vals in fleet_daily_profiles.get(metric_name, {}).items():
            fleet_hourly[hour] = round(sum(vals) / len(vals), 2) if vals else 0

        _fleet_warn = _metric_elevation(metric_name)["warn"]
        fleet_hot_hours = [
            h for h in range(24)
            if fleet_hourly.get(h, 0) >= _fleet_warn
        ]

        fleet_summary[metric_name] = {
            "daily_trend": all_daily_means,
            "fleet_trend_delta": fleet_trend,
            "fleet_hourly_profile": fleet_hourly,
            "fleet_hot_hours": fleet_hot_hours,
        }

    analysis["fleet"] = fleet_summary
    return analysis


def _positive_whole_number(value: Any) -> Optional[int]:
    """Return an Azure capability count only when it is a real positive integer."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return int(numeric) if numeric > 0 and numeric.is_integer() else None


def _sku_capacity_profile(capabilities: Any) -> Dict[str, Any]:
    """Extract auditable capacity facts from one Azure SKU capability list."""
    profile = {"memory_bytes": None, "vcpus": None, "vcpu_source": ""}
    values = {
        str(cap.name or "").strip().casefold(): cap.value
        for cap in (capabilities or [])
    }
    memory_gb = values.get("memorygb")
    try:
        if memory_gb is not None and float(memory_gb) > 0:
            profile["memory_bytes"] = float(memory_gb) * _BYTES_PER_GB
    except (TypeError, ValueError):
        pass
    usable_vcpus = _positive_whole_number(values.get("vcpusavailable"))
    total_vcpus = _positive_whole_number(values.get("vcpus"))
    if usable_vcpus is not None:
        profile["vcpus"] = usable_vcpus
        profile["vcpu_source"] = "vCPUsAvailable"
    elif total_vcpus is not None:
        profile["vcpus"] = total_vcpus
        profile["vcpu_source"] = "vCPUs"
    return profile


def _regional_vm_size_profile(vm_sizes: Any, vm_size: str) -> Dict[str, Any]:
    """Read core and RAM capacity from Azure's regional VM-size metadata.

    This is an authoritative Azure fallback when subscription-wide ResourceSkus
    capability enumeration is unavailable to the signed-in principal.  It does
    not try to decode capacity from a SKU name.
    """
    profile = {"memory_bytes": None, "vcpus": None, "vcpu_source": ""}
    expected_name = str(vm_size or "").strip().casefold()
    if not expected_name:
        return profile
    for size in vm_sizes or []:
        if str(getattr(size, "name", "") or "").strip().casefold() != expected_name:
            continue
        cores = _positive_whole_number(getattr(size, "number_of_cores", None))
        memory_mb = getattr(size, "memory_in_mb", None)
        if cores is not None:
            profile["vcpus"] = cores
            profile["vcpu_source"] = "regional_vm_size"
        try:
            if memory_mb is not None and float(memory_mb) > 0:
                profile["memory_bytes"] = float(memory_mb) * 1024 * 1024
        except (TypeError, ValueError):
            pass
        break
    return profile


def _vm_sku_profile(credential, subscription_id: str, vm_size: str,
                    location: str = "") -> Dict[str, Any]:
    """Return capacity facts for one Azure VM SKU, cached per subscription/SKU.

    ``vCPUsAvailable`` takes precedence over ``vCPUs``. Azure reports both on
    constrained-vCPU sizes, but the available value is the usable/licensed CPU
    count. This is intentionally distinct from CPU utilisation percentage.
    A missing SKU capability remains ``None``; it is never guessed from a SKU
    name or from a customer-specific lookup table.
    """
    empty = {"memory_bytes": None, "vcpus": None, "vcpu_source": ""}
    if not subscription_id or not vm_size:
        return dict(empty)
    cache = _vm_sku_profile.__dict__.setdefault("_cache", {})
    cache_key = (subscription_id.lower(), vm_size.lower(), location.strip().lower())
    if cache_key in cache:
        return dict(cache[cache_key])
    profile = dict(empty)
    try:
        from azure.mgmt.compute import ComputeManagementClient
        compute = ComputeManagementClient(credential, subscription_id)
        try:
            # ResourceSkus is subscription-scoped. Capability names are Azure's
            # contract, so match them case-insensitively but do not infer aliases.
            for sku in compute.resource_skus.list(filter=f"name eq '{vm_size}'"):
                candidate = _sku_capacity_profile(sku.capabilities)
                if profile["memory_bytes"] is None:
                    profile["memory_bytes"] = candidate["memory_bytes"]
                if profile["vcpus"] is None:
                    profile["vcpus"] = candidate["vcpus"]
                    profile["vcpu_source"] = candidate["vcpu_source"]
                # There can be more than one regional SKU record. The first record
                # with both capacity facts is sufficient because the requested size
                # is exact; incomplete rows may be followed by a complete one.
                if profile["memory_bytes"] is not None and profile["vcpus"] is not None:
                    break
        except Exception as exc:
            logger.info("Resource SKU capacity unavailable for %s; trying regional VM-size metadata: %s", vm_size, exc)

        # Reader-like access can expose regional VM-size metadata even where
        # ResourceSkus enumeration is denied. It is still Azure metadata and
        # returns number_of_cores directly, rather than a guessed SKU mapping.
        if location and (profile["memory_bytes"] is None or profile["vcpus"] is None):
            try:
                regional = _regional_vm_size_profile(
                    compute.virtual_machine_sizes.list(location), vm_size
                )
                if profile["memory_bytes"] is None:
                    profile["memory_bytes"] = regional["memory_bytes"]
                if profile["vcpus"] is None:
                    profile["vcpus"] = regional["vcpus"]
                    profile["vcpu_source"] = regional["vcpu_source"]
            except Exception as exc:
                logger.info("Regional VM-size capacity unavailable for %s in %s: %s", vm_size, location, exc)
    except Exception as exc:
        logger.debug("SKU capacity lookup failed for %s: %s", vm_size, exc)
    cache[cache_key] = dict(profile)
    return dict(profile)


def _vm_total_memory_bytes(credential, subscription_id: str, vm_size: str,
                           location: str = "") -> Optional[float]:
    """Backward-compatible RAM accessor backed by the shared SKU profile."""
    return _vm_sku_profile(credential, subscription_id, vm_size, location).get("memory_bytes")


def _extract_product_group(tags: dict) -> str:
    """Read a 'Product Group' tag under any real-world key casing/spacing a
    customer might use (ProductGroup, product_group, Product-Group, ...) —
    generic across every customer's own tagging convention, not one literal
    key name."""
    if not tags:
        return ""
    for k, v in tags.items():
        if k.replace("_", "").replace("-", "").replace(" ", "").lower() == "productgroup":
            return v or ""
    return ""


def _infer_server_type(name: str, tags: Optional[dict] = None, rg: str = "") -> str:
    """
    Classify VM role from its name, Azure tags, and resource group.
    Returns "DB", "SRE", or "APP".

    Priority: explicit Azure tag > name pattern > resource group pattern > default APP.
    """
    # 1. Check Azure tags (highest priority — user/infra explicitly set these)
    if tags:
        # Check "Application" tag first (common in enterprise Azure — e.g. "Oracle DB")
        app_tag = (tags.get("Application") or tags.get("application") or "").lower()
        if app_tag:
            if any(k in app_tag for k in ("oracle", "sql", "db", "database", "postgres",
                                           "mysql", "mongo", "redis", "cosmos", "data",
                                           "warehouse", "dw")):
                return "DB"
            if any(k in app_tag for k in ("batch", "ctm", "control-m", "scheduler",
                                           "automation", "sre", "infra")):
                return "SRE"

        for key in ("role", "Role", "server_type", "ServerType", "app-role",
                     "component", "Component", "tier", "Tier"):
            val = (tags.get(key) or "").lower()
            if val:
                if any(k in val for k in ("db", "sql", "database", "oracle",
                                           "postgres", "mysql", "mongo", "redis",
                                           "cosmos", "data")):
                    return "DB"
                if any(k in val for k in ("batch", "sre", "scheduler", "job",
                                           "worker", "cron", "infra", "ops",
                                           "control", "ctm", "automation")):
                    return "SRE"
                if any(k in val for k in ("app", "web", "api", "front", "service",
                                           "microservice", "gateway")):
                    return "APP"

    # 2. Check VM name (existing logic, expanded)
    n = (name or "").lower()
    if any(k in n for k in ("db", "sql", "ora", "pg", "mysql", "mongo", "redis",
                             "cosmos", "data", "dw", "warehouse")):
        return "DB"
    if any(k in n for k in ("sre", "batch", "sch", "job", "worker", "cron",
                             "ctm", "ctrl", "infra", "ops", "mgmt", "monitor")):
        return "SRE"

    # 3. Check resource group name
    rg_lower = (rg or "").lower()
    if any(k in rg_lower for k in ("db", "sql", "database", "data")):
        return "DB"
    if any(k in rg_lower for k in ("batch", "sre", "infra", "ops", "control")):
        return "SRE"

    return "APP"


def discover_vms(cfg: dict, resource_group: Optional[str] = None,
                 session_id=None) -> List[Dict[str, Any]]:
    """
    Discover all VMs in a subscription, classify them as APP/DB/SRE,
    and return a list for the user to select from before fetching metrics.
    """
    _require_sdk()

    sub_id = (cfg.get("azure_subscription_id") or "").strip()
    if not sub_id:
        raise AzureConfigError(
            "Azure Subscription ID not set. Add it in Settings → Azure Connection."
        )

    rg = (resource_group or cfg.get("azure_resource_group") or "").strip() or None
    credential = _build_credential(cfg, session_id)

    vms = _list_vms(credential, sub_id, rg)

    discovered = []
    for vm in vms:
        vm_type = _infer_server_type(vm["name"], vm.get("tags"), vm.get("rg", ""))
        discovered.append({
            "resource_id":   vm["resource_id"],
            "name":          vm["name"],
            "type":          vm_type,
            "location":      vm["location"],
            "vm_size":       vm["vm_size"],
            "resource_group": vm["rg"],
            "tags":          vm.get("tags", {}),
            "product_group": _extract_product_group(vm.get("tags") or {}),
        })

    # Sort: DB first, then SRE, then APP, then alphabetically
    order = {"DB": 0, "SRE": 1, "APP": 2}
    discovered.sort(key=lambda v: (order.get(v["type"], 9), v["name"]))
    return discovered


def search_vms(credential, query: str,
               subscription_ids: Optional[List[str]] = None,
               session_id=None) -> List[Dict[str, Any]]:
    """
    Search for VMs across all (or specified) subscriptions using Azure
    Resource Graph.  Matches VM name, resource group, tags (CustomerName,
    Application, Environment_Type, etc.).

    Returns the same shape as discover_vms() so the frontend can render
    the same VM table.
    """
    # A post-login inventory pre-warm already has the same metadata needed by
    # this search. Use it first: no network call and no Resource Graph delay.
    q = (query or "").strip()
    if not q:
        raise AzureConfigError("Search query is empty.")
    q_lower = q.lower()
    with _vm_prewarm_lock:
        cached = list(_vm_inventory_cache.get(_sid_norm(session_id), {}).items())
    cached_matches = []
    for cached_subscription_id, inventory in cached:
        if subscription_ids and cached_subscription_id not in subscription_ids:
            continue
        for vm in inventory:
            tags = vm.get("tags") or {}
            searchable = " ".join((
                str(vm.get("name", "")), str(vm.get("resource_group", "")),
                " ".join(f"{k} {v}" for k, v in tags.items()),
            )).lower()
            if q_lower in searchable:
                cached_vm = dict(vm)
                cached_vm["subscription_id"] = cached_subscription_id
                cached_matches.append(cached_vm)
    if cached_matches:
        order = {"DB": 0, "SRE": 1, "APP": 2}
        cached_matches.sort(key=lambda v: (order.get(v.get("type"), 9), v.get("name", "")))
        logger.info("VM search '%s' served %d match(es) from session inventory cache", q, len(cached_matches))
        return cached_matches

    try:
        from azure.mgmt.resourcegraph import ResourceGraphClient
        from azure.mgmt.resourcegraph.models import (
            QueryRequest, QueryRequestOptions,
        )
    except ImportError:
        raise AzureConfigError(
            "azure-mgmt-resourcegraph not installed. "
            "Run: pip install azure-mgmt-resourcegraph"
        )

    # Sanitize query for KQL (escape single quotes)
    q_kql = q.replace("'", "\\'")

    # KQL: search VMs where name, RG, or any tag value contains the query
    kql = f"""
    Resources
    | where type =~ 'microsoft.compute/virtualMachines'
    | where name contains '{q_kql}'
       or resourceGroup contains '{q_kql}'
       or tostring(tags) contains '{q_kql}'
    | project id, name, location, resourceGroup, subscriptionId,
              vmSize = tostring(properties.hardwareProfile.vmSize),
              tags,
              powerState = tostring(properties.extended.instanceView.powerState.code)
    | order by name asc
    | limit 200
    """

    from services import pe_config as _pc
    client = ResourceGraphClient(
        credential,
        connection_timeout=_pc.AZURE_RESOURCE_GRAPH_CONNECT_TIMEOUT_S,
        read_timeout=_pc.AZURE_RESOURCE_GRAPH_READ_TIMEOUT_S,
    )

    opts = QueryRequestOptions(result_format="objectArray")
    req_kwargs = {"query": kql, "options": opts}
    if subscription_ids:
        req_kwargs["subscriptions"] = subscription_ids

    request = QueryRequest(**req_kwargs)

    try:
        response = client.resources(request)
    except Exception as exc:
        if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
            raise AzureTimeoutError(
                "Azure Resource Graph search timed out. Narrow the subscription scope and try again."
            ) from exc
        raise AzureFetchError(f"Resource Graph query failed: {exc}") from exc

    results: List[Dict[str, Any]] = []
    for row in (response.data or []):
        rid  = row.get("id", "")
        name = row.get("name", "")
        tags = row.get("tags") or {}
        rg   = row.get("resourceGroup", "")
        vm_type = _infer_server_type(name, tags, rg)

        results.append({
            "resource_id":    rid,
            "name":           name,
            "type":           vm_type,
            "location":       row.get("location", ""),
            "vm_size":        row.get("vmSize", ""),
            "resource_group": rg,
            "subscription_id": row.get("subscriptionId", ""),
            "tags":           tags,
            "product_group":  _extract_product_group(tags),
            "customer":       tags.get("CustomerName") or tags.get("customerName") or "",
            "application":    tags.get("Application") or tags.get("application") or "",
            "environment":    tags.get("Environment_Type") or tags.get("environment_type")
                              or tags.get("Environment") or "",
        })

    order = {"DB": 0, "SRE": 1, "APP": 2}
    results.sort(key=lambda v: (order.get(v["type"], 9), v["name"]))
    logger.info("Resource Graph search '%s' → %d VMs", query, len(results))
    return results


def search_vms_with_fallback(credential, query: str,
                             subscription_ids: Optional[List[str]] = None,
                             session_id=None) -> tuple[List[Dict[str, Any]], bool]:
    """Search selected subscriptions, then the caller's accessible scope if empty.

    The retry deliberately uses the same session-scoped browser credential and no
    subscription list so Azure Resource Graph applies the signed-in caller's RBAC
    scope.  It never widens a search that already found a VM.
    """
    selected_ids = [str(sub_id).strip() for sub_id in (subscription_ids or [])
                    if str(sub_id).strip()]
    results = search_vms(
        credential,
        query,
        subscription_ids=selected_ids or None,
        session_id=session_id,
    )
    if results or not selected_ids:
        return results, False

    logger.info(
        "Resource Graph search '%s' returned no VMs in selected scope; retrying caller-accessible scope",
        query,
    )
    return search_vms(credential, query, session_id=session_id), True


def fetch_vm_metrics(cfg: dict, hours_back: int = 24,
                     vm_ids: Optional[List[str]] = None,
                     session_id=None) -> List[Dict[str, Any]]:
    """
    Main entry point. Fetches VM metrics from Azure Monitor.

    Parameters
    ----------
    cfg        : Full config dict from config_store.get_all()
    hours_back : How many hours of history to average (default 24)
    vm_ids     : Optional list of full Azure resource IDs to fetch.
                 When provided, subscriptions are extracted from the IDs
                 themselves so we don't depend on the config subscription.

    Returns
    -------
    List of server dicts compatible with resource_calculator.build_resource_payload():
        host, server, type, cpu_used, mem_used, disk_used_max,
        cpu_pct, mem_pct, disk_pct, source
    """
    _require_sdk()
    credential = _build_credential(cfg, session_id)

    # ── When explicit VM IDs are given, get each VM directly (fast + parallel) ──
    if vm_ids:
        import re
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            from azure.mgmt.compute import ComputeManagementClient
        except ImportError:
            raise AzureConfigError("azure-mgmt-compute not installed.")

        # Parse resource IDs
        parsed = []
        for rid in vm_ids:
            m = re.match(
                r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Compute/virtualMachines/([^/]+)",
                rid, re.IGNORECASE
            )
            if m:
                parsed.append((rid, m.group(1), m.group(2), m.group(3)))
            else:
                logger.warning("Cannot parse resource ID: %s", rid)

        if not parsed:
            raise AzureConfigError("No valid Azure resource IDs provided.")

        # Build compute clients per subscription
        _clients: Dict[str, Any] = {}
        for _, sub_id, _, _ in parsed:
            if sub_id not in _clients:
                _clients[sub_id] = ComputeManagementClient(credential, sub_id)

        def _get_vm(item):
            rid, sub_id, rg_name, vm_name = item
            vm = _clients[sub_id].virtual_machines.get(rg_name, vm_name)
            tags = dict(vm.tags) if vm.tags else {}
            return {
                "resource_id": vm.id,
                "name":        vm.name,
                "location":    vm.location or "",
                "vm_size":     (vm.hardware_profile.vm_size if vm.hardware_profile else "") or "",
                "rg":          rg_name,
                "tags":        tags,
            }

        all_vms: List[dict] = []
        workers = min(20, len(parsed))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_get_vm, item): item for item in parsed}
            for future in as_completed(futures):
                try:
                    all_vms.append(future.result())
                except Exception as exc:
                    item = futures[future]
                    logger.warning("Failed to get VM %s/%s: %s", item[2], item[3], exc)

        if not all_vms:
            raise AzureFetchError(
                "Could not find the selected VMs. Your account may lack "
                "Reader + Monitoring Reader roles on their subscriptions."
            )

        logger.info("Fetched %d VM details directly — querying metrics (last %dh)…",
                     len(all_vms), hours_back)
        return _build_server_records(credential, all_vms, hours_back)

    # ── Standard path: use configured subscription ──
    sub_id = (cfg.get("azure_subscription_id") or "").strip()
    if not sub_id:
        raise AzureConfigError(
            "Azure Subscription ID not set. Add it in Settings → Azure Connection."
        )

    rg = (cfg.get("azure_resource_group") or "").strip() or None
    logger.info("Listing Azure VMs (sub=%s, rg=%s)…", sub_id, rg or "ALL")
    vms = _list_vms(credential, sub_id, rg)

    if not vms:
        raise AzureFetchError(
            f"No VMs found in subscription {sub_id}"
            + (f" / resource group {rg}" if rg else "")
            + ". This subscription may not contain any Virtual Machines, "
            + "or your account may lack Reader + Monitoring Reader roles. "
            + "Run 'az vm list' to verify, or ask your Azure admin for access "
            + "to the correct subscription."
        )

    logger.info("Found %d VMs — querying metrics (last %dh)…", len(vms), hours_back)
    return _build_server_records(credential, vms, hours_back)


def _build_server_records(credential, vms: List[dict],
                          hours_back: int) -> List[Dict[str, Any]]:
    """Shared helper: query metrics for a list of VMs and build server records.
    
    Computes total memory from Available Memory Bytes + Available Memory
    Percentage to avoid slow SKU API calls. A bounded, cached SKU lookup also
    supplies usable vCPU capacity for the audit, and fills RAM only where live
    memory metrics cannot establish it.
    """
    import re as _re
    import time as _time

    resource_ids = [v["resource_id"] for v in vms]

    t0 = _time.perf_counter()

    metrics_map = _query_metrics(credential, resource_ids, hours_back)
    t_metrics = _time.perf_counter() - t0
    logger.info("Metrics query took %.1fs for %d VMs", t_metrics, len(vms))

    servers: List[Dict[str, Any]] = []
    sku_metadata_needed: list = []  # (server index, subscription, size, needs RAM fallback)

    for vm in vms:
        rid  = vm["resource_id"]
        name = vm["name"]
        m    = metrics_map.get(rid, {})

        cpu_pct_recent = m.get("Percentage CPU__recent")
        cpu_pct_avg = round(m.get("Percentage CPU", 0.0), 2)
        # CPU%: use most recent 1h data point; CPU AVG: use period average
        cpu_pct = round(cpu_pct_recent, 2) if cpu_pct_recent is not None else cpu_pct_avg
        # CPU MAX/MIN: true period extremes — the worst/best single hourly bucket
        # across the whole window. Lets a PE lead pick "Max" and see a job-driven
        # CPU spike that a 15-day average would otherwise smooth away to nothing.
        _cpu_max = m.get("Percentage CPU__max")
        _cpu_min = m.get("Percentage CPU__min")
        cpu_max_pct = round(_cpu_max, 2) if _cpu_max is not None else None
        cpu_min_pct = round(_cpu_min, 2) if _cpu_min is not None else None

        avail_pct   = m.get("Available Memory Percentage")
        avail_bytes = m.get("Available Memory Bytes")
        mem_pct = 0.0
        mem_total_gb = 0.0
        needs_memory_sku = False

        # FAST PATH: compute total memory from the two metrics (no API call)
        # Guard: avail_pct must be ≥1% to derive reliable total_bytes.
        # Below 1%, rounding artifacts can produce wildly wrong totals.
        if avail_pct is not None and avail_pct >= 1.0 and avail_bytes is not None and avail_bytes > 0:
            # total = available_bytes / (available_pct / 100)
            total_bytes = avail_bytes / (avail_pct / 100.0)
            mem_total_gb = round(total_bytes / _BYTES_PER_GB, 2)
            mem_pct = round(max(0.0, min(100.0, 100.0 - avail_pct)), 2)
        elif avail_pct is not None and avail_pct > 0:
            # Have percentage but not bytes — still know used %
            mem_pct = round(max(0.0, min(100.0, 100.0 - avail_pct)), 2)
            # Mark for SKU lookup to get total GB
            needs_memory_sku = True
        elif avail_bytes is not None:
            # Have bytes but not percentage — need SKU for total
            needs_memory_sku = True

        # Memory MAX/MIN — the raw Azure metric is "Available %" (lower = worse),
        # so the USED-% max (worst pressure point) comes from the AVAILABLE MIN,
        # and the USED-% min (best point) comes from the AVAILABLE MAX. Inverted
        # on purpose — do not swap these without also swapping the tooltip copy.
        _avail_min = m.get("Available Memory Percentage__min")
        _avail_max = m.get("Available Memory Percentage__max")
        mem_max_pct = round(max(0.0, min(100.0, 100.0 - _avail_min)), 2) if _avail_min is not None else None
        mem_min_pct = round(max(0.0, min(100.0, 100.0 - _avail_max)), 2) if _avail_max is not None else None

        # Absent disk metric → None (not a fabricated 0.0). Emitting 0.0 here
        # would let a server with no disk telemetry look like a genuine "0% disk"
        # reading and drag the fleet Avg Disk toward zero. None flows through to
        # disk_available=False → disk_pct=None so it is excluded from the mean.
        _disk_raw = m.get("OS Disk Bandwidth Consumed Percentage")
        _disk_max = m.get("OS Disk Bandwidth Consumed Percentage__max")
        _disk_min = m.get("OS Disk Bandwidth Consumed Percentage__min")
        if _disk_raw is None:
            _disk_raw = m.get("Data Disk Bandwidth Consumed Percentage")
            _disk_max = m.get("Data Disk Bandwidth Consumed Percentage__max")
            _disk_min = m.get("Data Disk Bandwidth Consumed Percentage__min")
        disk_pct = round(_disk_raw, 2) if _disk_raw is not None else None
        disk_max_pct = round(_disk_max, 2) if _disk_max is not None else None
        disk_min_pct = round(_disk_min, 2) if _disk_min is not None else None

        _tags = vm.get("tags") or {}
        sub_match = _re.match(r"/subscriptions/([^/]+)/", rid, _re.IGNORECASE)
        if sub_match and vm.get("vm_size"):
            sku_metadata_needed.append((
                len(servers), sub_match.group(1), vm["vm_size"],
                vm.get("location", ""), needs_memory_sku,
            ))
        servers.append({
            "host":          name.lower(),
            "server":        name.lower(),
            "type":          _infer_server_type(name, vm.get("tags"), vm.get("rg", "")),
            "cpu_used":      cpu_pct,
            "cpu_avg":       cpu_pct_avg,
            "cpu_max_pct":   cpu_max_pct,
            "cpu_min_pct":   cpu_min_pct,
            "mem_used":      mem_pct,
            "mem_max_pct":   mem_max_pct,
            "mem_min_pct":   mem_min_pct,
            "mem_total_gb":  mem_total_gb,
            "disk_used_max": disk_pct,
            "disk_max_pct":  disk_max_pct,
            "disk_min_pct":  disk_min_pct,
            "cpu_pct":       cpu_pct,
            "mem_pct":       mem_pct,
            "disk_pct":      disk_pct,
            "resource_id":   rid,
            "location":      vm["location"],
            "vm_size":       vm["vm_size"],
            "vm_size_desc":  (vm["vm_size"] or "").replace("_", " "),
            "vcpus":         None,
            "vcpu_source":   "",
            "resource_group":vm["rg"],
            "tags":          _tags,
            "product_group": _extract_product_group(_tags),
            "source":        "azure_monitor",
            "hours_back":    hours_back,
        })

    # Capacity metadata: one bounded lookup per unique subscription/SKU. This
    # never blocks a metric result: unavailable capacity stays explicitly empty.
    if sku_metadata_needed:
        from concurrent.futures import ThreadPoolExecutor
        memory_fallbacks = sum(1 for *_, needs_memory_sku in sku_metadata_needed if needs_memory_sku)
        logger.info("SKU capacity lookup for %d VMs (%d need RAM fallback)", len(sku_metadata_needed), memory_fallbacks)
        t1 = _time.perf_counter()
        unique_sizes = {(sub_id, vm_size, location) for _, sub_id, vm_size, location, _ in sku_metadata_needed}

        with ThreadPoolExecutor(max_workers=min(5, len(unique_sizes))) as pool:
            results = {
                pair: profile
                for pair, profile in pool.map(
                    lambda p: (p, _vm_sku_profile(credential, p[0], p[1], p[2])), unique_sizes
                )
            }

        for idx, sub_id, vm_size, location, needs_memory_sku in sku_metadata_needed:
            profile = results.get((sub_id, vm_size, location), {})
            servers[idx]["vcpus"] = profile.get("vcpus")
            servers[idx]["vcpu_source"] = profile.get("vcpu_source") or ""
            total_bytes = profile.get("memory_bytes")
            if needs_memory_sku and total_bytes and total_bytes > 0:
                servers[idx]["mem_total_gb"] = round(total_bytes / _BYTES_PER_GB, 2)
                avail_bytes = metrics_map.get(servers[idx]["resource_id"], {}).get("Available Memory Bytes")
                if avail_bytes is not None and servers[idx]["mem_pct"] == 0.0:
                    servers[idx]["mem_pct"] = round(max(0.0, min(100.0, (1.0 - avail_bytes / total_bytes) * 100.0)), 2)
                    servers[idx]["mem_used"] = servers[idx]["mem_pct"]

        logger.info("SKU capacity lookup took %.1fs", _time.perf_counter() - t1)

    total_time = _time.perf_counter() - t0
    logger.info("Azure fetch complete — %d servers in %.1fs (metrics: %.1fs)", len(servers), total_time, t_metrics)
    return servers


# ─────────────────────────────────────────────────────────────────
# VM inventory pre-warm cache
# ─────────────────────────────────────────────────────────────────
# Populated by prewarm_vm_inventory (background, triggered after login).
# clear_vm_inventory_cache wipes it on logout / credential change.
# get_vm_prewarm_state returns the current state for the polling endpoint.

_vm_inventory_cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
_vm_prewarm_state: Dict[str, Dict[str, Any]] = {}
_vm_prewarm_lock = __import__("threading").Lock()


def clear_vm_inventory_cache(session_id=None) -> None:
    """Wipe this session's VM inventory so identities never share cached metadata."""
    sid = _sid_norm(session_id)
    with _vm_prewarm_lock:
        _vm_inventory_cache.pop(sid, None)
        _vm_prewarm_state.pop(sid, None)


def get_vm_prewarm_state(session_id=None) -> Dict[str, Any]:
    """Return the current VM pre-warm status (no network call)."""
    with _vm_prewarm_lock:
        return dict(_vm_prewarm_state.get(_sid_norm(session_id), {
            "status": "idle", "vm_count": 0, "ts": 0.0, "error": None,
        }))


def prewarm_vm_inventory(credential, subscription_id: str,
                         resource_group: Optional[str] = None,
                         session_id=None) -> None:
    """Background: discover all VMs and cache them so search is instantaneous.

    Runs discover_vms() once and stores the result in _vm_inventory_cache.
    Subsequent calls to search_vms can check the cache first before hitting Azure.
    """
    import threading as _threading

    def _worker():
        sid = _sid_norm(session_id)
        with _vm_prewarm_lock:
            if _vm_prewarm_state.get(sid, {}).get("status") == "warming":
                return  # already running
            _vm_prewarm_state[sid] = {"status": "warming", "vm_count": 0, "ts": __import__("time").time(), "error": None}
        try:
            cfg = {"azure_subscription_id": subscription_id}
            vms = discover_vms(cfg, resource_group=resource_group, session_id=session_id)
            with _vm_prewarm_lock:
                _vm_inventory_cache[sid] = {subscription_id: vms}
                _vm_prewarm_state[sid] = {
                    "status": "ready",
                    "vm_count": len(vms),
                    "ts": __import__("time").time(),
                    "error": None,
                }
            logger.info("VM inventory pre-warm complete — %d VMs cached for sub %s", len(vms), subscription_id)
        except Exception as _e:
            with _vm_prewarm_lock:
                _vm_prewarm_state[sid] = {
                    "status": "error",
                    "vm_count": 0,
                    "ts": __import__("time").time(),
                    "error": str(_e),
                }
            logger.warning("VM inventory pre-warm failed: %s", _e)

    _threading.Thread(target=_worker, daemon=True).start()
