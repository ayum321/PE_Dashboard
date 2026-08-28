#!/usr/bin/env python3
"""
Deterministic Test-Expectation Checker

Scans test files for hardcoded UI expectations and compares them against 
the actual UI implementation. Catches stale test assertions automatically.

Usage:
  python check_test_expectations.py                # Check all tests
  python check_test_expectations.py ArchivePanel   # Check specific component
  npm run check-tests                             # Wired to package.json

Exit codes:
  0 = All checks pass (no mismatches found)
  1 = Mismatches found (test expectations don't match UI)
  2 = Scan error (file not found, read error, etc.)

Wiring to pre-commit:
  Add to package.json:
    "scripts": {
      "check-tests": "python ../check_test_expectations.py",
      "pre-commit": "npm run format && npm run lint && npm run check-tests && npm test"
    }
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Lockstep pairs: (test_file, ui_file) — these must be kept in sync
LOCKSTEP_PAIRS = [
    ('ArchivePanel.test.tsx', 'ArchivePanel.tsx'),
    ('ExecutivePanel.test.tsx', 'ExecutivePanel.tsx'),
    ('BatchPanel.test.tsx', 'BatchPanel.tsx'),
]

class TestExpectationChecker:
    def __init__(self, src_root: Path = None):
        if src_root is None:
            # Auto-detect: script is in root, tests are in pe-dashboard-mfe/src/
                src_root = Path(__file__).parent / 'frontend' / 'PE_Dashboard_MFE' / 'source' / 'src'
        self.src_root = src_root
        self.mismatches: List[Dict] = []
    
    def find_expected_texts(self, test_file: Path) -> List[str]:
        """
        Extract expected text from test file.
        
        Pattern 1: findByRole('button', { name: /submit form/i })
        Pattern 2: findByText(/welcome/i)
        Pattern 3: expect(... name /delete account/i).toBeDefined()
        """
        if not test_file.exists():
            return []
        
        content = test_file.read_text(encoding='utf-8')
        
        # Pattern: /text here/i (case-insensitive regex in test)
        regex_patterns = re.findall(r'/([^/]+)/i', content)
        
        # Pattern: "text here" or 'text here' (string literals)
        string_patterns = re.findall(r'(?:name|text):\s*["\']([^"\']+)["\']', content)
        
        return [p.lower() for p in regex_patterns + string_patterns]
    
    def find_ui_texts(self, ui_file: Path) -> List[str]:
        """
        Extract rendered text from UI file.
        
        Pattern 1: >Submit Form<
        Pattern 2: label="Delete Account"
        Pattern 3: placeholder="Enter email"
        """
        if not ui_file.exists():
            return []
        
        content = ui_file.read_text(encoding='utf-8')
        
        # Pattern: >text</
        rendered_texts = re.findall(r'>([^<>]+)<', content)
        
        # Pattern: label="text", placeholder="text", etc.
        attr_texts = re.findall(r'(?:label|placeholder|title)=["\'](.*?)["\']', content)
        
        return [t.lower().strip() for t in rendered_texts + attr_texts if t.strip()]
    
    def check_pair(self, test_file: Path, ui_file: Path) -> bool:
        """
        Compare test expectations against UI implementation.
        Returns True if all expectations are found in UI.
        """
        expected = self.find_expected_texts(test_file)
        actual = self.find_ui_texts(ui_file)
        
        if not expected:
            # No test expectations found (suspicious, but not a failure)
            return True
        
        mismatches_in_file = []
        
        for exp in expected:
            found = False
            for act in actual:
                # Substring match (test expects part of UI text)
                if exp in act or act in exp:
                    found = True
                    break
            
            if not found:
                mismatches_in_file.append({
                    'test_file': str(test_file.relative_to(self.src_root.parent)),
                    'ui_file': str(ui_file.relative_to(self.src_root.parent)),
                    'expected': exp,
                    'actual_texts': actual[:3],  # Show first 3 actual texts
                })
        
        if mismatches_in_file:
            self.mismatches.extend(mismatches_in_file)
            return False
        
        return True
    
    def scan_all_pairs(self, component_filter: str = None) -> bool:
        """
        Scan all lockstep pairs. Returns True if all pass.
        """
        all_pass = True
        
        for test_name, ui_name in LOCKSTEP_PAIRS:
            if component_filter and component_filter.lower() not in test_name.lower():
                continue
            
            # Find the files
            test_path = None
            ui_path = None
            
            # Search in panels/, views/, components/
            for search_dir in ['panels', 'views', 'components']:
                candidate_test = self.src_root / search_dir / test_name
                candidate_ui = self.src_root / search_dir / ui_name
                
                if candidate_test.exists() or candidate_ui.exists():
                    test_path = candidate_test
                    ui_path = candidate_ui
                    break
            
            if test_path and ui_path:
                if not self.check_pair(test_path, ui_path):
                    all_pass = False
        
        return all_pass
    
    def report(self):
        """Print human-readable report."""
        if not self.mismatches:
            print("✓ All test expectations match UI implementations")
            return True
        
        print(f"❌ Found {len(self.mismatches)} mismatch(es):\n")
        
        for mismatch in self.mismatches:
            print(f"File: {mismatch['test_file']}")
            print(f"      ↔ {mismatch['ui_file']}")
            print(f"  Expected text: '{mismatch['expected']}'")
            print(f"  UI texts found: {mismatch['actual_texts']}")
            print()
        
        print("ACTION: Update test expectations to match UI, or update UI to match test expectations.")
        print("VERIFY: Run `npm test` after fixing. The test must FAIL before the fix, PASS after.")
        
        return False

def main():
    component_filter = sys.argv[1] if len(sys.argv) > 1 else None
    
    try:
        checker = TestExpectationChecker()
        all_pass = checker.scan_all_pairs(component_filter)
        checker.report()
        
        sys.exit(0 if all_pass else 1)
    except Exception as e:
        print(f"✗ Error during check: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == '__main__':
    main()
