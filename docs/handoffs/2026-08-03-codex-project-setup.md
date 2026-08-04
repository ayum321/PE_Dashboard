# Codex project setup — 2026-08-03

## Gates run (measured)

- `py -3.14 C:\Users\1039081\.codex\skills\.system\skill-creator\scripts\quick_validate.py .agents\skills\{pe-analysis,dashboard-builder,azure-deep-dive}` → all three reported `Skill is valid!`.
- Python `tomllib` parse of `.codex/config.toml` and six `.codex/agents/*.toml` files → all parsed; each agent has `name`, `description`, and `developer_instructions`.
- `npm run verify:fast` → `static/app.js` (23,218 lines) and `static/deep_dive.js` (265 lines) passed; config checker reported `98 known names, 57 files scanned`.
- Isolated mutation of the real checker, `services/pe_config.py`, and `routers/redflags.py` → removing `DB_MEM_BAND_HIGH` produced two expected undefined-reference failures at `routers/redflags.py:336` and `:343`. The working tree was not mutated.
- Fresh `codex exec --ephemeral --sandbox read-only` routing check → selected `dashboard-builder`, `frontend-builder`, `npm run check:js`, and the bounded-delegation rule.

## What shipped

- `AGENTS.md` — compact Codex routing, gates, PE rules, and low-overhead delegation policy.
- `.codex/config.toml` — project default reasoning effort `medium`; three-agent concurrency limit.
- `.codex/agents/*.toml` — builders, verifier, PE analyst, reviewer, and security auditor.
- `.agents/skills/` — compact, auto-discovered PE analysis, dashboard, and Azure skills.
- `package.json` — `check:js`, `check:pe-config`, and `verify:fast` wrappers for existing deterministic checks.
- `docs/handoffs/README.md` — shared handoff template.
- `.claude/workflows/session-handoff.md` — points Claude at the shared tracked handoff folder.

## Proven vs. assumed

- Proven: Codex auto-routing and the two fast verification checks work in the current state.
- Proven: the config-reference check detects the documented missing-constant defect class.
- Not exercised: a real dashboard upload/browser smoke test; no application behavior changed in this setup work.
- Not re-run after lowering the project reasoning default: a second Codex model smoke test, to avoid spending another full model invocation. The TOML configuration was parsed successfully.

## Remaining work

- Start a new Codex app/CLI session after this setup so it loads the new project reasoning default.
- Add the new project files to version control when ready; existing user worktree changes were preserved.

## Lessons

- For this project, automatic routing should select one relevant skill or agent by default. Fan-out is reserved for independent read-only investigations or post-change verification.
