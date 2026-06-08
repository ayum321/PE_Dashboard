import os

f = r'.venv\Lib\site-packages\azure\identity\_internal\aad_client.py'
bak = f + '.bak'

with open(f) as fh:
    orig = fh.read()

if not os.path.exists(bak):
    with open(bak, 'w') as fh:
        fh.write(orig)

out = ['import sys as _saad', '_saad.stdout.write("AAD_START\\n"); _saad.stdout.flush()']

for line in orig.splitlines():
    out.append(line)
    stripped = line.strip()
    if (stripped.startswith('import ') or stripped.startswith('from ')) and not stripped.startswith('#'):
        # Tag each import
        tag = stripped[:80].replace('"', "'")
        out.append(f'_saad.stdout.write("AAD:{tag}\\n"); _saad.stdout.flush()')

with open(f, 'w') as fh:
    fh.write('\n'.join(out))
print('Patched aad_client.py OK')
