"""
PE Dashboard — Dev Server (VS Code auto-start)

Handles everything automatically:
  1. Validates JS syntax (blocks if broken)
  2. Kills any stale process on port 8765
  3. Starts uvicorn with --reload (auto-restart on file changes)
  4. Opens browser

Run manually:  py -3.14 _dev_server.py
Or let VS Code run it automatically when the folder opens.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

HOST = "127.0.0.1"
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

# Port candidates — tries in order, picks the first free one
PORT_CANDIDATES = [8765, 8080, 8888, 9000, 9090, 9999, 7878, 5000]


def _validate_js() -> bool:
    """Check brace balance in JS files. Returns True if OK."""
    ok = True
    for name in ("static/app.js", "static/deep_dive.js"):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        balance = sum(1 if c == "{" else -1 if c == "}" else 0 for c in src)
        if balance != 0:
            print(f"\n  ✗ {name}: brace balance {balance:+d} — FIX BEFORE RUNNING")
            print(f"    Run:  py -3.14 _validate_js.py  for details\n")
            ok = False
        else:
            print(f"  ✓ {name}: OK")
    return ok


def _kill_port(port: int) -> None:
    """Kill any process listening on the given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "LISTENING" in line and f":{port} " in line:
                pid = line.strip().split()[-1]
                if pid.isdigit():
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=5
                    )
                    print(f"  Killed stale PID {pid} on port {port}")
        time.sleep(1)
    except Exception as e:
        print(f"  Port cleanup warning: {e}")


def _open_browser(host: str, port: int) -> None:
    """Open browser after server is ready."""
    import webbrowser
    import urllib.request

    url = f"http://{host}:{port}/"
    # Poll until server responds (up to 10 seconds)
    for _ in range(20):
        try:
            urllib.request.urlopen(f"{url}api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    # Add cache-buster
    webbrowser.open(f"{url}?_={int(time.time())}")


def main() -> int:
    print("\n  ═══════════════════════════════════════════")
    print("   PE Dashboard — Dev Server")
    print("  ═══════════════════════════════════════════\n")

    # Step 1: Validate JS
    print("  [1/3] Validating JavaScript...")
    if not _validate_js():
        return 1

    # Step 2: Find a free port (kill stale process on preferred port first)
    print(f"\n  [2/3] Finding free port...")
    from _find_port import find_free_port, is_free
    # Try preferred port first — kill stale process if occupied
    preferred = PORT_CANDIDATES[0]
    if not is_free(preferred):
        _kill_port(preferred)
    port = find_free_port(PORT_CANDIDATES)
    print(f"        Port : {port}")

    # Step 3: Start server
    print(f"\n  [3/3] Starting server with auto-reload...")
    print(f"\n  Dashboard : http://{HOST}:{port}/")
    print(f"  Auto-reload watches: routers/ services/ static/ templates/")
    print(f"  Save file + refresh browser = changes live\n")

    # Open browser in background
    import threading
    threading.Thread(target=_open_browser, args=(HOST, port), daemon=True).start()

    # Launch uvicorn in its own Windows process group so VS Code terminal
    # cleanup (Ctrl+C / SIGTERM to THIS script) cannot cascade into uvicorn.
    # We use Popen (non-blocking) and catch KeyboardInterrupt so the server
    # keeps running even when VS Code closes the terminal that started it.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen([
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", HOST, "--port", str(port),
        "--reload",
        "--reload-dir", "routers",
        "--reload-dir", "services",
        "--reload-dir", "templates",
        "--reload-dir", "static",
    ], creationflags=creationflags)

    # Save PID so restart/kill scripts can find it
    try:
        Path(".server_pid").write_text(str(proc.pid))
    except Exception:
        pass

    try:
        proc.wait()  # Stream uvicorn output; block until it exits normally
    except KeyboardInterrupt:
        # VS Code terminal closed / Ctrl+C pressed in the wrapper script.
        # Because uvicorn is in its own process group (CREATE_NEW_PROCESS_GROUP),
        # it does NOT receive this signal — it keeps running.
        print("\n\n  ─────────────────────────────────────────────────────")
        print(f"  PE Dashboard server is STILL RUNNING (PID {proc.pid})")
        print(f"  Dashboard : http://{HOST}:{port}/")
        print(f"  To stop   : Stop-Process -Id {proc.pid} -Force")
        print("  ─────────────────────────────────────────────────────\n")
        return 0

    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
