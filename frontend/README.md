# PE Dashboard frontend

The React MFE source remains in `../react-dashboard/`. The deployable standalone image is defined by `PE_Dashboard_MFE/Dockerfile` and serves the compiled SPA on port `8080`.

Build from the repository root:

```powershell
docker build -f frontend/PE_Dashboard_MFE/Dockerfile -t pe-dashboard-mfe:VERSION .
```

For the company portal, publish `react-dashboard/build/` using the runtime `env.js` process described in `../react-dashboard/DEPLOYMENT_HANDOFF.md`.
