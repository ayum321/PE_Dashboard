"""
Live benchmark test for the PE Dashboard.
Run: python _bench_test.py [port]
"""
from __future__ import annotations
import json, sys, time, urllib.request, urllib.error
from typing import Optional

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else 8765}"

ENDPOINTS = [
    # (method, path, label)
    ("GET",  "/api/health",                       "Health"),
    ("GET",  "/api/session-data",                 "Session Data"),
    ("GET",  "/api/config",                       "Config GET"),
    ("GET",  "/docs",                             "Swagger UI"),
    # Batch / resource / SLA
    ("GET",  "/api/batch/status",                 "Batch Status"),
    ("GET",  "/api/resource/status",              "Resource Status"),
    ("GET",  "/api/sla-matrix/status",            "SLA Matrix Status"),
    ("GET",  "/api/sla-matrix/workflow-summary",  "Workflow Summary"),
    ("GET",  "/api/sla-intelligence/summary",     "SLA Intelligence"),
    # Analysis
    ("GET",  "/api/findings",                     "Findings"),
    ("GET",  "/api/executive-kpis",               "Executive KPIs"),
    ("GET",  "/api/redflags",                     "Red Flags"),
    ("GET",  "/api/correlation",                  "Correlation"),
    ("GET",  "/api/export/report-data",           "Export Report Data"),
    ("GET",  "/api/sow/status",                   "SOW Status"),
    ("GET",  "/api/benchmark/status",             "Benchmark Status"),
    ("GET",  "/api/final-judgment",               "Final Judgment"),
    # Azure
    ("GET",  "/api/azure/auth-status",            "Azure Auth Status"),
    ("GET",  "/api/azure/status",                 "Azure Config Status"),
    ("GET",  "/api/azure/whoami",                 "Azure Whoami"),
    ("GET",  "/api/azure/subscriptions",          "Azure Subscriptions"),
    ("GET",  "/api/azure/vm-cache-status",        "Azure VM Cache Status"),
    # POST
    ("POST", "/api/config",                       "Config POST (empty body)"),
    ("POST", "/api/clear-session",                "Clear Session"),
]

WIDTH = 42

def probe(method: str, path: str) -> tuple[int, float, Optional[str]]:
    url = BASE + path
    t0 = time.perf_counter()
    try:
        data = b"{}" if method == "POST" else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(512).decode("utf-8", errors="replace")
            return r.status, (time.perf_counter() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, None
    except Exception as exc:
        return 0, (time.perf_counter() - t0) * 1000, str(exc)[:80]

def color(code: int) -> str:
    if code == 0: return "\033[91m"    # red - connection error
    if code < 400: return "\033[92m"   # green - success
    if code < 500: return "\033[93m"   # yellow - client error (404/422)
    return "\033[91m"                  # red - server error

RESET = "\033[0m"
BOLD  = "\033[1m"

print(f"\n{BOLD}====== PE DASHBOARD LIVE BENCHMARK  [{BASE}] ======{RESET}\n")

sections = {
    "CORE":           [e for e in ENDPOINTS if e[2] in ("Health","Session Data","Config GET","Swagger UI","Config POST (empty body)","Clear Session")],
    "BATCH/SLA":      [e for e in ENDPOINTS if "Batch" in e[2] or "Resource" in e[2] or "SLA" in e[2] or "Workflow" in e[2] or "Intelligence" in e[2]],
    "ANALYSIS":       [e for e in ENDPOINTS if e[2] in ("Findings","Executive KPIs","Red Flags","Correlation","Export Report Data","SOW Status","Benchmark Status","Final Judgment")],
    "AZURE":          [e for e in ENDPOINTS if "Azure" in e[2]],}

pass_count = fail_count = total_ms = 0
all_failures: list[tuple[str,int,str]] = []

for section, items in sections.items():
    print(f"{BOLD}[{section}]{RESET}")
    for method, path, label in items:
        code, ms, body = probe(method, path)
        total_ms += ms
        ok = 0 < code < 500
        status_str = f"HTTP {code}" if code else "ERR(no conn)"
        sym = "OK " if ok else "FAIL"
        c = color(code)
        print(f"  {c}{sym}{RESET}  {label:<{WIDTH}} {status_str}  {ms:6.0f}ms")

        if ok:
            pass_count += 1
            # For key endpoints, show useful info
            if label == "Health" and body:
                try:
                    d = json.loads(body)
                    print(f"       service={d.get('service')}  version={d.get('version')}  pid={d.get('pid')}")
                except Exception:
                    pass
            if label == "Azure Cred Status" and body:
                try:
                    d = json.loads(body)
                    print(f"       {d}")
                except Exception:
                    print(f"       {body[:120]}")
            if label == "Azure Subscriptions" and body:
                try:
                    d = json.loads(body)
                    subs = d if isinstance(d, list) else d.get("subscriptions", [])
                    print(f"       {len(subs)} subscription(s) found")
                    for s in subs[:3]:
                        print(f"         - {s.get('display_name','?')} ({s.get('subscription_id','?')})")
                except Exception:
                    print(f"       {body[:120]}")
        else:
            fail_count += 1
            if code >= 500 or code == 0:
                all_failures.append((label, code, body or ""))
    print()

print("=" * 62)
print(f"  Total endpoints : {pass_count + fail_count}")
print(f"  Passed (2xx/4xx): {pass_count}  |  Server errors (5xx/conn): {fail_count}")
print(f"  Total probe time: {total_ms:.0f}ms  |  Avg: {total_ms/(pass_count+fail_count):.0f}ms")
print()

if all_failures:
    print(f"{BOLD}SERVER ERRORS (need fixing):{RESET}")
    for label, code, info in all_failures:
        print(f"  \033[91m[{code}] {label}\033[0m")
        if info:
            # Try to extract detail from FastAPI error body
            try:
                d = json.loads(info)
                print(f"    detail: {d.get('detail', info[:200])}")
            except Exception:
                print(f"    {info[:200]}")
else:
    print("\033[92mNo server errors detected.\033[0m")

print()
