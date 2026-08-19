"""
Auto-detect the first available TCP port from a candidate list.

Usage (from start.bat):
    for /f %%P in ('python _find_port.py') do set PORT=%%P

Prints a single integer to stdout — the first port in CANDIDATES that
can be bound on 127.0.0.1.  Falls back to an OS-assigned ephemeral
port if every candidate is busy (guarantees a result on any machine).

Windows-robust: uses SO_EXCLUSIVEADDRUSE to reject TIME_WAIT ports,
double-checks with a short delay to handle race conditions.
"""
from __future__ import annotations

import os
import socket
import sys
import time

# Preferred ports tried in order — edit freely
CANDIDATES: list[int] = [8000, 8765, 8080, 8888, 9000, 9090, 9999, 7878, 5000]

# Windows constant — not in the socket module
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)


def is_free(port: int) -> bool:
    """Return True if 127.0.0.1:<port> is genuinely available for binding.
    On Windows, uses SO_EXCLUSIVEADDRUSE to reject ports stuck in TIME_WAIT."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        # On Windows, SO_EXCLUSIVEADDRUSE prevents binding to TIME_WAIT ports
        if os.name == "nt":
            try:
                s.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
            except (OSError, AttributeError):
                pass
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_free_port(candidates: list[int]) -> int:
    """Find first truly free port — double-checks after a short delay."""
    for port in candidates:
        if is_free(port):
            # Double-check after a brief delay to catch race conditions
            time.sleep(0.05)
            if is_free(port):
                return port
    # OS fallback: bind to :0 and ask the kernel for a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    # Accept optional override list: python _find_port.py 3000 4000 5000
    overrides = [int(a) for a in sys.argv[1:] if a.isdigit()]
    port = find_free_port(overrides or CANDIDATES)
    print(port)
