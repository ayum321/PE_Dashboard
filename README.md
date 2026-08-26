# asre-plan-pe-dashboard
## Repository structure
The project has one source tree with explicit deployable boundaries:

```text
backend/PE_Dashboard_API/   FastAPI API source, configuration, and Dockerfile
frontend/PE_Dashboard_MFE/  React/Luminate MFE source, Nginx image, and Dockerfile
backend/legacy-ui/          local-only FastAPI comparison UI
docker-compose.yml          optional two-container local smoke deployment
```

There are no duplicate application source trees and no all-in-one Docker image: Docker builds the API and React MFE from their owners above.

## Local dashboards
- React Portal dashboard: [`frontend/start.bat`](frontend/start.bat)
- Original FastAPI dashboard, local comparison only: [`backend\legacy-ui/start.bat`](backend\legacy-ui/start.bat)

See [`other/RUN-LOCAL-DASHBOARDS.md`](other/RUN-LOCAL-DASHBOARDS.md) for source ownership and deployment boundaries.
