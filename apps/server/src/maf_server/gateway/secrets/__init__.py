"""Secret resolution boundary.

Re-exports the SecretService Protocol and the local concrete implementation
(``LocalSecretService``) plus the SecretStore backends (``KeyringStore`` /
``AesGcmFileStore``). Business code should depend on ``SecretService`` /
``SecretStore`` Protocols; bootstrap wires ``LocalSecretService`` with the
appropriate backends.
"""

from maf_server.core.secrets import SecretStore
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.keyring_store import KeyringStore
from maf_server.gateway.secrets.local_service import (
    LocalSecretService,
    PermissionPolicy,
    SecretMetadata,
)
from maf_server.gateway.secrets.service import SecretService

__all__ = [
    "AesGcmFileStore",
    "KeyringStore",
    "LocalSecretService",
    "PermissionPolicy",
    "SecretMetadata",
    "SecretService",
    "SecretStore",
]
