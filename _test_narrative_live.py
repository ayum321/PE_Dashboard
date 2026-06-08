"""Test pe-narrative endpoint live."""
import urllib.request, json, sys

req = urllib.request.Request(
    "http://127.0.0.1:8765/api/pe-narrative",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    print("verdict:", d.get("verdict"))
    print("model:", d.get("model"))
    for s in d.get("sections", []):
        prose = s.get("prose", "")
        rows  = (s.get("table") or {}).get("rows") or []
        print(f"\n  [{s['id']}]")
        print(f"   prose: {prose[:150]}")
        print(f"   rows:  {rows[:2]}")
except Exception as e:
    print("ERROR:", e)
    sys.exit(1)
