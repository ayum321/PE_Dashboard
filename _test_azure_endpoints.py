"""Quick smoke test for all Azure API endpoints against a running server."""
import requests
import sys

base = "http://127.0.0.1:8000/api"
passed = 0
failed = 0

def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}  {detail}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")

# 1. whoami
r = requests.get(f"{base}/azure/whoami")
check("GET /azure/whoami", r.status_code == 200, f"status={r.status_code}")

# 2. status
r = requests.get(f"{base}/azure/status")
d = r.json()
check("GET /azure/status", r.status_code == 200, f"configured={d.get('configured')}")

# 3. auth-status
r = requests.get(f"{base}/azure/auth-status")
d = r.json()
check("GET /azure/auth-status", r.status_code == 200, f"method={d.get('method')}")

# 4. subscriptions
r = requests.get(f"{base}/azure/subscriptions")
d = r.json()
check("GET /azure/subscriptions", r.status_code == 200, f"count={len(d.get('subscriptions', []))}")

# 5. resource-groups
r = requests.get(f"{base}/azure/resource-groups")
check("GET /azure/resource-groups", r.status_code == 200)

# 6. browser-logout
r = requests.post(f"{base}/azure/browser-logout")
d = r.json()
check("POST /azure/browser-logout", r.status_code == 200 and d.get("ok"), f"ok={d.get('ok')}")

# 7. validate (expects subscription_id)
r = requests.post(f"{base}/azure/validate", json={"subscription_id": "test"})
check("POST /azure/validate", r.status_code == 200)

# 8. fetch-resources (will fail with 400/404 but should not 500)
r = requests.post(f"{base}/azure/fetch-resources", json={"hours_back": 1})
check("POST /azure/fetch-resources", r.status_code in (200, 400, 404, 502), f"status={r.status_code}")

# 9. Homepage loads
r = requests.get("http://127.0.0.1:8000/")
check("GET / (homepage)", r.status_code == 200 and "Sign in with Browser" in r.text, f"status={r.status_code}")

# 10. JS has browser login function
r = requests.get("http://127.0.0.1:8000/static/app.js")
check("app.js azureBrowserLogin", "azureBrowserLogin" in r.text)
check("app.js azureBrowserLogout", "azureBrowserLogout" in r.text)

print(f"\n{'='*50}")
print(f"  {passed} passed, {failed} failed")
print(f"{'='*50}")
sys.exit(1 if failed else 0)
