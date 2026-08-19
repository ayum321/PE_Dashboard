"""
Stale-process reaper — kills leftover PE Dashboard server processes before a
fresh launch, so repeated runs can never pile up into the "machine is choked
with zombie Python" state that forces ephemeral-port fallback (e.g. :60371).

WHY THIS EXISTS
    start.bat used to only kill whatever was LISTENING on its 9 candidate
    ports. That misses three classes of leftovers that actually accumulate:
      1. uvicorn --reload WORKER children that got orphaned when the parent
         crashed or the cmd window was closed (they keep the app loaded but
         their cmdline has no "main:app", so a port/name scan never finds them).
      2. servers that fell back to an OS ephemeral port (60371) — not in the
         candidate list, so never cleared.
      3. processes stuck in non-LISTENING socket states holding the port.

WHAT IT DOES
    Attributes every Python process to THIS dashboard folder using two
    independent signals, then terminates the matches (and their child trees):
      * command-line signature  : "uvicorn" + "main:app"
      * working directory match  : cwd == this folder  AND the process is a
                                   uvicorn/multiprocessing worker (psutil only)
      * live descendants of any matched process (reaps reload workers)
      * orphaned multiprocessing spawn workers whose parent is already dead

    The launching process and its whole ancestor chain are PROTECTED, so this
    can never kill the cmd/python that started it.

USAGE
    python _cleanup_stale.py            # reap (default) — prints a summary
    python _cleanup_stale.py --report   # list only, kill nothing (dry run)
    python _cleanup_stale.py --quiet    # reap with minimal output

    Always exits 0 — a cleanup hiccup must never block the server launch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Unique command-line signature of the dashboard server process.
_SIG_PARTS = ("uvicorn", "main:app")
# Markers of a uvicorn --reload / multiprocessing worker child.
_WORKER_MARKERS = ("spawn_main", "--multiprocessing-fork", "resource_tracker")


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(f"        {msg}")


def _paths_equal(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except Exception:
        return False


# ── Process enumeration ──────────────────────────────────────────────
# Each record: {pid, ppid, name, cmd (lower), cwd}. cwd may be "" when we
# can't read it. Three backends, tried in order:
#   1. winapi — ctypes toolhelp snapshot + PEB read. Zero dependencies, no
#      WMI, fast even on a heavily congested box (this is the whole point:
#      WMI/CIM hangs under exactly the load this tool is meant to clear, and
#      pip-installing psutil at that moment also stalls). Gives cmdline + cwd.
#   2. psutil — used only if already importable (rich + reliable).
#   3. CIM   — PowerShell last resort, short timeout so it can't hang startup.

def _enum_winapi():
    """Windows-API process enumeration via ctypes — no deps, no WMI.

    Reads each Python process's command line and current directory straight
    from its PEB. Returns None on non-Windows or if the API path is
    unavailable, so the caller can fall through to another backend.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    try:
        ntdll = ctypes.WinDLL("ntdll")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return None

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    INVALID_HANDLE = wintypes.HANDLE(-1).value
    PTR = ctypes.sizeof(ctypes.c_void_p)
    if PTR != 8:
        # Offsets below are the x64 PEB layout. Bail on 32-bit to stay safe.
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
    ]

    def _read(handle, address, size):
        if not address:
            return None
        buf = ctypes.create_string_buffer(size)
        nread = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(
            handle, ctypes.c_void_p(address), buf, size, ctypes.byref(nread)
        )
        if not ok or nread.value < size:
            return None
        return buf.raw[: nread.value]

    def _read_ptr(handle, address):
        raw = _read(handle, address, PTR)
        if raw is None:
            return 0
        return int.from_bytes(raw, "little")

    def _read_unicode(handle, struct_addr):
        # UNICODE_STRING (x64): USHORT Length; USHORT Max; ULONG pad; PWSTR Buffer
        head = _read(handle, struct_addr, 16)
        if head is None:
            return ""
        length = int.from_bytes(head[0:2], "little")
        buffer_ptr = int.from_bytes(head[8:16], "little")
        if not length or not buffer_ptr or length > 32768:
            return ""
        raw = _read(handle, buffer_ptr, length)
        if raw is None:
            return ""
        try:
            return raw.decode("utf-16-le", "replace").rstrip("\x00")
        except Exception:
            return ""

    # PEB offsets (x64)
    OFF_PROCPARAMS = 0x20
    OFF_CURDIR = 0x38      # ProcessParameters -> CurrentDirectory.DosPath
    OFF_CMDLINE = 0x70     # ProcessParameters -> CommandLine

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return None

    procs = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = (entry.szExeFile or "").lower()
            pid = int(entry.th32ProcessID)
            ppid = int(entry.th32ParentProcessID)
            if "python" in name or "pythonw" in name:
                cmd, cwd = "", ""
                handle = k32.OpenProcess(
                    PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid
                )
                if handle:
                    try:
                        pbi = PROCESS_BASIC_INFORMATION()
                        retlen = wintypes.ULONG(0)
                        status = ntdll.NtQueryInformationProcess(
                            handle, 0, ctypes.byref(pbi),
                            ctypes.sizeof(pbi), ctypes.byref(retlen),
                        )
                        if status == 0 and pbi.PebBaseAddress:
                            peb = pbi.PebBaseAddress
                            params = _read_ptr(handle, peb + OFF_PROCPARAMS)
                            if params:
                                cmd = _read_unicode(handle, params + OFF_CMDLINE).lower()
                                cwd = _read_unicode(handle, params + OFF_CURDIR)
                    except Exception:
                        pass
                    finally:
                        k32.CloseHandle(handle)
                procs.append({
                    "pid": pid, "ppid": ppid, "name": name,
                    "cmd": cmd, "cwd": cwd,
                })
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return procs


def _enum_psutil():
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    procs = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if "python" not in name and "pythonw" not in name:
                continue
            cmd = " ".join(p.info.get("cmdline") or []).lower()
            try:
                cwd = p.cwd()
            except Exception:
                cwd = ""
            procs.append({
                "pid": int(p.info["pid"]),
                "ppid": int(p.info.get("ppid") or 0),
                "name": name,
                "cmd": cmd,
                "cwd": cwd or "",
            })
        except Exception:
            continue
    return procs


def _enum_cim():
    """PowerShell CIM fallback — no cwd, but gives cmdline + ppid reliably."""
    ps = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='python3.exe' "
        "OR Name='python3.14.exe' OR Name='python3.13.exe' OR Name='python3.12.exe' "
        "OR Name='python3.11.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=12,
        ).stdout.strip()
    except Exception:
        return []
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    procs = []
    for d in data:
        try:
            procs.append({
                "pid": int(d.get("ProcessId") or 0),
                "ppid": int(d.get("ParentProcessId") or 0),
                "name": (d.get("Name") or "").lower(),
                "cmd": (d.get("CommandLine") or "").lower(),
                "cwd": "",
            })
        except Exception:
            continue
    return procs


def _enumerate():
    """Return (process_records, backend_name). Tries the fast dependency-free
    Windows-API path first, then psutil if already present, then CIM."""
    procs = _enum_winapi()
    if procs:
        return procs, "winapi"
    procs = _enum_psutil()
    if procs:
        return procs, "psutil"
    return _enum_cim(), "cim"


# ── Attribution ──────────────────────────────────────────────────────

def _protected_pids(procs) -> set[int]:
    """Current process + its full ancestor chain — never kill these."""
    by_pid = {p["pid"]: p for p in procs}
    protected = set()
    pid = os.getpid()
    protected.add(pid)
    # Walk up the parent chain via the snapshot (covers the launching cmd/py).
    seen = set()
    cur = pid
    for _ in range(64):
        if cur in seen:
            break
        seen.add(cur)
        rec = by_pid.get(cur)
        if not rec:
            break
        ppid = rec.get("ppid") or 0
        if not ppid or ppid in protected:
            break
        protected.add(ppid)
        cur = ppid
    return protected


def _signature_match(p) -> bool:
    cmd = p["cmd"]
    return all(part in cmd for part in _SIG_PARTS)


def _cwd_worker_match(p) -> bool:
    """psutil-only: a python worker whose cwd is this dashboard folder."""
    if not p["cwd"] or not _paths_equal(p["cwd"], APP_DIR):
        return False
    cmd = p["cmd"]
    return (
        "uvicorn" in cmd
        or "main:app" in cmd
        or any(m in cmd for m in _WORKER_MARKERS)
    )


def select_targets(procs, protected) -> list[dict]:
    alive = {p["pid"] for p in procs}
    matched: dict[int, dict] = {}

    # Pass 1 — direct attribution (signature or cwd-worker).
    for p in procs:
        if p["pid"] in protected:
            continue
        if _signature_match(p) or _cwd_worker_match(p):
            matched[p["pid"]] = p

    # Pass 2 — live descendants of anything matched (reap reload workers whose
    # parent is still alive). Iterate to a fixed point for deep trees.
    changed = True
    while changed:
        changed = False
        for p in procs:
            pid = p["pid"]
            if pid in protected or pid in matched:
                continue
            if p.get("ppid") in matched:
                matched[pid] = p
                changed = True

    # Pass 3 — orphaned multiprocessing spawn workers (parent already dead).
    # Strong signal of a leftover uvicorn reload child. Conservative: only
    # python procs carrying the fork markers whose parent PID no longer exists.
    for p in procs:
        pid = p["pid"]
        if pid in protected or pid in matched:
            continue
        cmd = p["cmd"]
        if any(m in cmd for m in _WORKER_MARKERS):
            ppid = p.get("ppid") or 0
            if ppid and ppid not in alive:
                matched[pid] = p

    return list(matched.values())


# ── Termination ──────────────────────────────────────────────────────

def _kill(pid: int) -> bool:
    """Force-kill a PID and its child tree. Returns True on success."""
    try:
        r = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        # Last resort — direct os.kill (no tree).
        try:
            os.kill(pid, 9)
            return True
        except Exception:
            return False


def main() -> int:
    args = {a.lower() for a in sys.argv[1:]}
    report_only = "--report" in args or "--dry-run" in args
    quiet = "--quiet" in args

    procs, backend = _enumerate()
    if not procs:
        _log("No Python processes enumerated (nothing to clean).", quiet)
        return 0

    protected = _protected_pids(procs)
    targets = select_targets(procs, protected)

    if not targets:
        _log(f"No stale PE Dashboard processes found ({backend}).", quiet)
        return 0

    verb = "Would reap" if report_only else "Reaping"
    _log(f"{verb} {len(targets)} stale PE Dashboard process(es) [{backend}]:", quiet)
    killed = 0
    for t in sorted(targets, key=lambda x: x["pid"]):
        tag = t["cmd"][:70].strip() or t["name"]
        if report_only:
            _log(f"  PID {t['pid']}  {tag}", quiet)
            continue
        ok = _kill(t["pid"])
        killed += 1 if ok else 0
        _log(f"  {'killed' if ok else 'FAILED'} PID {t['pid']}  {tag}", quiet)

    if not report_only:
        _log(f"Cleaned {killed}/{len(targets)} stale process(es).", quiet)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Never let cleanup block a launch.
        print(f"        [cleanup] non-fatal: {exc}")
        sys.exit(0)
