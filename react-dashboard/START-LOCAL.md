# React Dashboard

Run [`start.bat`](start.bat) to start the React Portal MFE locally.

- Browser UI: `http://localhost:3000`
- Shared local processing API: `http://localhost:8765`
- Deployment target: Blue Yonder Portal / Stratosphere

This folder is the React source and Portal build target. The shared API remains in `../app/`; it is deliberately not copied into this folder, so all calculations and upload processing have one source of truth.

You may run this together with `../fastapi-dashboard/start.bat`. Both views deliberately reuse one local API process and one audit session.
