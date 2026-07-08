"""
Azure Monitor resource fetcher — personal identity via az login.

Uses the azure-monitor-query + azure-identity SDK to pull CPU / Memory /
Disk metrics for all VMs in a given subscription + resource group, then
returns records in the same dict shape as resource_parser_generic.py so
they feed directly into resource_calculator.build_resource_payload().

Authentication:
    DefaultAzureCredential — picks up the logged-in user from `az login`,
    managed identity, VS Code credential, or environment variables.
    Every data pull is tied to the user's own Azure AD identity.

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

import hashlib as _hashlib
import logging
import os
import sys as _sys
import threading as _threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.spike_schema import make_spike_record

logger = logging.getLogger("pe_dashboard.azure_monitor")


class AzureConfigError(Exception):
    """Raised when Azure credentials are missing or authentication fails."""


class AzureFetchError(Exception):
    """Raised when the Azure Monitor API call fails."""


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
# preserves the original single-user / az-login workflow when no session id is
# supplied (e.g. internal callers). All access is guarded by a re-entrant lock.
_cred_lock = _threading.RLock()
_cred_sessions: dict = {}   # sid -> {"cred": <credential|None>, "info": {...}}
_DEFAULT_SID = "_default"

# Seconds to wait for the user to finish the interactive browser sign-in before
# giving up. The SDK default is 300s; we cap lower so a stalled loopback redirect
# (corporate proxy / stale tab / wrong browser profile) fails fast with a clear,
# retryable error instead of leaving the request — and the UI — hanging.
_BROWSER_AUTH_TIMEOUT_S = 180

# Persistent credential cache — survives server restarts. Files are namespaced
# per session so a restart restores only the identity that owns each session,
# never a blanket "last person to log in" for everyone. The default bucket keeps
# the original unsuffixed filenames for backward compatibility.
_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"


def _sid_norm(session_id=None) -> str:
    sid = (session_id or "").strip()
    return sid or _DEFAULT_SID


def _sid_tag(session_id=None) -> str:
    """Filesystem-safe per-session suffix. Default bucket → '' (legacy names)."""
    sid = _sid_norm(session_id)
    if sid == _DEFAULT_SID:
        return ""
    return "_" + _hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]


def _auth_record_file(session_id=None) -> Path:
    return _CACHE_DIR / f"auth_record{_sid_tag(session_id)}.json"


def _credential_info_file(session_id=None) -> Path:
    return _CACHE_DIR / f"credential_info{_sid_tag(session_id)}.json"


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


def _save_credential_info(info: dict, session_id=None):
    """Persist credential identity info to this session's disk file."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        import json as _json
        _credential_info_file(session_id).write_text(_json.dumps(info), encoding="utf-8")
    except Exception:
        pass


def _load_credential_info(session_id=None) -> dict:
    """Load persisted credential identity info for this session from disk."""
    try:
        f = _credential_info_file(session_id)
        if f.exists():
            import json as _json
            return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_auth_record(record, session_id=None):
    """Persist AuthenticationRecord to this session's disk file."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _auth_record_file(session_id).write_text(record.serialize(), encoding="utf-8")
    except Exception as exc:
        logger.warning("Failed to persist auth record: %s", exc)


def _load_auth_record(session_id=None):
    """Load this session's AuthenticationRecord from disk, or None."""
    try:
        f = _auth_record_file(session_id)
        if f.exists():
            from azure.identity import AuthenticationRecord
            return AuthenticationRecord.deserialize(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _token_cache_options():
    """Return safe TokenCachePersistenceOptions (DPAPI bypassed via Fix 3 above)."""
    try:
        from azure.identity import TokenCachePersistenceOptions
        return TokenCachePersistenceOptions(
            name="pe_dashboard_browser_msal_cache",
            allow_unencrypted_storage=True,
        )
    except Exception:
        return None


_MSAL_CACHE_NAME = "pe_dashboard_browser_msal_cache"


def _msal_token_cache_files() -> list:
    """Best-effort list of the on-disk MSAL token-cache files this app creates.

    azure-identity stores the browser refresh-token cache under
    ``<user-data>/.IdentityService/<name><suffix>`` where suffix is ``.nocae``
    or ``.cae``. We list every variant so a corrupt/locked cache can be purged
    without depending on the SDK internals. Returns an empty list on any error."""
    out = []
    try:
        if _sys.platform.startswith("win"):
            base = os.environ.get("LOCALAPPDATA")
            root = Path(base) / ".IdentityService" if base else None
        else:
            root = Path(os.path.expanduser("~")) / ".IdentityService"
        if root is None:
            return out
        for suffix in (".nocae", ".cae", ""):
            out.append(root / f"{_MSAL_CACHE_NAME}{suffix}")
    except Exception:
        return []
    return out


def _purge_msal_token_cache() -> int:
    """Delete the MSAL persistent token-cache file(s). Best-effort, never raises.

    Used to self-heal a corrupt cache (e.g. the Windows cp1252/'charmap' decode
    crash, a stale DPAPI blob, or a version mismatch) so the next interactive
    login starts from a clean state. Returns the count of files removed."""
    removed = 0
    for f in _msal_token_cache_files():
        try:
            if f.exists():
                f.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("Purged %d corrupt MSAL token-cache file(s).", removed)
    return removed


def _is_cache_persistence_error(exc: BaseException) -> bool:
    """True when an auth exception is a token-cache read/write failure rather
    than a genuine user cancellation/timeout. Walks the exception chain and
    matches the Windows 'charmap' codec crash, any Unicode/codec decode error,
    and msal-extensions persistence errors. Conservative: only these classes of
    failure trigger the purge-and-retry-without-persistence path."""
    seen = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, UnicodeDecodeError):
            return True
        msg = str(cur).lower()
        if any(tok in msg for tok in (
            "charmap", "codec can't decode", "codec can not decode",
            "'utf-8' codec", "persistence", "persisted", "token cache",
            "tokencache", ".identityservice",
        )):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _restore_browser_credential(session_id=None) -> bool:
    """Try to restore this session's browser credential from its persistent
    AuthenticationRecord. Uses silent auth (no browser popup). Returns True if
    restored. Each session restores only its OWN identity file — never another
    session's."""
    if _get_cred(session_id) is not None:
        return True

    info = _load_credential_info(session_id)
    if not info.get("logged_in"):
        return False

    record = _load_auth_record(session_id)
    if record is None:
        return False

    try:
        from azure.identity import InteractiveBrowserCredential
        cache_opts = _token_cache_options()
        kwargs = {"authentication_record": record}
        if cache_opts is not None:
            kwargs["cache_persistence_options"] = cache_opts
        cred = InteractiveBrowserCredential(**kwargs)
        # Silently acquire a token (should use cached refresh token)
        token = cred.get_token("https://management.azure.com/.default")
        if token and token.token:
            _set_session(session_id, cred, info)
            logger.info("Restored browser credential from cache: %s", info.get("name", "?"))
            return True
    except Exception as exc:
        logger.debug("Could not restore cached credential: %s", exc)
        # If the persistent token cache itself is corrupt (e.g. the Windows
        # 'charmap' crash), purge it so the next interactive login starts clean.
        # Identity files are left intact — only on explicit sign-out are those removed.
        if _is_cache_persistence_error(exc):
            _purge_msal_token_cache()
    return False


def _clear_persistent_cache(session_id=None):
    """Remove this session's persistent cache files."""
    try:
        for f in (_auth_record_file(session_id), _credential_info_file(session_id)):
            if f.exists():
                f.unlink()
    except Exception:
        pass


def _preflight_auth_network(timeout: float = 6.0) -> None:
    """Quick DNS + TCP check for login.microsoftonline.com (IPv4, port 443).
    Raises AzureConfigError fast if network is down — avoids 180s SDK timeout."""
    import socket as _s
    try:
        addrs = _s.getaddrinfo("login.microsoftonline.com", 443,
                               _s.AF_INET, _s.SOCK_STREAM)
        if not addrs:
            raise AzureConfigError("DNS lookup for login.microsoftonline.com returned no IPv4 addresses.")
        ip = addrs[0][4][0]
        sock = _s.create_connection((ip, 443), timeout=timeout)
        sock.close()
    except AzureConfigError:
        raise
    except OSError as exc:
        raise AzureConfigError(
            f"Cannot reach login.microsoftonline.com — check network/VPN. ({exc})"
        ) from exc


def browser_login(session_id=None) -> dict:
    """Launch interactive browser login and cache the credential for THIS session.

    Opens the Microsoft "Pick an account" page in the user's default
    browser.  After successful sign-in the credential is cached both
    in-process (under this session id) AND on disk (per-session
    AuthenticationRecord) so it survives server restarts without re-prompting —
    and without overwriting any other session's identity.

    Returns a dict with identity info (name, tenant, etc.).
    """
    _require_sdk()
    from azure.identity import InteractiveBrowserCredential
    import json as _json, base64

    # Fix 5: network preflight — fail fast (6s) instead of 180s timeout
    _preflight_auth_network()

    logger.info("Azure auth: launching interactive browser login…")
    try:
        cache_opts = _token_cache_options()
        # Bound the interactive wait so a stalled loopback redirect fails fast
        # (clear, retryable error) instead of hanging on the SDK's 300s default.
        kwargs: dict = {"timeout": _BROWSER_AUTH_TIMEOUT_S}
        if cache_opts is not None:
            kwargs["cache_persistence_options"] = cache_opts
        cred = InteractiveBrowserCredential(**kwargs)
        # authenticate() opens browser AND returns an AuthenticationRecord
        record = cred.authenticate(scopes=["https://management.azure.com/.default"])
        _save_auth_record(record, session_id)   # save AFTER authenticate() returns the record
        # Now get a token (will be silent — cached from authenticate())
        token = cred.get_token("https://management.azure.com/.default")
    except Exception as exc:
        # A corrupt/legacy token cache (e.g. the Windows cp1252/'charmap' crash) must
        # never block sign-in. Purge the bad cache and retry ONCE without persistence —
        # login still succeeds; the only cost is re-auth after the next server restart.
        if _is_cache_persistence_error(exc):
            logger.warning(
                "Token cache unreadable (%s) — purging and retrying without persistence.", exc
            )
            _purge_msal_token_cache()
            try:
                cred = InteractiveBrowserCredential(timeout=_BROWSER_AUTH_TIMEOUT_S)
                record = cred.authenticate(scopes=["https://management.azure.com/.default"])
                _save_auth_record(record, session_id)
                token = cred.get_token("https://management.azure.com/.default")
            except Exception as exc2:
                raise AzureConfigError(
                    f"Browser login failed or was cancelled. Error: {exc2}"
                )
        else:
            raise AzureConfigError(
                f"Browser login failed or was cancelled. Error: {exc}"
            )

    # Decode JWT to extract identity
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
    _save_credential_info(info, session_id)

    logger.info("Browser login succeeded: %s", info.get("name", "?"))
    return info


def get_browser_credential_info(session_id=None) -> dict:
    """Return this session's cached browser credential identity, or empty dict.
    Also tries restoring from this session's disk cache on first call."""
    if not _get_info(session_id):
        _restore_browser_credential(session_id)
    return _get_info(session_id)


def get_browser_credential(session_id=None):
    """Return this session's browser credential object (restoring from this
    session's disk cache if needed), or None. Used by background workers that
    need the credential without rebuilding it."""
    cred = _get_cred(session_id)
    if cred is not None:
        return cred
    if _restore_browser_credential(session_id):
        return _get_cred(session_id)
    return None


def clear_browser_credential(session_id=None) -> None:
    """Clear this session's cached browser credential (sign-out) — both
    in-process and on disk. Other sessions are unaffected."""
    _clear_session(session_id)
    _clear_persistent_cache(session_id)


def _build_credential(cfg: dict, session_id=None):
    """Build Azure credential for this session — prefers the session's cached
    browser credential (including restored from disk), then falls back to
    DefaultAzureCredential (the server's ambient az-login / managed identity).
    """
    # Try this session's in-memory credential first
    cred = _get_cred(session_id)
    if cred is not None:
        logger.info("Azure auth: reusing cached browser credential")
        return cred
    # Try restoring this session's credential from its persistent cache
    if _restore_browser_credential(session_id):
        logger.info("Azure auth: restored browser credential from disk cache")
        return _get_cred(session_id)

    from azure.identity import DefaultAzureCredential
    import logging as _logging
    # Suppress noisy credential chain errors in the console
    _logging.getLogger("azure.identity").setLevel(_logging.ERROR)
    logger.info("Azure auth: DefaultAzureCredential (az login / personal identity)")
    try:
        cred = DefaultAzureCredential()
        # Eagerly verify the credential can obtain a token
        cred.get_token("https://management.azure.com/.default")
        return cred
    except Exception:
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
    
    Each metric returns:
      metric_name          → overall period average (for CPU AVG column)
      metric_name__recent  → last data point / most recent hour (for CPU% column)
    
    Strategy: try ALL metrics in a single API call first (fastest).
    If that fails, fall back to platform-only + individual disk queries.
    """
    from azure.monitor.query import MetricAggregationType
    import time as _t
    t0 = _t.perf_counter()
    vm_label = rid.split("/")[-1]
    metrics = {}

    def _extract(response):
        for m in response.metrics:
            vals = [dp.average for ts in m.timeseries for dp in ts.data if dp.average is not None]
            if vals:
                metrics[m.name] = sum(vals) / len(vals)        # period average
                metrics[m.name + "__recent"] = vals[-1]         # most recent data point

    # Fast path: all metrics in one call
    try:
        response = client.query_resource(
            resource_uri=rid,
            metric_names=list(_VM_METRICS),
            timespan=(start_time, end_time),
            granularity=granularity,
            aggregations=[MetricAggregationType.AVERAGE],
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
            aggregations=[MetricAggregationType.AVERAGE],
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
                aggregations=[MetricAggregationType.AVERAGE],
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
    Returns (rid, {metric_name: [{t: iso_str, v: float}, ...], ...}, {metric_name: {true_max, true_min}}).
    The third element contains true Max/Min aggregation values for accurate header stats.
    """
    from azure.monitor.query import MetricAggregationType
    vm_label = rid.split("/")[-1]
    series = {}
    true_extremes = {}

    try:
        # Primary query: Average aggregation for chart line rendering
        response = client.query_resource(
            resource_uri=rid,
            metric_names=list(_VM_METRICS),
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
        logger.warning("Time-series query failed for %s: %s", vm_label, exc)

    try:
        # Secondary query: Max/Min aggregation for accurate header stats
        extremes_response = client.query_resource(
            resource_uri=rid,
            metric_names=list(_VM_METRICS),
            timespan=(start_time, end_time),
            granularity=granularity,
            aggregations=[MetricAggregationType.MAXIMUM, MetricAggregationType.MINIMUM],
        )
        for m in extremes_response.metrics:
            max_val = None
            min_val = None
            for ts in m.timeseries:
                for dp in ts.data:
                    if dp.maximum is not None:
                        max_val = max(max_val, dp.maximum) if max_val is not None else dp.maximum
                    if dp.minimum is not None:
                        min_val = min(min_val, dp.minimum) if min_val is not None else dp.minimum
            true_extremes[m.name] = {
                "true_max": round(max_val, 4) if max_val is not None else None,
                "true_min": round(min_val, 4) if min_val is not None else None,
            }
    except Exception as exc:
        logger.warning("Max/Min aggregation query failed for %s: %s", vm_label, exc)

    return (rid, series, true_extremes)


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


def _metric_elevation(metric_label: str) -> dict:
    """Single source of truth for 'is this metric elevated' bands.

    Reads the canonical CPU/MEM/DISK warn/crit thresholds from ``pe_config``
    (live — picks up Settings overrides after ``pe_config.reload()``), so the
    spike detector, per-VM hot-hours, and fleet hot-hours can no longer drift
    apart with parallel hardcoded tables.

    Returns warn/crit in USED-% terms (higher = worse). ``invert`` marks metrics
    whose RAW samples are 'available %' (memory) — callers working in that space
    convert via ``100 - used``.
    """
    from services import pe_config
    name = (metric_label or "").lower()
    if "cpu" in name:
        return {"metric": "cpu", "warn": float(pe_config.CPU_WARN),
                "crit": float(pe_config.CPU_CRIT), "invert": False}
    if "mem" in name:
        return {"metric": "mem", "warn": float(pe_config.MEM_WARN),
                "crit": float(pe_config.MEM_CRIT), "invert": True}
    if "disk" in name:
        return {"metric": "disk", "warn": float(pe_config.DISK_WARN),
                "crit": float(pe_config.DISK_CRIT), "invert": False}
    return {"metric": "other", "warn": 80.0, "crit": 90.0, "invert": False}


def _abs_breach_cfg(metric_name: str) -> dict | None:
    """Absolute-breach thresholds for the spike detector, derived from the same
    canonical ``pe_config`` bands as ``_metric_elevation`` (single source).

    Memory is detected in 'available %' space (lower = worse), so used→available
    is converted as ``100 - used``. ``min_minutes`` is the spike-duration gate and
    stays metric-specific (orthogonal to the elevation threshold).
    """
    band = _metric_elevation(metric_name)
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
    warn, crit = band["warn"], band["crit"]
    conf = "high" if z >= z_crit else "medium" if z >= 2.0 else "low"
    pk, du, zr = round(used_peak, 1), int(dur_min), round(z, 1)
    if used_peak >= crit:
        if dur_min > 30:
            return {"severity": "critical_sustained", "reason_code": "abs_crit_sustained",
                    "severity_reason": f"{pk:.0f}% ≥ {crit:.0f}% crit for {du}min",
                    "confidence": "high", "threshold": crit, "peak_pct": pk,
                    "duration_min": du, "z_score": zr}
        if dur_min < 5:
            return {"severity": "warning", "reason_code": "abs_crit_brief",
                    "severity_reason": f"{pk:.0f}% ≥ {crit:.0f}% crit but only {du}min — possible artifact",
                    "confidence": conf, "threshold": crit, "peak_pct": pk,
                    "duration_min": du, "z_score": zr}
        return {"severity": "critical", "reason_code": "abs_crit",
                "severity_reason": f"{pk:.0f}% ≥ {crit:.0f}% crit for {du}min",
                "confidence": "high", "threshold": crit, "peak_pct": pk,
                "duration_min": du, "z_score": zr}
    if used_peak >= warn:
        return {"severity": "warning", "reason_code": "abs_warn",
                "severity_reason": f"{pk:.0f}% ≥ {warn:.0f}% warn band",
                "confidence": conf, "threshold": warn, "peak_pct": pk,
                "duration_min": du, "z_score": zr}
    return {"severity": "notable", "reason_code": "stat_anomaly_immaterial",
            "severity_reason": f"statistical anomaly (z={zr}) but {pk:.0f}% < {warn:.0f}% warn — not operationally material",
            "confidence": conf, "threshold": warn, "peak_pct": pk,
            "duration_min": du, "z_score": zr}


def _detect_spikes(series_points: list, threshold_sigma: float = 2.0,
                   metric_name: str = "") -> list:
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

    # Metric-specific z-score thresholds
    mn = (metric_name or "").lower()
    if "cpu" in mn:
        z_critical = 2.5   # CPU has natural batch variance
    elif "memory" in mn or "mem" in mn:
        z_critical = 2.5   # Memory available dips are significant
    elif "disk" in mn:
        z_critical = 4.0   # Near-zero baseline → high z from tiny changes
    else:
        z_critical = 3.0

    # Absolute thresholds (Classifier 2) — chronic breach detection.
    # Sourced from the canonical pe_config bands via _abs_breach_cfg, so the
    # spike detector, per-VM hot-hours, and fleet hot-hours all read ONE shared
    # threshold set instead of three parallel hardcoded tables.
    abs_cfg = _abs_breach_cfg(metric_name)
    band = _metric_elevation(metric_name)   # used-% warn/crit for the abs-significance gate

    spikes = []

    # ── Classifier 1: Z-score spike detection ──
    # For "Available Memory %", a SPIKE is a DROP (negative z).
    # For CPU/Disk, a SPIKE is a RISE (positive z).
    is_inverted_metric = "memory" in mn or "mem" in mn  # Available Memory: lower = worse
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
            if effective_z >= threshold_sigma:
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
                            spikes.append(make_spike_record(
                                start=breach_start, end=series_points[i - 1]["t"],
                                peak=round(breach_peak, 2), peak_time=breach_peak_time,
                                duration_min=dur_min, severity=sev,
                                reason_code="abs_sustained" if dur_min > 60 else "abs_breach",
                                severity_reason=f"sustained absolute breach {dur_min}min ≥ {min_dur}min",
                                confidence="high", detection="absolute_threshold",
                                z_score=round((breach_peak - mean) / std, 2) if std > 0.001 else 0,
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
                    spikes.append(make_spike_record(
                        start=breach_start, end=series_points[-1]["t"],
                        peak=round(breach_peak, 2), peak_time=breach_peak_time,
                        duration_min=dur_min, severity=sev,
                        reason_code="abs_sustained" if dur_min > 60 else "abs_breach",
                        severity_reason=f"sustained absolute breach {dur_min}min ≥ {min_dur}min",
                        confidence="high", detection="absolute_threshold",
                        z_score=round((breach_peak - mean) / std, 2) if std > 0.001 else 0,
                        mean=round(mean, 2), std=round(std, 2),
                        threshold=crit_thresh if breach_severity == "critical" else warn_thresh,
                        peak_pct=round(used_pk, 1),
                    ))

    return spikes


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
            ratio = day_count / days_observed
            if day_count >= min_occ and ratio >= min_ratio:
                metrics_hit = list({e["metric"] for e in events})
                worst = max(events, key=lambda e: e["z_score"])
                pct = round(ratio * 100)
                patterns.append({
                    "type": "recurring_time",
                    "severity": "critical" if worst["z_score"] >= 3.0 else "high",
                    "title": f"Recurring spikes at ~{hour:02d}:00 on {vm_name} ({day_count}/{round(days_observed)} days, {pct}%)",
                    "description": (
                        f"Spikes recurred on {day_count} distinct days "
                        f"({pct}% of {round(days_observed)} days observed) around {hour:02d}:00 UTC "
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
            })

    # ── 3. Sustained high utilization (mean above threshold) ──
    for vm_name, metric_spikes in all_vm_spikes.items():
        # We get stats passed separately, but we can flag VMs with many spikes
        for metric, spikes in metric_spikes.items():
            critical_count = sum(1 for s in spikes if s["severity"] == "critical")
            total_dur = sum(s.get("duration_min", 0) for s in spikes)
            if critical_count >= 3 or total_dur >= 60:
                patterns.append({
                    "type": "sustained_pressure",
                    "severity": "critical",
                    "title": f"Sustained {metric} pressure on {vm_name}",
                    "description": (
                        f"{critical_count} critical spikes totaling {total_dur} min "
                        f"of elevated {metric}. This VM is under persistent load "
                        f"and may require capacity investigation."
                    ),
                    "vms": [vm_name],
                    "count": critical_count,
                    "total_duration_min": total_dur,
                })

    # Sort by severity (critical first) then by peak z-score descending
    sev_order = {"critical": 0, "high": 1}
    patterns.sort(key=lambda p: (sev_order.get(p["severity"], 9), -p.get("peak_z", 0)))
    return patterns


def fetch_vm_timeseries(credential, resource_ids: List[str],
                        hours_back: int,
                        start_utc: Optional[datetime] = None,
                        end_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Fetch time-series data + spike detection for a list of VMs.
    
    When start_utc/end_utc are provided they override hours_back for the
    query window (used for custom time-range deep dives from the UI).
    
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
                rid, series, true_extremes = future.result()
                vm_name = rid.split("/")[-1].lower()

                # Compute stats and spikes per metric
                stats = {}
                spikes = {}
                for metric_name, points in series.items():
                    # Skip Available Memory Bytes from spike detection and chart stats
                    # — raw byte values are not percentages; only use the % metric
                    if metric_name == "Available Memory Bytes":
                        # Store as metadata for unit-aware display, not as a charted metric
                        vals = [p["v"] for p in points]
                        if vals:
                            stats[metric_name] = {
                                "mean": round(sum(vals) / len(vals), 2),
                                "max": round(max(vals), 2),
                                "min": round(min(vals), 2),
                                "std": 0,
                                "p95": 0,
                                "count": len(vals),
                                "unit": "bytes",
                            }
                        continue

                    vals = [p["v"] for p in points]
                    if vals:
                        sorted_vals = sorted(vals)
                        p95_idx = int(len(sorted_vals) * 0.95)
                        p5_idx  = int(len(sorted_vals) * 0.05)
                        mean_v = sum(vals) / len(vals)
                        var_v = sum((v - mean_v) ** 2 for v in vals) / len(vals)

                        # Use true Max/Min from Azure when available, fall back to avg-based
                        extremes = true_extremes.get(metric_name, {})
                        true_max = extremes.get("true_max")
                        true_min = extremes.get("true_min")

                        # Outlier filter for max: if true_max is >2x the P95 and only
                        # appears in a single data point, flag it as potentially anomalous
                        max_val = true_max if true_max is not None else max(vals)
                        p95_val = sorted_vals[min(p95_idx, len(sorted_vals) - 1)]
                        p5_val  = sorted_vals[min(p5_idx,  len(sorted_vals) - 1)]
                        max_anomalous = (max_val > p95_val * 2) and (max_val > mean_v + 4 * (var_v ** 0.5))

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
                    spikes[metric_name] = _detect_spikes(points, metric_name=metric_name)

                result[vm_name] = {
                    "resource_id": rid,
                    "series": series,
                    "spikes": spikes,
                    "stats": stats,
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

    return {
        "vms": result,
        "patterns": patterns,
        "baseline": baseline,
        "window": {
            "hours_back": hours_back,
            "grain": _grain_label,
            "timezone": "UTC",
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

    # Key metrics to analyze
    _ANALYSIS_METRICS = [
        "Percentage CPU",
        "Available Memory Percentage",
        "OS Disk Bandwidth Consumed Percentage",
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
            recurring_spikes = []
            spikes_data = vm_info.get("spikes", {}).get(metric_name, [])
            if is_mem_avail:
                spikes_data = vm_info.get("spikes", {}).get("Available Memory Percentage", [])
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
                    recurring_spikes.append({
                        "hour": hour,
                        "day_count": len(unique_days),
                        "days": sorted(unique_days),
                        "day_names": sorted(set(e["day_name"] for e in events)),
                        "worst_peak": max(e["peak"] for e in events),
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


def _vm_total_memory_bytes(credential, subscription_id: str, vm_size: str) -> Optional[float]:
    """
    Look up total RAM for a VM size from the Azure Compute SKUs list.
    Returns bytes, or None if unavailable.
    Cached in-process to avoid repeated API calls for the same size.
    """
    if not vm_size:
        return None
    cache = _vm_total_memory_bytes.__dict__.setdefault("_cache", {})
    if vm_size in cache:
        return cache[vm_size]
    try:
        from azure.mgmt.compute import ComputeManagementClient
        compute = ComputeManagementClient(credential, subscription_id)
        # list_skus is subscription-scoped; we just need any location result
        for sku in compute.resource_skus.list(filter=f"name eq '{vm_size}'"):
            for cap in (sku.capabilities or []):
                if cap.name == "MemoryGB":
                    val = float(cap.value) * _BYTES_PER_GB
                    cache[vm_size] = val
                    return val
    except Exception:
        pass
    cache[vm_size] = None
    return None


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
        })

    # Sort: DB first, then SRE, then APP, then alphabetically
    order = {"DB": 0, "SRE": 1, "APP": 2}
    discovered.sort(key=lambda v: (order.get(v["type"], 9), v["name"]))
    return discovered


def search_vms(credential, query: str,
               subscription_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Search for VMs across all (or specified) subscriptions using Azure
    Resource Graph.  Matches VM name, resource group, tags (CustomerName,
    Application, Environment_Type, etc.).

    Returns the same shape as discover_vms() so the frontend can render
    the same VM table.
    """
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
    q = (query or "").strip().replace("'", "\\'")
    if not q:
        raise AzureConfigError("Search query is empty.")

    # KQL: search VMs where name, RG, or any tag value contains the query
    kql = f"""
    Resources
    | where type =~ 'microsoft.compute/virtualMachines'
    | where name contains '{q}'
       or resourceGroup contains '{q}'
       or tostring(tags) contains '{q}'
    | project id, name, location, resourceGroup, subscriptionId,
              vmSize = tostring(properties.hardwareProfile.vmSize),
              tags,
              powerState = tostring(properties.extended.instanceView.powerState.code)
    | order by name asc
    | limit 200
    """

    client = ResourceGraphClient(credential)

    opts = QueryRequestOptions(result_format="objectArray")
    req_kwargs = {"query": kql, "options": opts}
    if subscription_ids:
        req_kwargs["subscriptions"] = subscription_ids

    request = QueryRequest(**req_kwargs)

    try:
        response = client.resources(request)
    except Exception as exc:
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
            "customer":       tags.get("CustomerName") or tags.get("customerName") or "",
            "application":    tags.get("Application") or tags.get("application") or "",
            "environment":    tags.get("Environment_Type") or tags.get("environment_type")
                              or tags.get("Environment") or "",
        })

    order = {"DB": 0, "SRE": 1, "APP": 2}
    results.sort(key=lambda v: (order.get(v["type"], 9), v["name"]))
    logger.info("Resource Graph search '%s' → %d VMs", query, len(results))
    return results


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
    Percentage to avoid slow SKU API calls.  Falls back to SKU lookup only
    when both metrics are missing.
    """
    import re as _re
    import time as _time

    resource_ids = [v["resource_id"] for v in vms]

    t0 = _time.perf_counter()
    metrics_map = _query_metrics(credential, resource_ids, hours_back)
    t_metrics = _time.perf_counter() - t0
    logger.info("Metrics query took %.1fs for %d VMs", t_metrics, len(vms))

    servers: List[Dict[str, Any]] = []
    sku_needed: list = []  # VMs that need SKU fallback (no metrics for memory)

    for vm in vms:
        rid  = vm["resource_id"]
        name = vm["name"]
        m    = metrics_map.get(rid, {})

        cpu_pct_recent = m.get("Percentage CPU__recent")
        cpu_pct_avg = round(m.get("Percentage CPU", 0.0), 2)
        # CPU%: use most recent 1h data point; CPU AVG: use period average
        cpu_pct = round(cpu_pct_recent, 2) if cpu_pct_recent is not None else cpu_pct_avg

        avail_pct   = m.get("Available Memory Percentage")
        avail_bytes = m.get("Available Memory Bytes")
        mem_pct = 0.0
        mem_total_gb = 0.0

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
            sub_match = _re.match(r"/subscriptions/([^/]+)/", rid, _re.IGNORECASE)
            if sub_match and vm.get("vm_size"):
                sku_needed.append((len(servers), sub_match.group(1), vm["vm_size"]))
        elif avail_bytes is not None:
            # Have bytes but not percentage — need SKU for total
            sub_match = _re.match(r"/subscriptions/([^/]+)/", rid, _re.IGNORECASE)
            if sub_match and vm.get("vm_size"):
                sku_needed.append((len(servers), sub_match.group(1), vm["vm_size"]))

        disk_pct = round(
            m.get("OS Disk Bandwidth Consumed Percentage",
                   m.get("Data Disk Bandwidth Consumed Percentage", 0.0)),
            2,
        )

        servers.append({
            "host":          name.lower(),
            "server":        name.lower(),
            "type":          _infer_server_type(name, vm.get("tags"), vm.get("rg", "")),
            "cpu_used":      cpu_pct,
            "cpu_avg":       cpu_pct_avg,
            "mem_used":      mem_pct,
            "mem_total_gb":  mem_total_gb,
            "disk_used_max": disk_pct,
            "cpu_pct":       cpu_pct,
            "mem_pct":       mem_pct,
            "disk_pct":      disk_pct,
            "resource_id":   rid,
            "location":      vm["location"],
            "vm_size":       vm["vm_size"],
            "resource_group":vm["rg"],
            "source":        "azure_monitor",
            "hours_back":    hours_back,
        })

    # SKU fallback: only for VMs missing both memory metrics (rare)
    if sku_needed:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        logger.info("SKU fallback needed for %d VMs (missing memory metrics)", len(sku_needed))
        t1 = _time.perf_counter()
        unique_sizes = {(s, sz) for _, s, sz in sku_needed}

        def _lookup(pair):
            return pair, _vm_total_memory_bytes(credential, pair[0], pair[1])

        with ThreadPoolExecutor(max_workers=min(5, len(unique_sizes))) as pool:
            results = {pair: val for pair, val in pool.map(lambda p: (p, _vm_total_memory_bytes(credential, p[0], p[1])), unique_sizes)}

        for idx, sub_id, vm_size in sku_needed:
            total_bytes = results.get((sub_id, vm_size))
            if total_bytes and total_bytes > 0:
                servers[idx]["mem_total_gb"] = round(total_bytes / _BYTES_PER_GB, 2)
                avail_bytes = metrics_map.get(servers[idx]["resource_id"], {}).get("Available Memory Bytes")
                if avail_bytes is not None and servers[idx]["mem_pct"] == 0.0:
                    servers[idx]["mem_pct"] = round(max(0.0, min(100.0, (1.0 - avail_bytes / total_bytes) * 100.0)), 2)
                    servers[idx]["mem_used"] = servers[idx]["mem_pct"]

        logger.info("SKU fallback took %.1fs", _time.perf_counter() - t1)

    total_time = _time.perf_counter() - t0
    logger.info("Azure fetch complete — %d servers in %.1fs (metrics: %.1fs)", len(servers), total_time, t_metrics)
    return servers


# ─────────────────────────────────────────────────────────────────
# VM inventory pre-warm cache
# ─────────────────────────────────────────────────────────────────
# Populated by prewarm_vm_inventory (background, triggered after login).
# clear_vm_inventory_cache wipes it on logout / credential change.
# get_vm_prewarm_state returns the current state for the polling endpoint.

_vm_inventory_cache: Dict[str, Any] = {}
_vm_prewarm_state: Dict[str, Any] = {"status": "idle", "vm_count": 0, "ts": 0.0, "error": None}
_vm_prewarm_lock = __import__("threading").Lock()


def clear_vm_inventory_cache() -> None:
    """Wipe the VM inventory cache so a different user/credential never sees stale data."""
    global _vm_inventory_cache, _vm_prewarm_state
    with _vm_prewarm_lock:
        _vm_inventory_cache.clear()
        _vm_prewarm_state = {"status": "idle", "vm_count": 0, "ts": 0.0, "error": None}


def get_vm_prewarm_state() -> Dict[str, Any]:
    """Return the current VM pre-warm status (no network call)."""
    with _vm_prewarm_lock:
        return dict(_vm_prewarm_state)


def prewarm_vm_inventory(credential, subscription_id: str,
                         resource_group: Optional[str] = None,
                         session_id=None) -> None:
    """Background: discover all VMs and cache them so search is instantaneous.

    Runs discover_vms() once and stores the result in _vm_inventory_cache.
    Subsequent calls to search_vms can check the cache first before hitting Azure.
    """
    import threading as _threading

    def _worker():
        global _vm_prewarm_state
        with _vm_prewarm_lock:
            if _vm_prewarm_state.get("status") == "warming":
                return  # already running
            _vm_prewarm_state = {"status": "warming", "vm_count": 0, "ts": __import__("time").time(), "error": None}
        try:
            cfg = {"credential": credential, "subscription_id": subscription_id}
            vms = discover_vms(cfg, resource_group=resource_group, session_id=session_id)
            with _vm_prewarm_lock:
                _vm_inventory_cache.clear()
                _vm_inventory_cache[subscription_id] = vms
                _vm_prewarm_state = {
                    "status": "ready",
                    "vm_count": len(vms),
                    "ts": __import__("time").time(),
                    "error": None,
                }
            logger.info("VM inventory pre-warm complete — %d VMs cached for sub %s", len(vms), subscription_id)
        except Exception as _e:
            with _vm_prewarm_lock:
                _vm_prewarm_state = {
                    "status": "error",
                    "vm_count": 0,
                    "ts": __import__("time").time(),
                    "error": str(_e),
                }
            logger.warning("VM inventory pre-warm failed: %s", _e)

    _threading.Thread(target=_worker, daemon=True).start()
