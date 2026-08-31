# Workflow: Prepare for Deploy

**Purpose:** Multi-step procedural guide to prepare PE_Dashboard for production deployment to Stratosphere.

**When to use:** Before tagging a release, before merging to main, before handoff to DevOps

**Expected duration:** 20–30 minutes

---

## Step-by-Step Procedure

### Phase 1: Pre-Flight (5 min)

**Goal:** Gather context from the last session.

1. **Read SESSION_HANDOFF.md** (see .claude/SESSION_HANDOFF.md)
   - What shipped last?
   - What failed?
   - What remains?

2. **Check git status**
   ```bash
   git status
   git log --oneline -5
   ```
   - Any uncommitted changes? Stash or commit.
   - Any unpushed commits? Push before proceeding.

3. **Verify you're on the merge-target branch**
   ```bash
   git branch -v | grep "\*"
   ```
   - Should be on `main` or the release branch
   - If not, switch: `git checkout main`

### Phase 2: Run Deploy-Readiness Check (10 min)

**Goal:** Verify all 7 gates pass.

Use the skill: `.claude/skills/deploy-readiness-check.md`

Commands (in order):
```bash
# Gate 1: Lint
cd pe-dashboard-mfe && npm run lint || exit 1

# Gate 2: TypeScript
npx tsc --noEmit || exit 1

# Gate 3: Tests
npm test || exit 1

# Gate 4: Build
npm run build || exit 1

# Gate 5: Backend Config Safety
cd ..
python app/_test_config_deployment_safety.py || exit 1

# Gate 6: Backend Config Refs
python app/_check_pe_config_refs.py || exit 1

# Gate 7: Docker Build
docker build -t pe-dashboard:candidate . || exit 1
```

**If any gate fails:**
- Stop. Do not proceed.
- Identify the failure (see "Fixing Test Failures" in deploy-readiness-check.md)
- Fix it.
- Re-run that gate.
- Once fixed, re-run all gates from Gate 1.

**If all gates pass:** Proceed to Phase 3.

### Phase 3: Test-Expectation Audit (3 min)

**Goal:** Catch stale test assertions before shipping.

Use the skill: `.claude/skills/test-expectation-audit.md`

Run the audit:
```bash
cd pe-dashboard-mfe
npm run audit-tests  # (if you wired it to package.json)
# OR manually: review test files against UI files
```

**If mismatches found:**
- Fix the test or fix the UI (must be coordinated)
- Re-run `npm test`
- Proceed.

**If no mismatches:** Proceed to Phase 4.

### Phase 4: Live App Validation (5 min)

**Goal:** Verify behavior in a running instance (not just code correctness).

Start the app:
```bash
# Terminal 1: Frontend
cd pe-dashboard-mfe && npm start

# Terminal 2: Backend
cd ..
python app/_dev_server.py
```

Wait for both to be ready (DevTools or curl):
```bash
curl http://localhost:8765/api/health
curl http://localhost:3000  # Should render without 404
```

**Spot checks (pick 3–5 key features):**
1. Open the MFE at http://localhost:3000
2. Navigate to a critical panel (e.g., ArchivePanel, SLA Matrix)
3. Check the browser console (DevTools → Console): any errors? If yes, fail this phase.
4. Interact with a key button/form (e.g., click "Open full HTML", upload a file)
5. Verify the data rendered is correct (compare with database if needed)

**Example check for ArchivePanel:**
- Open http://localhost:3000/archive
- Check that archive history renders
- Click "Open full HTML" button
- Verify link opens a report in a new tab
- Check browser console: no red errors

**If issues found:**
- Note them. Ask a builder agent to fix.
- Once fixed, re-run spot checks.
- Proceed only when all checks pass.

**If no issues:** Proceed to Phase 5.

### Phase 5: Final Checklist (2 min)

**Goal:** One last verification before shipment.

```
- [ ] All 7 gates pass (Gate 7 Docker build succeeded)
- [ ] Test-expectation audit found no mismatches
- [ ] Spot checks passed (at least 3 features work correctly)
- [ ] No console errors in browser (DevTools → Console is clean)
- [ ] env.js exists and API_BASE_URL is set correctly
- [ ] All secrets (AZURE_CLIENT_SECRET, etc.) are NOT in logs
- [ ] Git history is clean (no merge conflicts, no dirty state)
- [ ] Commit message is descriptive (not "fix stuff")
- [ ] PR review is approved (if using PR workflow)
```

If any checkbox is unchecked, go back to the corresponding phase and fix it.

### Phase 6: Document Outcomes & Hand Off (2 min)

**Goal:** Create a record for the next session.

Create/update `.claude/SESSION_HANDOFF.md`:

```
## Session Handoff — Prepare for Deploy

**Date:** 2026-08-28  
**Performer:** [Your Name]  
**Status:** READY FOR DEPLOY ✓

### Gate Results
- Gate 1 (Lint): PASS
- Gate 2 (TypeScript): PASS
- Gate 3 (Tests): PASS (35 tests)
- Gate 4 (Build): PASS (395 KB gzipped)
- Gate 5 (Backend Config Safety): PASS
- Gate 6 (Backend Config Refs): PASS
- Gate 7 (Docker): PASS

### What Shipped
- ArchivePanel test expectation fixed (now expects "Open full HTML")
- No UI changes, only test correction

### Spot Checks
- ✓ ArchivePanel renders and "Open full HTML" button works
- ✓ SLA Matrix displays correct compliance %
- ✓ No console errors

### Lessons Learned
- Test expectations must be updated immediately when UI text changes
- Mutation-testing proves assertions are real (not just coincidentally passing)

### Next Steps
- DevOps: Run pre-deploy checklist before merging to main
- Monitor: Watch for env.js regeneration in Stratosphere
- Known constraint: Single-instance deployment (no multi-user isolation yet)

### Git Details
- Branch: ayush/pe-dashboard-migration-pr
- Latest commit: 533b602
- Ready to merge to: main
```

Push the handoff document:
```bash
git add .claude/SESSION_HANDOFF.md
git commit -m "docs: session handoff — deploy-ready, all gates pass"
git push
```

**Done.** App is ready for deployment. Next session starts by reading this handoff.

---

## Abort Criteria

**Stop and escalate if:**
1. A gate fails and you don't know how to fix it (ask a builder agent)
2. Spot checks reveal a behavioral bug (ask a builder agent, don't ship)
3. A secret appears in logs or Docker build (fix immediately, don't ship)
4. Git history shows conflicts or uncommitted changes (resolve before shipping)
5. env.js is missing or API_BASE_URL is wrong (must be fixed before Stratosphere picks it up)

---

## Manual Checkpoints

At each phase end, pause and verify:
- Phase 1: ✓ No uncommitted changes, on correct branch
- Phase 2: ✓ All 7 gates pass
- Phase 3: ✓ Test expectations match UI text
- Phase 4: ✓ App runs, spot checks pass, no console errors
- Phase 5: ✓ All final checklist items are checked
- Phase 6: ✓ SESSION_HANDOFF.md is updated and pushed

Only after all checkpoints pass is the app safe to ship.
