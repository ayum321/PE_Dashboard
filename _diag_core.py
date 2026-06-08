"""Test azure.core.pipeline.policies imports for hang."""
import subprocess, sys, textwrap

TESTS = [
    ("azure.core.configuration", "from azure.core.configuration import Configuration"),
    ("azure.core.pipeline.Pipeline", "from azure.core.pipeline import Pipeline"),
    ("azure.core.pipeline.policies (all)", "from azure.core.pipeline.policies import ContentDecodePolicy, CustomHookPolicy, DistributedTracingPolicy, HeadersPolicy, NetworkTraceLoggingPolicy, ProxyPolicy, RetryPolicy, UserAgentPolicy, HttpLoggingPolicy"),
    ("azure.core.pipeline.policies.ContentDecodePolicy", "from azure.core.pipeline.policies import ContentDecodePolicy"),
    ("azure.core.pipeline.policies.ProxyPolicy", "from azure.core.pipeline.policies import ProxyPolicy"),
    ("azure.core.pipeline.policies.RetryPolicy", "from azure.core.pipeline.policies import RetryPolicy"),
    ("azure.core.pipeline.policies.UserAgentPolicy", "from azure.core.pipeline.policies import UserAgentPolicy"),
    ("azure.core.pipeline.transport.RequestsTransport", "from azure.core.pipeline.transport import RequestsTransport"),
    ("requests", "import requests"),
    ("urllib.request.getproxies", "from urllib.request import getproxies; getproxies()"),
    ("requests.utils.get_environ_proxies", "import requests; p = requests.utils.get_environ_proxies('https://login.microsoftonline.com')"),
]

PYTHON  = sys.executable
TIMEOUT = 8

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
        result = out if out else f"ERR(empty): {proc.stderr.strip()[:60]}"
    except subprocess.TimeoutExpired:
        result = "HANG"
    print(f"{result}\t{label}", flush=True)
