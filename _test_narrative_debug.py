"""Debug: print what pe_narrative._digest sees in the live server process."""
import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:8765/api/pe-narrative-debug",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    print(json.dumps(d, indent=2, default=str))
except Exception as e:
    print("ERROR (endpoint may not exist):", e)
