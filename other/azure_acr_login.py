import json
import os
import re
import shutil
import subprocess
import sys

def main():
    creds_str = os.environ.get('CREDS', '').strip()
    registry = os.environ.get('STRATO_CONTAINER_REGISTRY', '').strip()

    if not creds_str:
        print("ERROR: Environment variable CREDS is empty. Check STRATO_SP_ACR_CREDENTIALS secret.")
        sys.exit(1)

    data = {}
    try:
        data = json.loads(creds_str, strict=False)
    except Exception as err:
        print(f"Standard JSON parse note: {err}. Attempting sanitized parse...")
        try:
            cleaned = re.sub(r'[\r\n\t]+', ' ', creds_str)
            data = json.loads(cleaned, strict=False)
        except Exception:
            pass

    client_id = data.get('clientId') or data.get('appId') or data.get('client_id')
    client_secret = data.get('clientSecret') or data.get('password') or data.get('client_secret')
    tenant_id = data.get('tenantId') or data.get('tenant') or data.get('tenant_id')

    if not (client_id and client_secret and tenant_id):
        # Robust regex extraction if JSON structure was broken by formatting
        cid_match = re.search(r'["\']?(?:clientId|appId|client_id)["\']?\s*[:=]\s*["\']?([^"\'\s,{}]+)', creds_str, re.I)
        csec_match = re.search(r'["\']?(?:clientSecret|password|client_secret)["\']?\s*[:=]\s*["\']?([^"\'\s,{}]+)', creds_str, re.I)
        tid_match = re.search(r'["\']?(?:tenantId|tenant|tenant_id)["\']?\s*[:=]\s*["\']?([^"\'\s,{}]+)', creds_str, re.I)

        client_id = client_id or (cid_match.group(1) if cid_match else '')
        client_secret = client_secret or (csec_match.group(1) if csec_match else '')
        tenant_id = tenant_id or (tid_match.group(1) if tid_match else '')

    if not (client_id and client_secret and tenant_id):
        print(f"ERROR: Could not parse client credentials. Length of CREDS: {len(creds_str)}")
        sys.exit(1)

    print(f"Logging into Azure CLI via Service Principal (Client ID: {client_id[:4]}***, Tenant: {tenant_id[:4]}***)...")
    az_bin = shutil.which('az') or 'az'
    login_cmd = [
        az_bin, 'login', '--service-principal',
        '-u', client_id,
        '-p', client_secret,
        '--tenant', tenant_id,
        '--allow-no-subscriptions'
    ]
    use_shell = (sys.platform == 'win32')
    res = subprocess.run(login_cmd, capture_output=True, text=True, shell=use_shell)
    if res.returncode != 0:
        print(f"az login failed: {res.stderr or res.stdout}")
        sys.exit(res.returncode)

    print("Azure CLI login successful.")

    if registry:
        print(f"Logging into ACR registry: {registry}...")
        acr_cmd = [az_bin, 'acr', 'login', '-n', registry]
        res_acr = subprocess.run(acr_cmd, capture_output=True, text=True, shell=use_shell)
        if res_acr.returncode != 0:
            print(f"az acr login failed: {res_acr.stderr or res_acr.stdout}")
            sys.exit(res_acr.returncode)
        print("ACR login successful.")
    else:
        print("WARNING: STRATO_CONTAINER_REGISTRY not set, skipping az acr login.")

if __name__ == '__main__':
    main()
