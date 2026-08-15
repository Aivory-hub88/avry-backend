"""
AES-256-GCM encryption for tenant custom MCP server auth-header values
(product.tenant_custom_mcp_servers.auth_header_value_encrypted). Ports the
nonce+ciphertext+tag format already established twice independently
(frontend/avry-user-dashboard/lib/crypto.ts for dashboard.n8n_credentials,
services/avry-careers/app/services/encryption_service.py for PII/CV files)
into avry-backend, with its own dedicated env var — key rotation stays
decoupled from either existing use. See ADR-006 §B2.

Storage format: [12-byte nonce] + [ciphertext] + [16-byte GCM auth tag].
"""

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

_NONCE_SIZE = 12


def _get_key() -> bytes:
    raw_key = settings.mcp_server_auth_encryption_key

    if not raw_key:
        raise ValueError(
            "MCP_SERVER_AUTH_ENCRYPTION_KEY is not configured. Set it to enable "
            "tenant custom MCP server auth-header encryption."
        )

    if len(raw_key) == 64:
        try:
            return bytes.fromhex(raw_key)
        except ValueError:
            pass

    return hashlib.sha256(raw_key.encode("utf-8")).digest()


def encrypt_auth_header_value(plaintext: str) -> bytes:
    key = _get_key()
    nonce = os.urandom(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext_with_tag


def decrypt_auth_header_value(encrypted_data: bytes) -> str:
    if len(encrypted_data) <= _NONCE_SIZE:
        raise ValueError("Encrypted auth header value is too short to contain a valid nonce and ciphertext.")

    key = _get_key()
    nonce = encrypted_data[:_NONCE_SIZE]
    ciphertext_with_tag = encrypted_data[_NONCE_SIZE:]

    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
    return plaintext_bytes.decode("utf-8")
