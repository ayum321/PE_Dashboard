import os

f = r'.venv\Lib\site-packages\azure\identity\_credentials\__init__.py'
bak = f + '.bak'

# Read backup (original)
with open(bak) as fh:
    orig = fh.read()

# Write instrumented version
lines = ['import sys as _s', '_s.stdout.write("CRED_INIT_START\\n"); _s.stdout.flush()']
for line in orig.splitlines():
    lines.append(line)
    if line.startswith('from .') and ' import ' in line:
        mod = line.split('from .')[1].split(' import')[0].strip()
        lines.append('_s.stdout.write("CRED:' + mod + '\\n"); _s.stdout.flush()')

with open(f, 'w') as fh:
    fh.write('\n'.join(lines))
print('Patched OK')
print(open(f).read()[:300])
