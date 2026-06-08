"""Verify the platform.platform() patch fixes the azure.identity import hang."""
import time
import platform

_orig = platform.platform

def _fast_platform(**_kw):
    try:
        return f"{platform.system()}-{platform.release()}"
    except Exception:
        return "Windows"

platform.platform = _fast_platform

try:
    t0 = time.perf_counter()
    from azure.identity import InteractiveBrowserCredential, AuthenticationRecord
    elapsed = time.perf_counter() - t0
    print(f"OK — imported in {elapsed:.2f}s")
    print(f"IBC = {InteractiveBrowserCredential}")
    print(f"AuthRecord = {AuthenticationRecord}")
except Exception as e:
    print(f"ERR: {e}")
finally:
    platform.platform = _orig
