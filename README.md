# asre-plan-pe-dashboard
## Repository structure
The project has one source tree with explicit deployable boundaries:

```text
backend/PE_Dashboard_API/   API image definition; source is app/
frontend/PE_Dashboard_MFE/  standalone MFE image definition; source is react-dashboard/
devops/                     split-service compose and deployment handoff
app/                        FastAPI processing engine
react-dashboard/            React/Luminate MFE source and portal build
fastapi-dashboard/          retired legacy UI for local comparison only
```

See [`devops/README.md`](devops/README.md) for the DevOps deployment topology. The root [`Dockerfile`](Dockerfile) remains the all-in-one image option.

## Local dashboards
- React Portal dashboard: [`react-dashboard/start.bat`](react-dashboard/start.bat)
- Original FastAPI dashboard, local comparison only: [`fastapi-dashboard/start.bat`](fastapi-dashboard/start.bat)

See [`RUN-LOCAL-DASHBOARDS.md`](RUN-LOCAL-DASHBOARDS.md) for source ownership and deployment boundaries.
