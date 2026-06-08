import os

f = r'.venv\Lib\site-packages\azure\identity\_internal\__init__.py'
bak = f + '.bak'

# Read backup (original)
src = bak if os.path.exists(bak) else f
with open(src) as fh:
    orig = fh.read()

if not os.path.exists(bak):
    with open(bak, 'w') as fh:
        fh.write(orig)

# Write instrumented version — must handle multi-line from...import(...) blocks
out = ['import sys as _si', '_si.stdout.write("INTERNAL_INIT_START\\n"); _si.stdout.flush()']
in_multiline = False
pending_mod = None

for line in orig.splitlines():
    out.append(line)
    stripped = line.strip()

    if in_multiline:
        # End of multi-line import block?
        if ')' in stripped:
            in_multiline = False
            out.append(f'_si.stdout.write("INT:{pending_mod}\\n"); _si.stdout.flush()')
    elif stripped.startswith('from .') and ' import ' in stripped:
        mod = stripped.split('from .')[1].split(' import')[0].strip()
        if '(' in stripped and ')' not in stripped:
            # Multi-line import starts here
            in_multiline = True
            pending_mod = mod
        else:
            out.append(f'_si.stdout.write("INT:{mod}\\n"); _si.stdout.flush()')

with open(f, 'w') as fh:
    fh.write('\n'.join(out))
print('Patched OK')
print(open(f).read()[:500])

