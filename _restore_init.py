import os

# Clean version of azure/identity/__init__.py (restore original without diagnostics)
f = r'.venv\Lib\site-packages\azure\identity\__init__.py'

clean = '''# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------
"""Credentials for Azure SDK clients."""

from ._auth_record import AuthenticationRecord
from ._exceptions import AuthenticationRequiredError, CredentialUnavailableError
from ._constants import AzureAuthorityHosts, KnownAuthorities
from ._credentials import (
    AuthorizationCodeCredential,
    AzureDeveloperCliCredential,
    AzureCliCredential,
    AzurePowerShellCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientAssertionCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
    DeviceCodeCredential,
    EnvironmentCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    OnBehalfOfCredential,
    SharedTokenCacheCredential,
    UsernamePasswordCredential,
    VisualStudioCodeCredential,
    WorkloadIdentityCredential,
    AzurePipelinesCredential,
)
from ._persistent_cache import TokenCachePersistenceOptions
from ._bearer_token_provider import get_bearer_token_provider


__all__ = [
    "AuthenticationRecord",
    "AuthenticationRequiredError",
    "AuthorizationCodeCredential",
    "AzureAuthorityHosts",
    "AzureCliCredential",
    "AzureDeveloperCliCredential",
    "AzurePipelinesCredential",
    "AzurePowerShellCredential",
    "CertificateCredential",
    "ChainedTokenCredential",
    "ClientAssertionCredential",
    "ClientSecretCredential",
    "CredentialUnavailableError",
    "DefaultAzureCredential",
    "DeviceCodeCredential",
    "EnvironmentCredential",
    "InteractiveBrowserCredential",
    "KnownAuthorities",
    "OnBehalfOfCredential",
    "ManagedIdentityCredential",
    "SharedTokenCacheCredential",
    "TokenCachePersistenceOptions",
    "UsernamePasswordCredential",
    "VisualStudioCodeCredential",
    "WorkloadIdentityCredential",
    "get_bearer_token_provider",
]

from ._version import VERSION

__version__ = VERSION
'''

with open(f, 'w') as fh:
    fh.write(clean)
print(f'Written clean {f}')
print(open(f).read()[:200])
