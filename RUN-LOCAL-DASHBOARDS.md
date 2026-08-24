# Local dashboard launchers

There are two browser dashboards. They use the same FastAPI processing API, but are launched separately.

| What you need | Run | Browser address | Deployment use |
| --- | --- | --- | --- |
| React Portal MFE | `react-dashboard\\start.bat` | `http://localhost:3000` | Blue Yonder Portal / Stratosphere MFE |
| Original FastAPI dashboard | `fastapi-dashboard\\start.bat` | `http://localhost:8765/legacy` | Local legacy comparison only |

You may run both at once. They intentionally reuse one local API process on port `8765` and therefore show the same local audit session data.

## Source ownership

- `react-dashboard/` — React Portal MFE source, build, tests, Portal runtime configuration, and local launcher.
- `fastapi-dashboard/` — original FastAPI browser assets in `legacy-ui/` and its local comparison launcher.
- `app/` — shared FastAPI processing API: uploads, SLA calculations, Azure fetch, findings, exports, and session state. It does not select a browser UI unless a launcher explicitly chooses a mode.

The production Dockerfile packages only the FastAPI API (`PE_UI_MODE=api`). The Portal pipeline builds and publishes the React MFE independently from `react-dashboard/`.
