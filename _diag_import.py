"""Diagnose which azure.identity sub-import blocks."""
import sys, threading, time

def try_import(mod):
    result = []
    def do():
        try:
            __import__(mod)
            result.append('OK')
        except Exception as e:
            result.append(f'ERROR: {e}')
    t = threading.Thread(target=do, daemon=True)
    t.start()
    t.join(timeout=5)
    if t.is_alive():
        return 'HANGS'
    return result[0] if result else 'NO RESULT'

mods = [
    'azure.core',
    'msal',
    'azure.identity._constants',
    'azure.identity._exceptions',
    'azure.identity._auth_record',
    'azure.identity._persistent_cache',
    'azure.identity._internal.utils',
    'azure.identity._internal.decorators',
    'azure.identity._internal.get_token_mixin',
    'azure.identity._credentials.managed_identity',
    'azure.identity._credentials.azure_cli',
    'azure.identity._credentials.shared_cache',
    'azure.identity._credentials.browser',
    'azure.identity._credentials.default',
    'azure.identity._internal',
    'azure.identity._credentials',
    'azure.identity',
]

for m in mods:
    r = try_import(m)
    print(f'{r:8s}  {m}')
    sys.stdout.flush()
    if r == 'HANGS':
        print('==> FOUND THE CULPRIT:', m)
        break
