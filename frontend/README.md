# PE Dashboard frontend

The React MFE source is in `PE_Dashboard_MFE/source/`. The deployable standalone image is defined by `PE_Dashboard_MFE/Dockerfile` and serves the compiled SPA on port `8080`.

Build from the repository root:

```powershell
docker build -f frontend/PE_Dashboard_MFE/Dockerfile -t pe-dashboard-mfe:VERSION .
```

For the company portal, publish `frontend\PE_Dashboard_MFE\source/build/` using the runtime `env.js` process described in `../frontend\PE_Dashboard_MFE\source/DEPLOYMENT_HANDOFF.md`.
