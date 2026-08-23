# Docker and Stratosphere handoff

## Artifacts

- **React MFE**: `pe-dashboard-mfe` builds to static files. Stratosphere must generate/replace its published `env.js` with the HTTPS FastAPI `API_BASE_URL`; no API URL is hard-coded in the React source. A blank API URL is valid only for the all-in-one image, where the MFE and API share one origin.
- **API**: the root `Dockerfile` builds the React bundle and packages it with FastAPI. The same image can serve the MFE directly, or Stratosphere can serve the MFE while the API runs independently.

The sample MFE workflow under `pe-dashboard-mfe/.github/workflows/` is a template, not an active root GitHub Actions workflow. DevOps owns the final workflow, registry, Stratosphere variables, and deployment approval.

## Build and health check

```sh
docker build -t pe-dashboard:VERSION .
docker run --rm -p 8765:8765 pe-dashboard:VERSION
```

The image health check calls `GET /api/health`. The image runs as an unprivileged `peapp` user and stores writable state below `PE_STATE_DIR` (`/data` by default).

Before publishing an image, run:

```sh
python app/_test_config_deployment_safety.py
python app/_test_mfe_spa_fallback.py
python app/_test_report_archive.py
python app/_check_pe_config_refs.py
docker build -t pe-dashboard:VERSION .
```

## Deployment-supplied configuration

Inject all values through the company secret manager or workload identity. Never commit values to Git, Docker build arguments, MFE `env.js`, or Compose files.

| Name | Purpose |
| --- | --- |
| `ALLOWED_ORIGINS` | Exact HTTPS Stratosphere/portal origin(s), comma-separated. |
| `PE_STATE_DIR` | Persistent writable volume path, normally `/data`. |
| `PE_COOKIE_SECURE=true` | Required behind HTTPS ingress. |
| `GOOGLE_API_KEY` or `GEMINI_API_KEY` | Optional AI provider secret. |
| `NVIDIA_API_KEY` or `NIM_API_KEY` | Optional AI provider secret. |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | Only when the approved Azure workload-identity/service-principal flow needs them. |
| `AI_ENABLED` | Enables AI routes only when explicitly set to `true`. |

`GET /api/config` never returns provider keys. UI attempts to save provider keys are rejected unless the explicit local-only override `PE_ALLOW_UI_SECRET_CONFIG=true` is supplied.

## Ingress and operating constraints

- Terminate TLS and enforce company SSO/authorization at the portal or API ingress; CORS alone is not authorization.
- Keep the MFE and API on the same HTTPS site when using the current `pe_sid` cookie. A truly cross-site MFE/API split needs an approved cookie/session redesign; `SameSite=lax` is intentionally not a cross-site XHR credential policy.
- Apply an ingress request-body limit of **50 MB or lower**, upload rate limits, and parser CPU/memory limits.
- Do not publish raw container port 8765 to the internet. The Compose example binds it to loopback for local use only.
- The current audit cache/configuration is shared within one application process. Deploy one isolated engagement per service instance and keep a single worker/replica until application-level user/engagement isolation is introduced. SQLite/WAL persistence is not a multi-replica shared store. Verify that the mounted state volume is writable by the image's `peapp` user before rollout.
- Azure interactive browser login is a local-development feature. Hosted Azure access must use the company-approved workload identity or OAuth design.
