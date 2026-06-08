"""
_start_helper.py  --  called by start.bat instead of _seed_config.py + _find_port.py

Combines both tasks in a single Python process, saving ~1.5-2s of
Python interpreter startup overhead on every dashboard launch.

Outputs: one integer (the free port) to stdout — nothing else.
"""
from __future__ import annotations

import os
import socket
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 1. Seed config (idempotent, fast) ────────────────────────────────────────
try:
    from services.config_store import get, set as cfg_set  # noqa: F401
    # Only write defaults if missing — never overwrite user settings
    _DEFAULTS = {
        "gemini_api_key":      "",
        "nvidia_api_key":      "",
        "vision_provider":     "gemini",
        "ai_text_provider":    "nvidia",
        "ai_text_model":       "openai/gpt-oss-120b",
        "ai_post_upload":      True,
        "daily_sla_hrs":       6.0,
        "weekly_sla_hrs":      17.0,
        "biweekly_sla_hrs":    17.0,
        "monthly_sla_hrs":     17.0,
        "custom_sla_hrs":      6.0,
        "sla_mode":            "daily",
        "sla_buffer_warn":     15.0,
        "sla_atrisk_pct":      15.0,
        "sla_longjob_pct":     40.0,
        "cpu_warning":         75.0,
        "cpu_critical":        90.0,
        "mem_warning":         70.0,
        "mem_critical":        80.0,
        "disk_warning":        70.0,
        "disk_critical":       85.0,
        "batch_fail_rate":     5.0,
        "zero_dur_flag":       True,
        "benchmark_threshold": 10.0,
        "anomaly_z_threshold": 2.0,
        "sow_dfu":             499999.0,
        "sow_sku":             80000.0,
        "sow_orders":          200000.0,
        "sow_batch_jobs":      450.0,
        "azure_subscription_id": "",
        "azure_resource_group":  "",
    }
    for k, v in _DEFAULTS.items():
        existing = get(k)
        # Only set if truly absent or corrupted (dict/None for numeric fields)
        if existing is None or (isinstance(v, float) and isinstance(existing, dict)):
            cfg_set(k, v)
except Exception:
    pass  # non-fatal — server will still start

# ── 2. Find a free port (kill squatter if needed — zero netstat) ─────────────
import ctypes
import struct

_SO_EXCL = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)
CANDIDATES = [8765, 8080, 8888, 9000, 9090, 9999, 7878, 5000]


def _free(port: int) -> bool:
    """True if we can bind the port right now."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.setsockopt(socket.SOL_SOCKET, _SO_EXCL, 1)
            except OSError:
                pass
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _kill_squatter(port: int) -> bool:
    """
    Kill whatever PID is LISTENING on *port* using Windows iphlpapi directly.
    No subprocess, no netstat — completes in <5 ms.
    Returns False immediately when no live PID owns the port (TIME_WAIT etc.)
    so the caller moves on to the next candidate without any blocking call.
    """
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        # TCP_TABLE_OWNER_PID_LISTENER = 3, AF_INET = 2
        size = ctypes.c_ulong(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)
        buf = (ctypes.c_byte * size.value)()
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) == 0:
            count = struct.unpack_from("I", buf, 0)[0]
            offset = 4
            for _ in range(count):
                # Row layout: state(4) localAddr(4) localPort(4) remoteAddr(4) remotePort(4) ownerPid(4)
                state, _la, lport_net, _ra, _rp, pid = struct.unpack_from("6I", buf, offset)
                offset += 24
                # localPort is big-endian in the table
                lport = ((lport_net & 0xFF) << 8) | ((lport_net >> 8) & 0xFF)
                if lport == port and pid and pid != os.getpid():
                    k32 = ctypes.windll.kernel32
                    h = k32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE = 0x0001
                    if h:
                        k32.TerminateProcess(h, 1)
                        k32.CloseHandle(h)
                        return True
    except Exception:
        pass
    # No live PID found (TIME_WAIT / CLOSE_WAIT / iphlpapi unavailable) — don't block.
    return False


# Kill any squatter on the preferred port, then find a free slot
if not _free(8765):
    _kill_squatter(8765)
    # Brief yield for the OS to release the port after termination
    import time as _t; _t.sleep(0.05)

port = next((p for p in CANDIDATES if _free(p)), None)
if port is None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

print(port)
