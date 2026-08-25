---
name: mfe-api-contract
description: Use when changing React API calls, runtime env.js, upload progress, portal routing, local launchers, CORS, cookies, or Stratosphere MFE deployment.
---

# React MFE API Contract

1. Read `CLAUDE.md` and `pe-dashboard-mfe/src/api/dashboardApi.ts` before editing.
2. Keep all API calls in `dashboardApi.ts`; use `credentials: 'include'` and
   `xhr.withCredentials = true` for uploads.
3. `env.js` only exposes `LOCAL_APP_NAME`, `FRAME_URL_PATH`, and public
   `API_BASE_URL`. It never contains secrets.
4. Treat `localhost` and `127.0.0.1` as different cookie sites. Local mode may
   derive the backend host from the browser; production uses the ingress URL.
5. Run `npm run check:api-contract`, typecheck, targeted tests, and build. For
   a contract guard, prove a temporary direct fetch or removed credential fails,
   then restore it.
