# Azure auth and VM-search reliability - 2026-08-16

## Gates run (measured)

- `py -3.14 _test_azure_auth_search.py` -> 5 passed: one interactive browser launch for concurrent requests, warm VM-inventory search, session-cache isolation, no machine-wide CLI fallback for resource groups, and correct 503 treatment for VPN/DNS preflight failures.
- `py -3.14 _test_azure_endpoints.py` -> 11 passed (run by the implementation agent against the local server before handoff).
- `py -3.14 -m py_compile services\pe_config.py services\azure_monitor.py routers\azure_resource.py` -> passed.
- `py -3.14 _check_pe_config_refs.py` -> 118 known configuration names resolved across 60 files.
- `py -3.14 _validate_js.py` -> `static/app.js` and `static/deep_dive.js` passed.
- Local TestClient smoke -> `/api/azure/auth-status` returned 200; an unauthenticated `/api/azure/search-vms` request returned an immediate 401 and did not launch browser authentication.
- Live loopback smoke -> `uvicorn main:app --host 127.0.0.1 --port 8767` booted; `/api/health` and `/api/azure/auth-status` returned successfully, then that exact temporary process was stopped.
- `git diff --check` -> no whitespace errors; existing CRLF conversion warnings remain.
- `npm run check:js` and `npm run check:pe-config` are not defined by this repository's `package.json`; the direct Python validators above were used instead.

## What shipped

- Interactive Azure browser login is single-flight per dashboard session. Concurrent Settings/modal requests wait for the existing Microsoft account-picker window rather than launching another.
- Azure credentials are browser-session-only and process-memory-only. `DefaultAzureCredential`, persistent SDK token-cache construction, and CLI/ambient-credential fallback are not used by live dashboard data calls or resource-group selection.
- VM search first uses the session-scoped pre-warmed inventory. Cold search defaults to the selected subscription; full-tenant search is explicit and labelled as slower.
- Azure Resource Graph uses configuration-backed connect/read limits (6s/35s defaults) and returns a clear 504 error instead of leaving the UI in `Searching…` indefinitely. Frontend requests also have bounded timeouts and prevent duplicate concurrent searches.
- Microsoft-login DNS/VPN preflight failures return 503 (service unavailable), rather than the misleading 401 authentication status.
- Browser authentication retains the documented forced-IPv4 and platform safeguards for the supported corporate Windows environment.

## Remaining live check

- A real Azure MFA/RBAC sign-in and VM search was intentionally not run in this session because it opens the user's Microsoft account flow. Confirm it once on the target account: click **Sign in with Browser**, complete the one Microsoft window, wait for `VM inventory ready`, then search the selected subscription for a known customer or tag.
