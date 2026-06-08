import time
from services import azure_monitor as az

t = time.perf_counter()
ok = az._restore_browser_credential()
print(f"restore_credential : {time.perf_counter()-t:6.1f}s  ok={ok}")

cred = az._build_credential({})

t = time.perf_counter()
cred.get_token("https://management.azure.com/.default")
print(f"get_token (1st)    : {time.perf_counter()-t:6.1f}s")

t = time.perf_counter()
cred.get_token("https://management.azure.com/.default")
print(f"get_token (2nd)    : {time.perf_counter()-t:6.1f}s")

# Resource Graph search (cross-sub)
t = time.perf_counter()
vms = az.search_vms(cred, "vm")
print(f"search_vms('vm')   : {time.perf_counter()-t:6.1f}s  found={len(vms)}")

t = time.perf_counter()
vms2 = az.search_vms(cred, "vm")
print(f"search_vms (2nd)   : {time.perf_counter()-t:6.1f}s  found={len(vms2)}")
