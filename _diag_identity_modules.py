"""
Test each azure.identity credential sub-module for import hangs.
Each module is tested in a SEPARATE subprocess to avoid import-lock
cross-contamination between tests.

Run: .venv\Scripts\python.exe _diag_identity_modules.py
"""
import subprocess
import sys
import os
import textwrap

MODS = [
    "authorization_code",
    "azure_powershell",
    "browser",
    "certificate",
    "chained",
    "client_assertion",
    "client_secret",
    "default",
    "device_code",
    "environment",
    "managed_identity",
    "on_behalf_of",
    "shared_cache",
    "azd_cli",
    "azure_cli",
    "user_password",
    "vscode",
    "workload_identity",
    "azure_pipelines",
]

TIMEOUT = 6  # seconds per module
PYTHON = sys.executable

for mod in MODS:
    full = f"azure.identity._credentials.{mod}"
    code = textwrap.dedent(f"""
        try:
            __import__({full!r})
            print("OK")
        except Exception as e:
            print("ERR: " + str(e)[:60])
    """)
    try:
        proc = subprocess.run(
            [PYTHON, "-c", code],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        out = proc.stdout.strip()
        result = out if out else f"ERR(empty): {proc.stderr.strip()[:40]}"
    except subprocess.TimeoutExpired:
        result = "HANG"
    print(f"{result}\t{mod}", flush=True)

