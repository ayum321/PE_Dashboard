# Skill: Deploy-Readiness Check

**Purpose:** Run the full 7-gate sequence and report GO/NO-GO for deployment to Stratosphere.

**Triggers:** Before merging a PR, before tagging a release, user asks "is this ready to deploy?"

## Procedure

### Gate 1: Frontend Lint (2 min)
```bash
cd pe-dashboard-mfe
npm run lint
```
**Expected:** Exit 0, no errors reported  
**If fails:** ESLint found style/import violations. Fix and re-run.

### Gate 2: Frontend TypeScript (2 min)
```bash
cd pe-dashboard-mfe
npx tsc --noEmit
```
**Expected:** Exit 0, no type errors  
**If fails:** Type mismatch in TypeScript code. Check the error message, update type annotation.

### Gate 3: Frontend Tests (3 min)
```bash
cd pe-dashboard-mfe
npm test
```
**Expected:** Exit 0, all suites pass  
**If fails:** At least one Jest test is failing. This is a **BLOCKER**. See section "Fixing Test Failures" below.

### Gate 4: Frontend Build (2 min)
```bash
cd pe-dashboard-mfe
npm run build
```
**Expected:** Exit 0, build artifact in build/  
**If fails:** Build bundling failed. Check for circular imports, missing assets, Webpack config.

### Gate 5: Backend Config Safety (1 min)
```bash
python app/_test_config_deployment_safety.py
```
**Expected:** Exit 0, no secrets leaked  
**If fails:** A secret (AZURE_CLIENT_SECRET, etc.) is hardcoded or logged. Fix before deploy.

### Gate 6: Backend Config Refs (1 min)
```bash
python app/_check_pe_config_refs.py
```
**Expected:** Exit 0, all pe_config keys are valid  
**If fails:** Code references a config key that doesn't exist in pe_config.py. Add the key or remove the reference.

### Gate 7: Docker Build (3 min)
```bash
docker build -t pe-dashboard:test .
```
**Expected:** Exit 0, image created  
**If fails:** Dockerfile syntax error or missing dependency. Check the build log.

### Validation Step (Run the app, don't just build it)
```bash
docker run --rm -p 8765:8765 pe-dashboard:test &
sleep 5
curl http://localhost:8765/api/health
kill %1
```
**Expected:** HTTP 200, response includes `"status": "ok"`  
**If fails:** App crashed on startup. Check logs with `docker logs <container_id>`.

---

## Fixing Test Failures

Test failures block deployment. They are not optional.

### Typical Failure: Test Expects Old UI Text

**Example:** ArchivePanel.test.tsx expects "Open exported report" but ArchivePanel.tsx renders "Open full HTML"

**Fix:**
1. Read the test file (pe-dashboard-mfe/src/components/panels/ArchivePanel.test.tsx)
2. Read the UI file (pe-dashboard-mfe/src/components/panels/ArchivePanel.tsx)
3. Compare the expected text (test) with actual text (UI)
4. Update the test to match the UI
5. Re-run `npm test`
6. Verify the test now passes

**Verify it's real:** Mutation-test — break the UI text intentionally, run test, verify it fails. Revert, test passes. This proves the test catches the bug.

### Typical Failure: New Component, No Test

If you added a new component but didn't add tests, write a basic smoke test:

```javascript
// MyNewPanel.test.tsx
import React from 'react';
import { render } from '@testing-library/react';
import MyNewPanel from './MyNewPanel';

describe('MyNewPanel', () => {
  it('renders without crashing', () => {
    const { container } = render(<MyNewPanel />);
    expect(container).toBeDefined();
  });
});
```

Then run `npm test` again.

---

## Report Template

```
## Deploy-Readiness Check

**Date:** YYYY-MM-DD  
**Commit:** <hash>  
**Branch:** <name>

### Gate Results
- Gate 1 (Lint): ✓ PASS
- Gate 2 (TypeScript): ✓ PASS
- Gate 3 (Tests): ✗ FAIL — ArchivePanel test expects old text
- Gate 4 (Build): ⏹ BLOCKED (not reached, test failed)
- Gate 5 (Backend Config Safety): ✓ PASS
- Gate 6 (Backend Config Refs): ✓ PASS
- Gate 7 (Docker): ⏹ BLOCKED (not reached)

### Verdict
**NO-GO.** Gate 3 (Tests) is failing. Fix the test expectation and re-run the gate sequence.

### Next Action
1. Update ArchivePanel.test.tsx to expect "Open full HTML" instead of "Open exported report"
2. Run `npm test` to verify the fix
3. Re-run full gate sequence
4. Report new results
```

---

## Notes
- All 7 gates must pass to deploy. There are no exceptions.
- If a gate passes and you still find a behavior bug, the test is incomplete. Mutation-test the test and improve it.
- "All gates pass but app crashes in prod" = test suite is insufficient. Use verifier-agent to catch behavioral issues before they ship.
