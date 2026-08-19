"""
Socket-polling browser auto-launcher for the PE Dashboard.

Waits until the FastAPI server is accepting TCP connections on the
configured host:port, then opens the dashboard URL in the default
browser (Chrome on Windows). Designed to be spawned in parallel with
`uvicorn` from start.bat.

Usage:
    python _open_browser.py [host] [port]
        host  default 127.0.0.1
        port  default 8765
"""
from __future__ import annotations

import json
import socket
import sys
import time
import urllib.request
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
POLL_INTERVAL_SEC = 0.25
TIMEOUT_SEC = 30.0
EXPECTED_SERVICE = "pe-audit-dashboard"


def wait_for_port(host: str, port: int, timeout: float = TIMEOUT_SEC) -> bool:
    """Block until <host>:<port> accepts a TCP connection or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(POLL_INTERVAL_SEC)
    return False


def verify_identity(host: str, port: int) -> bool:
    """Check /api/health to confirm the PE Dashboard is the one responding,
    not some other app squatting on the port."""
    url = f"http://{host}:{port}/api/health"
    for _ in range(5):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
                if data.get("service") == EXPECTED_SERVICE:
                    return True
                print(f"[browser] WARNING: port {port} serves '{data.get('service')}', not PE Dashboard!")
                return False
        except Exception:
            time.sleep(0.5)
    return False


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    # Cache-busting timestamp forces the browser to make a fresh HTTP request
    # instead of serving a previously cached page from the same port.
    url = f"http://{host}:{port}/?_={int(time.time())}"

    print(f"[browser] waiting for server on {host}:{port} ...")
    if not wait_for_port(host, port):
        print(f"[browser] timeout — server did not start within {TIMEOUT_SEC}s")
        return 1

    if not verify_identity(host, port):
        print(f"[browser] ABORT: port {port} is not serving PE Dashboard")
        return 3

    print(f"[browser] PE Dashboard verified — opening {url}")
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:
        print(f"[browser] failed to open browser: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
