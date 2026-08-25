# DevOps deployment assets

This folder contains deployment-owned composition for the two-service topology:

- `pe-dashboard-api` — FastAPI processing API on port `8765`.
- `pe-dashboard-mfe` — React static SPA behind Nginx on port `8080`.

The source of truth stays separated and is not duplicated:

```text
backend/PE_Dashboard_API/Dockerfile  -> app/ + configuration/
frontend/PE_Dashboard_MFE/Dockerfile -> react-dashboard/
devops/docker-compose.split.yml     -> local split-service smoke deployment
```

Build both images from the repository root:

```powershell
docker compose -f devops/docker-compose.split.yml build
docker compose -f devops/docker-compose.split.yml up -d
```

For production, DevOps must provide HTTPS ingress/SSO, persistent `/data` for the API, `ALLOWED_ORIGINS`, runtime `env.js` for the MFE, and secret-manager/workload-identity injection. Do not commit secrets or customer data. The existing root `Dockerfile` remains the all-in-one deployment option.
