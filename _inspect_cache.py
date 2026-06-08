"""Quick diagnostic — print what's in session_cache right now."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from services import session_cache

plain_keys = [
    "last_batch", "last_resource", "last_sla_matrix",
    "last_red_flags", "last_smart_findings", "last_findings", "last_sow_compare",
]

print("=== ac_snapshot keys ===")
ac = session_cache.ac_snapshot()
for k, v in ac.items():
    if v:
        ln = len(v) if hasattr(v, "__len__") else "-"
        print(f"  {k}: {type(v).__name__}  len={ln}")

print("\n=== plain cache keys ===")
for k in plain_keys:
    v = session_cache.get(k)
    if v:
        keys = list(v.keys())[:6] if isinstance(v, dict) else "..."
        print(f"  {k}: PRESENT  keys={keys}")
    else:
        print(f"  {k}: EMPTY")

# Print batch kpis if present
print("\n=== batch kpis detail ===")
bk = ac.get("batch_kpis") or {}
lb = session_cache.get("last_batch") or {}
sf = session_cache.get("last_smart_findings") or {}
print("  ac.batch_kpis:", bk)
print("  last_batch.kpis:", lb.get("kpis", {}))
print("  last_smart_findings keys:", list(sf.keys())[:8] if sf else "EMPTY")
