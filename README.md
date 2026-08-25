# asre-plan-pe-dashboard
## Repository structure
The project has one source tree with explicit deployable boundaries:

```text
backend/PE_Dashboard_API/   API image definition; source is backend/PE_Dashboard_API/app/
frontend/PE_Dashboard_MFE/  standalone MFE image definition; source is frontend\PE_Dashboard_MFE\source/
devops/                     split-service compose and deployment handoff
backend/PE_Dashboard_API/app/                        FastAPI processing engine
frontend\PE_Dashboard_MFE\source/            React/Luminate MFE source and portal build
backend\legacy-ui/          retired legacy UI for local comparison only
```

See [`devops/README.md`](devops/README.md) for the DevOps deployment topology. The root [`Dockerfile`](Dockerfile) remains the all-in-one image option.

## Local dashboards
- React Portal dashboard: [`frontend\PE_Dashboard_MFE\source/start.bat`](frontend\PE_Dashboard_MFE\source/start.bat)
- Original FastAPI dashboard, local comparison only: [`backend\legacy-ui/start.bat`](backend\legacy-ui/start.bat)

See [`RUN-LOCAL-DASHBOARDS.md`](RUN-LOCAL-DASHBOARDS.md) for source ownership and deployment boundaries.
