import os, re
written = {}
read    = {}
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in ["__pycache__", ".venv", ".claude", "node_modules"]]
    for fname in files:
        if not fname.endswith(".py"): continue
        path = os.path.join(root, fname)
        src = open(path, encoding="utf-8", errors="ignore").read()
        for k in re.findall(r'ac_set\("([\w_]+)"', src):   written.setdefault(k,[]).append(fname)
        for k in re.findall(r"ac_set\('([\w_]+)'", src):   written.setdefault(k,[]).append(fname)
        for k in re.findall(r'ac_get\("([\w_]+)"', src):   read.setdefault(k,[]).append(fname)
        for k in re.findall(r"ac_get\('([\w_]+)'", src):   read.setdefault(k,[]).append(fname)
        for k in re.findall(r'session_cache\.set\("([\w_]+)"', src): written.setdefault(k,[]).append(fname)
        for k in re.findall(r"session_cache\.set\('([\w_]+)'", src): written.setdefault(k,[]).append(fname)
        for k in re.findall(r'session_cache\.get\("([\w_]+)"', src): read.setdefault(k,[]).append(fname)
        for k in re.findall(r"session_cache\.get\('([\w_]+)'", src): read.setdefault(k,[]).append(fname)

print("=== WRITTEN (source) ===")
for k in sorted(written): print(f"  {k:45s} <- {set(written[k])}")
print("\n=== READ (consumer) ===")
for k in sorted(read): print(f"  {k:45s} -> {set(read[k])}")
dead = [k for k in written if k not in read]
miss = [k for k in read if k not in written]
print(f"\n=== DEAD (written, never read): {dead}")
print(f"=== MISSING (read, never written): {miss}")
