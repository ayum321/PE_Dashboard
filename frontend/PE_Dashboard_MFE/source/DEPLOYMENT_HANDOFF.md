# PE Audit Dashboard MFE deployment handoff

This folder is the company-portal deployment artifact. Build and publish the React MFE only; do not package the retired FastAPI UI with this artifact.

`start-mfe.bat` is for local React development only. It is never used by the company portal or by the deployment pipeline.

`localhost:3000` is also local-only. The deployed MFE has no fixed port: the company portal serves the static files through its approved HTTPS frame URL.

## Build

Run the following with `react-dashboard` as the working directory:

```sh
npm ci
npm run lint
npx tsc --noEmit --pretty false
npm run build
```

Publish the contents of `react-dashboard/build/` to the approved Stratosphere static-content location.

## Runtime configuration

After the build, generate `build/env.js` from `.github/envs/env.ejs`:

```sh
node .github/scripts/setRuntimeEnv.js ./build/
```

The deployment pipeline supplies these non-secret values:

| Variable | Purpose |
| --- | --- |
| `LOCAL_APP_NAME` | Portal display name. |
| `FRAME_URL_PATH` | Path where the MFE is mounted in the company portal. |
| `API_BASE_URL` | Base URL for the separately hosted audit API. Leave empty only when `/api` is routed at the same origin. |

`API_BASE_URL` is an endpoint, not a credential. Never put passwords, client secrets, Azure credentials, or API keys in `env.js`, repository variables, or the browser bundle.

The generator also accepts the older camel-case aliases (`appName`, `frameUrlPath`, `apiBaseUrl`) for local compatibility; DevOps should use the uppercase names above.

The MFE uses `/api` endpoints for uploads and audit results. The API can be hosted separately, but it must be reachable from the portal and configured for the selected same-origin or cross-origin model.

For deployment, the static MFE is independent of the local `.bat` files: DevOps builds the immutable `build/` assets, generates `build/env.js` with the approved public endpoint values, publishes those assets, and rolls back by republishing the preceding static artifact. No password or service credential is required by—or may be included in—the React bundle.

## Pipeline ownership

The workflow files under `react-dashboard/.github/workflows/` are reference templates. Because this repository's Git root is one level above `react-dashboard`, GitHub will not discover them automatically. The DevOps-owned pipeline must target this folder for its Node version, dependency cache, build, runtime-env generation, and static-content upload.

## Deployment completion boundary

The MFE code is ready to build and accepts runtime portal configuration. The actual Stratosphere publication is completed only when DevOps creates or adapts the repository-level pipeline and supplies the approved `FRAME_URL_PATH` plus the API routing choice. Until those company values exist, no source change can determine the final portal URL or trigger a live release automatically.
