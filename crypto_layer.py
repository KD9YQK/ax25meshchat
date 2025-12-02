"""
Optional encryption layer for mesh payloads.

Encryption is disabled by default and becomes a pass-through.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from mesh_config import MeshSecurityConfig

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except ImportError:
    AESGCM = None   # type: ignore
    _HAS_CRYPTO = False


class MeshEncryptor:
    """
    Handles optional encryption/decryption of payloads.

    - If encryption disabled → passthrough.
    - If enabled but `cryptography` missing → falls back to passthrough.
    - Uses AES-GCM with 12-byte nonce.

    No broad exceptions, fully Python 3.10 safe.
    """

    def __init__(self, config: MeshSecurityConfig) -> None:
        self._enabled = False
        self._aesgcm: Optional[AESGCM] = None
        self._key: Optional[bytes] = None

        if not config.enable_encryption:
            return

        if not _HAS_CRYPTO:
            return

        if config.key is None:
            return

        # key provided, crypto available → enable encryption
        self._enabled = True
        self._key = config.key
        self._aesgcm = AESGCM(self._key)

    @property
    def encryption_enabled(self) -> bool:
        return self._enabled

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> Tuple[bytes, bytes]:
        """
        Returns (nonce, ciphertext). If encryption disabled:
            returns (b"", plaintext)
        """
        if not self._enabled or self._aesgcm is None:
            return b"", plaintext

        nonce = os.urandom(12)
        ciphertext = self._aesgcm.encrypt(
            nonce=nonce,
            data=plaintext,
            associated_data=associated_data,
        )
        return nonce, ciphertext

    def decrypt(self, nonce: bytes, ciphertext: bytes, associated_data: bytes) -> bytes:
        """
        Returns plaintext. If encryption disabled:
            returns ciphertext unchanged.
        """
        if not self._enabled or self._aesgcm is None:
            return ciphertext

        plaintext = self._aesgcm.decrypt(
            nonce=nonce,
            data=ciphertext,
            associated_data=associated_data,
        )
        return plaintext
