"""Deterministic check: every `_pc.NAME` / `pe_config.NAME` reference in the
codebase must resolve to an actual module-level name (constant or function)
in services/pe_config.py.

Catches the recurring "AttributeError: module 'pe_config' has no attribute X"
class of bug — a real, repeated incident class in this repo (DB_MEM_BAND_LOW,
RESOURCE_CAPTURE_DAYS were both added to code that referenced them before they
existed in pe_config.py). This is exactly the kind of mistake a linter should
catch, not something a human should have to remember to grep for.

Run:
    py -3.14 _check_pe_config_refs.py

Wired into dev.bat / start.bat next to _validate_js.py — both must pass before
the server starts.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PE_CONFIG = ROOT / "services" / "pe_config.py"
SELF_FILE = Path(__file__).resolve()
# Only scan the actual shipped app — not one-off dev/audit/diagnostic scripts at
# the repo root, which reference "pe_config.py" / "pe_config.json" in prose/strings.
SCAN_TARGETS = ("routers", "services", "main.py")

_REF_RE = re.compile(r"\b(?:_pc|pe_config)\.([A-Za-z_][A-Za-z0-9_]*)")
# Filename-extension false positives: "pe_config.py" / "pe_config.json" in comments/paths.
_NOT_A_REAL_NAME = {"py", "json"}


def _collect_pe_config_names(path: Path) -> set[str]:
    """Every module-level constant / function / class name defined in pe_config.py."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    for elt in t.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _scan_file(path: Path, known: set[str]) -> list[str]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return errors
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("#"):
            continue
        for m in _REF_RE.finditer(line):
            name = m.group(1)
            if name in _NOT_A_REAL_NAME:
                continue
            if name not in known:
                errors.append(f"{path.relative_to(ROOT)}:{lineno}: pe_config.{name} — not defined in services/pe_config.py\n      {line.strip()[:120]}")
    return errors


def main() -> int:
    if not PE_CONFIG.exists():
        print(f"[ERROR] {PE_CONFIG} not found")
        return 1

    known = _collect_pe_config_names(PE_CONFIG)

    files: list[Path] = []
    for target in SCAN_TARGETS:
        p = ROOT / target
        if p.is_dir():
            files.extend(p.rglob("*.py"))
        elif p.is_file():
            files.append(p)
    files = sorted({f for f in files if f.resolve() not in (PE_CONFIG.resolve(), SELF_FILE)})

    all_errors: list[str] = []
    for f in files:
        all_errors.extend(_scan_file(f, known))

    if all_errors:
        print(f"[FAIL] {len(all_errors)} undefined pe_config reference(s):\n")
        for e in all_errors:
            print(f"  {e}")
        print("\n  Fix: add the missing constant to services/pe_config.py (module-level),")
        print("  plus the `global` decl + reload() body entry if it should be Settings-overridable.")
        return 1

    print(f"[OK] All pe_config references resolve ({len(known)} known names, {len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
