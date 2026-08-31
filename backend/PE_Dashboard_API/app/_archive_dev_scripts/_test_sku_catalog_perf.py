"""Verifies the SKU-catalog caching fix: N VM sizes in one region must cost
exactly ONE resource_skus.list() call (not one per distinct size), and that
concurrent worker threads resolving different sizes in a cold region do not
each issue their own duplicate catalog call.

Run: python _test_sku_catalog_perf.py   (from backend/PE_Dashboard_API/app)
"""
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, ".")

from services import azure_monitor as am

# Reset module-level caches so this test is order-independent.
am._sku_catalog_cache.clear()
am._vmsize_catalog_cache.clear()
am._catalog_locks.clear()
am._vm_sku_profile.__dict__.pop("_cache", None)

CALL_LOG = []
CALL_LOCK = threading.Lock()


def _fake_cap(name, value):
    return SimpleNamespace(name=name, value=value)


class _FakeSku:
    def __init__(self, name, resource_type, memory_gb, vcpus):
        self.name = name
        self.resource_type = resource_type
        self.capabilities = [_fake_cap("MemoryGB", str(memory_gb)), _fake_cap("vCPUs", str(vcpus))]


class _FakeResourceSkusOps:
    def list(self, filter):  # noqa: A002 - matches Azure SDK signature
        with CALL_LOCK:
            CALL_LOG.append(("resource_skus.list", filter))
        # Simulate real network latency so concurrent callers would overlap
        # if the lock did not serialise the first fetch.
        time.sleep(0.05)
        return [
            _FakeSku("Standard_D4s_v5", "virtualMachines", 16, 4),
            _FakeSku("Standard_D8s_v5", "virtualMachines", 32, 8),
            _FakeSku("Standard_E8s_v5", "virtualMachines", 64, 8),
        ]


class _FakeComputeClient:
    def __init__(self, credential, subscription_id):
        self.resource_skus = _FakeResourceSkusOps()


def _worker(sub_id, vm_size, location, results, idx):
    results[idx] = am._vm_sku_profile(credential=None, subscription_id=sub_id,
                                       vm_size=vm_size, location=location)


with patch("azure.mgmt.compute.ComputeManagementClient", _FakeComputeClient):
    sub_id = "11111111-1111-1111-1111-111111111111"
    location = "eastus2"
    sizes = ["Standard_D4s_v5", "Standard_D8s_v5", "Standard_E8s_v5",
             "Standard_D4s_v5", "Standard_D8s_v5"]  # 3 distinct, 2 repeats
    threads, results = [], [None] * len(sizes)
    for i, size in enumerate(sizes):
        t = threading.Thread(target=_worker, args=(sub_id, size, location, results, i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

resource_sku_calls = [c for c in CALL_LOG if c[0] == "resource_skus.list"]
print("resource_skus.list call count:", len(resource_sku_calls))
print("results:", results)

assert len(resource_sku_calls) == 1, (
    f"expected exactly ONE resource_skus.list call for one region, got {len(resource_sku_calls)}: {resource_sku_calls}"
)
assert results[0]["vcpus"] == 4 and results[0]["memory_bytes"] == 16 * am._BYTES_PER_GB
assert results[3]["vcpus"] == 4, "repeat lookup for the same size must hit the cache, not re-fetch"
assert results[2]["vcpus"] == 8

# A second, brand-new fetch for the SAME region must not call Azure again at all.
again = am._vm_sku_profile(credential=None, subscription_id=sub_id, vm_size="Standard_D4s_v5", location=location)
assert again["vcpus"] == 4
assert len([c for c in CALL_LOG if c[0] == "resource_skus.list"]) == 1, "cache must persist across calls"

print("OK — 5 lookups (3 distinct sizes, 2 repeats, across 5 concurrent threads) cost exactly 1 Azure call.")
