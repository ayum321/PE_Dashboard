"""
Pinpoint which exact import line in azure/identity/__init__.py hangs.
Tests each top-level import as a subprocess with a 5s timeout.
"""
import subprocess, sys, textwrap

TESTS = [
    ("_auth_record",        "from azure.identity._auth_record import AuthenticationRecord"),
    ("_exceptions",         "from azure.identity._exceptions import AuthenticationRequiredError, CredentialUnavailableError"),
    ("_constants",          "from azure.identity._constants import AzureAuthorityHosts, KnownAuthorities"),
    ("_persistent_cache",   "from azure.identity._persistent_cache import TokenCachePersistenceOptions"),
    ("_bearer_token",       "from azure.identity._bearer_token_provider import get_bearer_token_provider"),
    ("_credentials_init",   "from azure.identity._credentials import InteractiveBrowserCredential"),
    ("_internal_init",      "from azure.identity._internal import InteractiveCredential"),
    ("_internal.utils",     "from azure.identity._internal.utils import encode_base64"),
    ("_internal.aad_client","from azure.identity._internal.aad_client import AadClient"),
    ("_internal.pipeline",  "from azure.identity._internal.pipeline import build_pipeline"),
    ("_internal.interactive","from azure.identity._internal.interactive import InteractiveCredential"),
    ("_internal.msal_creds","from azure.identity._internal.msal_credentials import MsalCredential"),
    ("_internal.msal_client","from azure.identity._internal.msal_client import MsalClient"),
]

PYTHON  = sys.executable
TIMEOUT = 6

for label, stmt in TESTS:
    code = textwrap.dedent(f"""
        try:
            {stmt}
            print("OK")
        except Exception as e:
            print("ERR: " + str(e)[:80])
    """)
    try:
        proc = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=TIMEOUT)
        out = proc.stdout.strip()
        result = out if out else f"ERR(no-out): stderr={proc.stderr.strip()[:50]}"
    except subprocess.TimeoutExpired:
        result = "HANG"
    print(f"{result}\t{label}", flush=True)
