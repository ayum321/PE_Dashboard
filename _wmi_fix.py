"""
_wmi_fix.py — PE Dashboard WMI bypass patch for Python 3.14 on Windows.

Python 3.14 on Windows calls _get_machine_win32() which makes a WMI query
that can hang indefinitely when the WMI service is deadlocked.
This patch replaces the WMI-based machine/uname calls with fast env-var
and registry fallbacks so that platform.machine() never blocks.

start.bat copies this file into the venv as sitecustomize.py, which Python
executes automatically at startup before any user code runs.
"""
from __future__ import annotations

import os
import platform
import sys


def _fast_machine_win32() -> str:
    """Return CPU architecture string without WMI (never blocks)."""
    # 1) Environment variables (always available on Windows)
    arch = (
        os.environ.get("PROCESSOR_ARCHITEW6432", "")
        or os.environ.get("PROCESSOR_ARCHITECTURE", "")
    )
    if arch:
        return arch.upper()

    # 2) Lightweight registry read (no WMI, no subprocess)
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            arch, _ = winreg.QueryValueEx(key, "PROCESSOR_ARCHITECTURE")
            if arch:
                return arch.upper()
    except Exception:
        pass

    # 3) Python pointer size fallback
    return "AMD64" if sys.maxsize > 2**32 else "x86"


def _patch_platform() -> None:
    """Patch platform module to bypass WMI calls."""
    # Only needed when _wmi is importable (Python 3.14 specific behaviour)
    try:
        import _wmi  # noqa: F401
    except ImportError:
        return

    if hasattr(platform, "_get_machine_win32"):
        platform._get_machine_win32 = _fast_machine_win32  # type: ignore[attr-defined]

    # Pre-populate uname cache so platform.uname()/platform.machine() never
    # calls _get_machine_win32() or _Processor.get() (both hit WMI).
    # Python 3.14: uname_result has 5 positional fields only —
    # system, node, release, version, machine. 'processor' is a cached_property.
    if getattr(platform, "_uname_cache", None) is None:
        machine = _fast_machine_win32()
        try:
            import socket
            node = socket.gethostname()
        except Exception:
            node = ""
        platform._uname_cache = platform.uname_result(  # type: ignore[attr-defined]
            "Windows", node, "", "", machine
        )


_patch_platform()
