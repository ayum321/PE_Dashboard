# PE Audit Dashboard — Operating Manual

## Product boundary

The React/Luminate MFE in `frontend/PE_Dashboard_MFE/source/` is the deployed user interface.
FastAPI in `backend/PE_Dashboard_API/app/` remains the processing and decision engine for uploads, SLA
resolution, Azure data, findings, exports, and the report archive. Do not port
calculation rules into React and do not treat the retired FastAPI-rendered UI as
the deploy target.

## The gate

Run commands from the directory shown. The production MFE gate is a clean
typecheck, targeted tests, and `npm run build`; backend changes additionally
need their direct Python gate and relevant runner. Green commands are necessary,
not sufficient—exercise the changed API or React screen with representative
evidence before calling behaviour verified.

```powershell
# frontend/PE_Dashboard_MFE/source/
npm run check:api-contract
npm run lint
npx tsc --noEmit --pretty false
npm test -- --watchAll=false --runInBand
npm run build

# backend/PE_Dashboard_API/app/
py -3.14 _check_pe_config_refs.py
py -3.14 _test_<relevant_area>.py
```

For deployed-image changes also run the commands in `documentation/DEPLOYMENT.md`,
including `app/_test_config_deployment_safety.py`, `app/_test_mfe_spa_fallback.py`,
and the Docker build. `start-mfe.bat` is local-only; it is not a Stratosphere
deployment mechanism.

## Hard-won gotchas

1. **One SLA truth.** `resolved_workflow_df` and backend payloads own runtime,
   status, buffer, headroom, and provenance. React displays them; it must not
   recompute them. Canonical buffer is `(SLA_h - runtime_h) / SLA_h * 100`.
2. **Name normalisation is coupled.** Python workflow normalisation and React
   presentation/matching logic must change together, with a regression test.
3. **Duration names are not interchangeable.** Daily elapsed span, active busy
   time, effective contiguous window, and a workflow's worst anchored run are
   distinct. Show the basis; never silently substitute one for another.
4. **Keep the Azure session on one browser site.** `localhost` and `127.0.0.1`
   do not share `pe_sid`. All MFE API calls route through `dashboardApi.ts` with
   credentials included. In production use the runtime `env.js` endpoint, never
   a hard-coded host or port.
5. **Browser config is public.** `env.js` can contain only public MFE name/path/
   API URL. Secrets belong in the deployment secret manager or backend workload
   identity, never in source, Docker arguments, or browser runtime config.
6. **Narrative is evidence, not a second calculator.** Do not show a pillar,
   question, or UAT section when its evidence is absent. Do not make a page look
   green while row-level evidence is critical.
7. **Thresholds live in `app/services/pe_config.py`.** Do not hard-code status
   bands in routers or React. Preserve `sla_source`, `reason_code`, and debug
   provenance when adding derived fields.

## Conventions

- React UI: `react-dashboard/src/components/panels/`; API adapter:
  `react-dashboard/src/api/dashboardApi.ts`; visual tokens:
  `react-dashboard/src/theme/`.
- FastAPI routes: `app/routers/`; calculations/parsers: `app/services/`; direct
  regression runners: `app/_test_*.py`.
- MFE files use TypeScript, MUI, and Highcharts. Prefer shared CSS evidence
  states and semantic tokens over scattered inline colour/size literals.
- One writer owns each file group. A read-only investigator may trace call chains
  but never fixes them. Builders own disjoint frontend/backend paths; a verifier
  runs after the builder and does not edit.
- Coupled pairs that must stay in lockstep: backend SLA output ↔ MFE interfaces;
  `pe_config.py` thresholds ↔ API config ↔ MFE legend; export payload ↔ archive
  contract; deployed `env.js` ↔ `dashboardApi.ts` cookie/origin behaviour.

## Portable principles applied here

1. **Green gates are not verification.** Passing typecheck, lint, and tests
   proves known cases only. Real verification reads real returned data and opens
   the actual React screen.
2. **Mutation-test correctness guards.** Temporarily break the protected branch,
   confirm the relevant test fails, then restore it. A test that stays green is
   decoration; prove the mutant actually ran.
3. **Deterministic checks beat vigilance.** Put recurring contract mistakes in a
   script or linter. `npm run check:api-contract` protects MFE runtime API and
   cookie rules on every run.
4. **Delegate investigation, keep decisions.** Use read-only agents for tracing
   and sweep work. Keep coupled decisions and edits in the main change; agents'
   reports are claims until gates are rerun locally.
5. **Verify absence before building.** Inspect active code and API payloads;
   documentation and legacy FastAPI UI are not proof of React behaviour.
6. **Report outcomes faithfully.** State what ran, what failed, and what could
   not be visually or live-environment verified.

## Handoff

For substantial work create `docs/handoffs/YYYY-MM-DD-topic.md` from
`docs/handoffs/README.md`. Record measured command output, shipped files,
unverified manual checks, and only durable lessons.
