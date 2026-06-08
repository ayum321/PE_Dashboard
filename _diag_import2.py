"""Find exact file inside azure.identity that hangs."""
import sys, threading, importlib.util, importlib

# We need to load _constants.py WITHOUT triggering azure.identity.__init__
# Do this by manually loading just the file
import importlib.util as _u

def try_file(path, mod_name):
    result = []
    def do():
        try:
            spec = _u.spec_from_file_location(mod_name, path, submodule_search_locations=[])
            m = _u.module_from_spec(spec)
            spec.loader.exec_module(m)
            result.append('OK')
        except Exception as e:
            result.append(f'ERR: {e}')
    t = threading.Thread(target=do, daemon=True)
    t.start()
    t.join(5)
    return 'HANGS' if t.is_alive() else (result[0] if result else '?')

import os
base = r'.venv\Lib\site-packages\azure\identity'
files = [
    ('_constants.py', '_constants'),
    ('_exceptions.py', '_exceptions'),
    ('_auth_record.py', '_auth_record'),
    ('_persistent_cache.py', '_persistent_cache'),
    ('_bearer_token_provider.py', '_bearer_token_provider'),
    (r'_credentials\azure_cli.py', '_azure_cli'),
    (r'_credentials\managed_identity.py', '_managed_identity'),
    (r'_credentials\vscode.py', '_vscode'),
    (r'_credentials\shared_cache.py', '_shared_cache'),
    (r'_credentials\browser.py', '_browser'),
    (r'_credentials\default.py', '_default'),
    (r'_internal\__init__.py', '_internal'),
    (r'_internal\shared_token_cache.py', '_shared_token_cache'),
    (r'_internal\msal_credentials.py', '_msal_credentials'),
]

for rel, name in files:
    full = os.path.join(base, rel)
    r = try_file(full, name)
    print(f'{r:8s}  {rel}')
    if r == 'HANGS':
        print('=> CULPRIT:', rel)
        break
