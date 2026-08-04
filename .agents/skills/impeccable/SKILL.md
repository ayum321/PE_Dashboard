---
name: impeccable
description: Use only when the user explicitly asks for Impeccable or requests a deliberate frontend redesign, visual-direction decision, UX critique/audit, polish pass, responsive overhaul, or ambitious visual refinement. Do not use for routine dashboard bug fixes, small layout edits, or backend work.
---

# Impeccable bridge for the PE Dashboard

1. Treat this as a deliberate design workflow, not the default frontend workflow. For ordinary UI implementation or a narrow defect, use `dashboard-builder` alone.
2. Read `AGENTS.md`, then load `.github/skills/impeccable/SKILL.md`. Run its `context.mjs` once only when its workflow calls for it. Load exactly one matching reference playbook—or `reference/routing.md` when the user has not named a mode—rather than loading the whole reference library.
3. This dashboard is normally **Operate** mode: prioritize scanability, accuracy, information hierarchy, accessibility, responsive behavior, and fast use during a PE audit. Preserve real customer data, threshold semantics, and existing product truth.
4. Distinguish refinement from redesign. Preserve the incumbent identity and behavior for refinement. For a true redesign, establish the replacement visual direction before code; do not quietly blend an abandoned design with the new one.
5. Before editing implementation, also follow `dashboard-builder` and the frontend rules in `CLAUDE.md`. Never bypass `window.appData` data flow, live configuration thresholds, or JavaScript validation.
6. Use bounded verification: inspect desktop and mobile in one pass, correct all material findings in one batch, confirm once, then stop. Do not run open-ended polishing loops.
7. Report the chosen mode, what was visually verified, and any unverified behavior. Run `npm run check:js` after frontend changes.
