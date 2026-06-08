"""Direct in-process test of pe_narrative._digest and _deterministic_fallback."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Load the cache data first
from services import session_cache
print("ac_snapshot batch_kpis:", bool(session_cache.ac_snapshot().get("batch_kpis")))
print("last_batch kpis:", bool((session_cache.get("last_batch") or {}).get("kpis")))
print("last_resource servers:", len((session_cache.get("last_resource") or {}).get("servers") or []))

# Simulate what _pe_narrative_inner does
payload = {}

sc_batch    = session_cache.get("last_batch")      or {}
sc_resource = session_cache.get("last_resource")   or {}
sc_sla      = session_cache.get("last_sla_matrix") or {}

if sc_batch and (sc_batch.get("kpis") or sc_batch.get("top_jobs")):
    payload["batch"] = sc_batch
    print("\nHydrated payload['batch'] kpis:", list(sc_batch.get("kpis",{}).keys())[:5])

if sc_resource:
    payload["resource"] = sc_resource
    print("Hydrated payload['resource'] servers:", len(sc_resource.get("servers", [])))

if sc_sla and not payload.get("sla_matrix"):
    payload["sla_matrix"] = sc_sla
    print("Hydrated payload['sla_matrix'] keys:", list(sc_sla.keys())[:5])

# Now call _digest
from routers.pe_narrative import _digest, _deterministic_fallback

digest = _digest(payload)
print("\n=== digest keys present ===")
for k in ("batch", "sla_matrix", "resource", "sow_compare", "smart_findings"):
    v = digest.get(k)
    if v:
        if isinstance(v, dict):
            sub = v.get("kpis") or v.get("servers") or {}
            print(f"  {k}: present, sub-keys={list(sub.keys() if isinstance(sub,dict) else [])[:4]}")
        else:
            print(f"  {k}: {type(v).__name__}")
    else:
        print(f"  {k}: EMPTY")

print("\n=== _deterministic_fallback ===")
result = _deterministic_fallback(digest, digest.get("customer_name", "Test"))
print("verdict:", result.get("verdict"))
print("summary:", result.get("summary", "")[:200])
for s in result.get("sections", []):
    prose = s.get("prose", "")
    rows  = (s.get("table") or {}).get("rows") or []
    print(f"\n  [{s['id']}]")
    print(f"   prose: {prose[:120]}")
    print(f"   rows[0]: {rows[0] if rows else '[]'}")
