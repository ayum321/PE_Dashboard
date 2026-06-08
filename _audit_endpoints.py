import re, os

endpoints = {}
for root, dirs, files in os.walk("routers"):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if not f.endswith(".py"): continue
        src = open(os.path.join(root, f), encoding="utf-8").read()
        for m in re.finditer(r'@router\.(get|post|put|delete)\(["\'](/[\w/\-{}]+)', src):
            endpoints[m.group(2)] = f

js = open("static/app.js", encoding="utf-8", errors="ignore").read()
js_calls = set(re.findall(r'fetch\(["\'](/api/[\w/\-]+)', js))

print("=== BACKEND ENDPOINTS vs JS CALLS ===")
for ep, f in sorted(endpoints.items()):
    called = any(ep in c or c.split("{")[0] in c for c in js_calls)
    # also check if the path without /api prefix matches
    short = ep.lstrip("/")
    called2 = any(short in c for c in js_calls)
    marker = "OK" if (called or called2) else "** NOT CALLED FROM JS **"
    print(f"  {ep:55s} ({f})  {marker}")

print()
print("=== JS FETCH CALLS ===")
for c in sorted(js_calls):
    print(f"  {c}")

# dead session cache keys
print()
print("=== DEAD SESSION CACHE KEYS (written, never consumed by any reader) ===")
dead = ["batch_kpis", "workflow_rollup", "regression_df", "customer_name",
        "sla_resolved", "adaptive_sla", "last_smart_findings", "volume_vs_sow", "resource_summary"]
for k in dead:
    print(f"  {k}")
