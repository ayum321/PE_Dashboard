# FastAPI Dashboard (local comparison)

Run [`start.bat`](start.bat) to start the original FastAPI browser dashboard locally.

- Browser UI: `http://127.0.0.1:8765/legacy`
- Purpose: parity comparison and local review only
- Not deployed to the Blue Yonder Portal

Its browser assets are in `legacy-ui/`. It uses the same `../app/` processing API as the React dashboard so uploads, SLA formulas, findings, and exports cannot drift between the two local UIs.

You may run this together with `../react-dashboard/start.bat`. It detects and reuses the shared local API rather than trying to bind a second process to port `8765`.
