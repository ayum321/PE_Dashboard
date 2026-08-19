# PE Audit Dashboard — Codex Guide

## Load context economically

- Treat `CLAUDE.md` as the detailed operating manual. Before editing, read only the sections relevant to the area you will change: **The Gate**, **Conventions**, the matching **Hard-Won Gotcha**, and the named data-pillar architecture.
- Keep this file concise. Add a rule here only after it is a repeatable project-level requirement.
- Do not load more than one project skill unless the task genuinely crosses those domains.

## Automatic routing

| Task | Automatically use |
|---|---|
| Batch/SLA/resource/SOW/benchmark metrics, findings, provenance, or PE narrative | `pe-analysis` skill; use `pe-analyst` for read-only tracing |
| `static/app.js`, `static/deep_dive.js`, `templates/`, charts, panels, or upload UI | `dashboard-builder` skill; use `frontend-builder` for implementation |
| Explicit “Impeccable”, visual redesign/direction, UX critique/audit, or deliberate polish | `impeccable` skill; pair with `dashboard-builder` only when implementation begins |
| Azure Monitor, VM metrics, resource deep dive, or `azure_monitor.py` | `azure-deep-dive` skill |
| `routers/`, `services/`, `main.py`, parsers, config, or endpoints | use `backend-builder` for implementation |
| Non-trivial completed change | use `verifier` after the builder; use `code-reviewer` or `security-auditor` only when their focused review is relevant |

Delegate automatically only for independent, bounded work. Prefer one focused agent over a broad fan-out: investigators and reviewers are read-only, return file references and a concise conclusion, and never make the fix. Keep coupled decisions and edits in the main session. Never parallelize writers that may touch `services/pe_config.py`, `services/session_cache.py`, `_norm()`/`_normWf()`, or the same `static/app.js` scope; read `.claude/workflows/parallel-work.md` before any parallel edit.

## Required verification

- Any `static/` change: `npm run check:js`.
- Any `routers/`, `services/`, or `main.py` change: `npm run check:pe-config`.
- Run the directly relevant `_test_*.py` script; these are direct Python runners, not a pytest suite.
- Build CSS only after changing its source: `npm run build:css`.
- Green commands prove syntax and known scenarios only. For behavior changes, run the app and exercise the changed endpoint or screen with representative upload data. Report any skipped gate or manual check plainly.

## Non-negotiable project rules

- Never hardcode thresholds: define them in `services/pe_config.py`, then reference `_pc.NAME` or live `window.appData.config`.
- Preserve provenance (`sla_source`, `reason_code`, `debug_*`) for derived metrics.
- Use explicit NaN and zero-denominator guards; never use `float(NaN) or 0`.
- Keep Python `_norm()` and frontend `_normWf()` in lockstep. Keep the canonical buffer formula consistent: `(SLA_h - runtime_h) / SLA_h * 100`.
- Do not undo the documented Azure corporate-machine auth safeguards.
- Do not use mock customer values in production dashboard output.

## Completion and handoff

- Batch verification by changed file group as described in `.claude/workflows/verification.md`; rerun the fast gates yourself after any agent report.
- For substantial work, create a dated entry under `docs/handoffs/` using its README template. Record terminal output measured in this session, shipped files, remaining manual checks, and only genuinely new lessons.
