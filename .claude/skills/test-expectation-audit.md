# Skill: Test-Expectation Audit

**Purpose:** Detect when test expectations drift from UI implementation, causing silent test failures.

**Triggers:** After any change to a tested component (ArchivePanel.tsx, etc.), before running jest

**Problem:** A test that passes doesn't mean it's testing the right thing. This skill finds stale assertions.

---

## Manual Audit (Do This First)

### Step 1: Find All Test Files
```bash
find pe-dashboard-mfe/src -name "*.test.tsx" -o -name "*.test.ts" -o -name "*.spec.tsx"
```

### Step 2: For Each Test File
Open the .test.tsx and the corresponding .tsx file side-by-side.

**Example:** ArchivePanel.test.tsx ↔ ArchivePanel.tsx

### Step 3: Compare Assertions

**In test file (ArchivePanel.test.tsx line 36):**
```javascript
expect(await findByRole('link', { name: /open exported report/i })).toBeDefined()
```

**In UI file (ArchivePanel.tsx line 270):**
```javascript
<Button ... href={...}>Open full HTML</Button>
```

**Mismatch found:**
- Test expects: a `<link>` role with text "open exported report"
- UI renders: a `<Button>` (which is a `<button>` element) with text "Open full HTML"

**Action:** Update the test to match the UI OR update the UI to match the test (coordinated with product).

### Step 4: Mutation-Test Each Fix

After updating a test, prove it actually catches bugs:

```bash
# Edit ArchivePanel.tsx: change "Open full HTML" to "Download full HTML"
npm test                    # Should FAIL
# Revert the change
npm test                    # Should PASS
```

If the test does NOT fail after breaking the code, the test is not actually protecting anything. Improve it or delete it.

---

## Automated Audit (Script)

Create this script at `pe-dashboard-mfe/audit-test-expectations.js`:

```javascript
#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const glob = require('glob');

// Find all .test.tsx files
const testFiles = glob.sync('src/**/*.test.tsx');

testFiles.forEach(testFile => {
  const uiFile = testFile.replace('.test.tsx', '.tsx');
  
  if (!fs.existsSync(uiFile)) {
    console.warn(`⚠️  Test file ${testFile} has no corresponding UI file ${uiFile}`);
    return;
  }
  
  const testContent = fs.readFileSync(testFile, 'utf-8');
  const uiContent = fs.readFileSync(uiFile, 'utf-8');
  
  // Extract expected text from test assertions
  const testTexts = testContent.match(/name:\s*\/(.+?)\//gi) || [];
  
  // Extract rendered text from UI
  const uiTexts = uiContent.match(/>(.+?)</g) || [];
  
  testTexts.forEach(testText => {
    const cleaned = testText.match(/\/(.+?)\//)[1].toLowerCase();
    const found = uiTexts.some(ui => ui.toLowerCase().includes(cleaned));
    
    if (!found) {
      console.error(
        `❌ Mismatch in ${testFile}:\n` +
        `   Test expects: "${cleaned}"\n` +
        `   UI text not found in ${uiFile}\n`
      );
    }
  });
});

console.log('✓ Audit complete');
```

Wire to package.json:
```json
{
  "scripts": {
    "audit-tests": "node audit-test-expectations.js",
    "pre-commit": "npm run format && npm run lint && npm run audit-tests && npm test"
  }
}
```

Then run:
```bash
npm run audit-tests
```

---

## Detection Rules

### Rule 1: Button Text Mismatch
- **Test:** `findByRole('button', { name: /save changes/i })`
- **UI:** `<Button>Save</Button>`
- **Issue:** "save changes" ≠ "Save"
- **Fix:** Update test to `/save/i` (case-insensitive partial match)

### Rule 2: Link Role Missing
- **Test:** `findByRole('link', { name: /open report/i })`
- **UI:** `<Button href="...">Open report</Button>`
- **Issue:** Button is not a link role (it's a button role)
- **Fix:** Change UI to `<a href="...">Open report</a>` OR update test to `role="button"`

### Rule 3: Field Not Present
- **Test:** `getByLabelText('Customer ID')`
- **UI:** No field with label "Customer ID"
- **Issue:** Field was removed but test not updated
- **Fix:** Delete the test assertion OR add the field back to UI

### Rule 4: Conditional Rendering
- **Test:** `expect(getByText('Loading...')).toBeInTheDocument()`
- **UI:** `{isLoading && <div>Loading...</div>}`
- **Issue:** Test assumes `isLoading=true` but component is rendered with `isLoading=false`
- **Fix:** Update test setup to pass `isLoading={true}` to the component

---

## Sign That A Test Is Dead

- Test passes even after you intentionally break the feature
- Test fixture (mock data) is hardcoded and never used from real API
- Test doesn't interact with the component; it just checks that render() doesn't throw
- Test file hasn't been updated in >6 months while corresponding UI changed frequently

**Action:** Delete or rewrite the test. A dead test is worse than no test — it gives false confidence.

---

## When to Run This Skill

1. After any text/label change in a UI component
2. Before running `npm test` (as a pre-flight check)
3. After a component is refactored (buttons become links, etc.)
4. Before a PR review (ensure test assertions match what was actually implemented)
5. As part of pre-deploy checklist

---

## Report Template

```
## Test-Expectation Audit

**Files Checked:** 18 test files  
**Mismatches Found:** 1

### Issues

1. **ArchivePanel.test.tsx** (line 36)
   - Test expects: `role="link"` with name "Open exported report"
   - UI renders: `<Button>` (role="button") with text "Open full HTML"
   - Action: Update test to match UI
   
   Fix:
   ```javascript
   // OLD
   expect(await findByRole('link', { name: /open exported report/i })).toBeDefined()
   
   // NEW
   expect(await findByRole('button', { name: /open full html/i })).toBeDefined()
   ```

### Verdict
**Audit complete.** 1 mismatch found and fixed. Run `npm test` to verify.
```
