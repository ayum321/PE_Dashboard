# Local dashboard launchers

There are two browser dashboards. They use the same FastAPI processing API, but are launched separately.

| What you need | Run | Browser address | Deployment use |
| --- | --- | --- | --- |
| React Portal MFE | `frontend\\start.bat` | `http://127.0.0.1:3000` | Blue Yonder Portal / Stratosphere MFE |
| Original FastAPI dashboard | `backend\\legacy-ui\\start.bat` | `http://127.0.0.1:8765/legacy` | Local legacy comparison only |

You may run both at once. They intentionally reuse one local API process on port `8765` and therefore show the same local audit session data.

## Source ownership

- `frontend/PE_Dashboard_MFE/source/` — React Portal MFE source, build, tests, and Portal runtime configuration. `frontend/start.bat` starts it locally.
- `backend/PE_Dashboard_API/app/` — shared FastAPI processing API: uploads, SLA calculations, Azure fetch, findings, exports, and session state. `backend/start-api.bat` starts it locally.
- `backend/legacy-ui/` — original FastAPI browser assets and its local comparison launcher.

The production images are explicit: `backend/PE_Dashboard_API/Dockerfile` packages only the API (`PE_UI_MODE=api`), while `frontend/PE_Dashboard_MFE/Dockerfile` packages the React MFE. The Portal pipeline can publish the MFE independently when required.
