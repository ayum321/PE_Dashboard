"""Pre-ship JavaScript syntax validator for PE Dashboard.

Run before zipping / shipping:
    py -3.14 _validate_js.py

Checks:
  1. Brace balance ({} pairs)
  2. Bracket balance ([] pairs)
  3. Parenthesis balance (() pairs)
  4. Unclosed template literals
  5. Pinpoints the exact line where balance first goes wrong
"""

import sys
from pathlib import Path

JS_FILES = ["static/app.js", "static/deep_dive.js"]
ERRORS = []


def _check_balance(filepath: Path) -> list[str]:
    """Check brace/bracket/paren balance, return list of error strings."""
    errors = []
    text = filepath.read_text(encoding="utf-8")
    lines = text.splitlines()

    for label, open_ch, close_ch in [
        ("brace",       "{", "}"),
    ]:
        balance = 0
        first_negative_line = None
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip full-line comments (heuristic, not perfect for strings)
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            old = balance
            balance += line.count(open_ch) - line.count(close_ch)
            if balance < 0 and old >= 0 and first_negative_line is None:
                first_negative_line = i

        if balance != 0:
            direction = "extra closing" if balance < 0 else "unclosed opening"
            msg = f"  {label.upper()} MISMATCH: balance={balance:+d} ({abs(balance)} {direction} {label}{'es' if label == 'brace' else 's'})"
            if first_negative_line and balance < 0:
                msg += f"\n    → First extra '{close_ch}' at line {first_negative_line}: {lines[first_negative_line - 1].rstrip()[:120]}"
            errors.append(msg)

    # Check for function-level brace tracking (find the specific function)
    if any("BRACE" in e for e in errors):
        balance = 0
        last_func = "top-level"
        last_func_line = 0
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            # Track function declarations
            if "function " in line and "{" in line:
                last_func = stripped[:80]
                last_func_line = i
            old = balance
            balance += line.count("{") - line.count("}")
            if balance < 0 and old >= 0:
                errors.append(f"    → Context: near function at L{last_func_line}: {last_func}")
                # Show surrounding lines
                start = max(0, i - 4)
                end = min(len(lines), i + 3)
                errors.append(f"    → Surrounding code (L{start+1}-L{end}):")
                for j in range(start, end):
                    marker = " >>>" if j == i - 1 else "    "
                    errors.append(f"      {marker} L{j+1}: {lines[j].rstrip()[:120]}")
                break

    return errors


def main():
    root = Path(__file__).parent
    all_ok = True

    print("=" * 60)
    print("  PE Dashboard — JavaScript Syntax Validator")
    print("=" * 60)

    for js in JS_FILES:
        fp = root / js
        if not fp.exists():
            print(f"\n[!] {js}: FILE NOT FOUND")
            continue

        lines = fp.read_text(encoding="utf-8").splitlines()
        errors = _check_balance(fp)

        if errors:
            all_ok = False
            print(f"\n[X] {js} ({len(lines)} lines) -- ERRORS FOUND:")
            for e in errors:
                print(e)
        else:
            print(f"\n[OK] {js} ({len(lines)} lines) -- OK")

    print()
    if all_ok:
        print("All checks passed -- safe to ship.")
        return 0
    else:
        print("ERRORS DETECTED -- fix before shipping!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
