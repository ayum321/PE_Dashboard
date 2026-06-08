"""
Test if TokenCachePersistenceOptions(allow_unencrypted_storage=True)
avoids the DPAPI hang on this machine.
Run: .venv\Scripts\python.exe _test_persistence.py
"""
import threading, time, sys
done = threading.Event()
result = []

def go():
    try:
        from azure.identity import TokenCachePersistenceOptions
        opts = TokenCachePersistenceOptions(name="pe_dash_test", allow_unencrypted_storage=True)
        result.append(f"OK — {opts}")
    except Exception as e:
        result.append(f"ERR: {e}")
    finally:
        done.set()

t = threading.Thread(target=go, daemon=True)
t.start()

if done.wait(8):
    print(result[0] if result else "?")
else:
    print("HANG — allow_unencrypted_storage=True still hangs (DPAPI or msal_extensions issue)")
    sys.exit(1)
