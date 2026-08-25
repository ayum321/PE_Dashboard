# PE Dashboard backend

The FastAPI service source remains in `../app/`. The deployable API Dockerfile is `PE_Dashboard_API/Dockerfile`.

Build from the repository root:

```powershell
docker build -f backend/PE_Dashboard_API/Dockerfile -t pe-dashboard-api:VERSION .
```

Run with `PE_STATE_DIR=/data`, a persistent volume, approved ingress authentication, and secrets injected by the deployment platform.
