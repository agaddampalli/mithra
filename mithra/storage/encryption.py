"""
EncryptedStorageAdapter — decorator that wraps any StorageAdapter with AES-256-GCM
field-level encryption for structured records and full-content encryption for files.

Pattern: decorator — EncryptedStorageAdapter(adapter) wraps any StorageAdapter.
Algorithm: AES-256-GCM.
Key source: MITHRA_ENCRYPTION_KEY env var (base64-encoded 32-byte key).

Structured data: encrypts specific fields listed in encrypted_fields.
  If encrypted_fields is None, all fields in data are encrypted.
Binary files: full content encrypted before write, decrypted on read.

AESGCMCipher handles the raw encrypt/decrypt operations. It is not used
directly by any module other than EncryptedStorageAdapter.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mithra.storage.adapter import StorageAdapter, StorageRecord


class AESGCMCipher:
    """AES-256-GCM encrypt/decrypt primitive."""

    def __init__(self, key_b64: str) -> None:
        """
        Args:
            key_b64: Base64-encoded 32-byte encryption key from MITHRA_ENCRYPTION_KEY.
        """
        padded = key_b64 + "=" * (-len(key_b64) % 4)
        self._key = base64.b64decode(padded)
        if len(self._key) != 32:
            raise ValueError("Encryption key must be 32 bytes (base64-encoded).")
        self._aesgcm = AESGCM(self._key)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Return authenticated ciphertext with 12-byte nonce prepended."""
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ct

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Verify authentication tag and return plaintext."""
        nonce, ct = ciphertext[:12], ciphertext[12:]
        return self._aesgcm.decrypt(nonce, ct, None)

    def encrypt_field(self, value: Any) -> str:
        """Encrypt any JSON-serialisable value; return base64-encoded ciphertext string."""
        plaintext = json.dumps(value).encode()
        return base64.b64encode(self.encrypt(plaintext)).decode()

    def decrypt_field(self, encrypted: str) -> Any:
        """Decrypt a base64-encoded field and restore the original Python type."""
        plaintext = self.decrypt(base64.b64decode(encrypted))
        return json.loads(plaintext)


class EncryptedStorageAdapter(StorageAdapter):
    """Decorator that adds AES-256-GCM encryption to any StorageAdapter."""

    def __init__(
        self,
        inner: StorageAdapter,
        cipher: AESGCMCipher,
        encrypted_fields: list[str] | None = None,
        # None  → encrypt every key in record.data (used for health domain)
        # list  → encrypt only the named top-level keys in record.data
    ) -> None:
        """
        Args:
            inner: The underlying StorageAdapter to wrap.
            cipher: AESGCMCipher instance for this adapter.
            encrypted_fields: Specific data fields to encrypt. See comment above.
        """
        self._inner = inner
        self._cipher = cipher
        self._encrypted_fields = encrypted_fields

    def _encrypt_data(self, data: dict) -> dict:
        fields = self._encrypted_fields if self._encrypted_fields is not None else list(data.keys())
        result = dict(data)
        for f in fields:
            if f in result:
                result[f] = self._cipher.encrypt_field(result[f])
        return result

    def _decrypt_data(self, data: dict) -> dict:
        fields = self._encrypted_fields if self._encrypted_fields is not None else list(data.keys())
        result = dict(data)
        for f in fields:
            if f in result and isinstance(result[f], str):
                try:
                    result[f] = self._cipher.decrypt_field(result[f])
                except Exception:
                    pass  # non-encrypted legacy field; leave as-is
        return result

    def read(self, domain: str, key: str, layer: str, user_id: str) -> StorageRecord | None:
        """Read and decrypt a record from the inner adapter."""
        record = self._inner.read(domain, key, layer, user_id)
        if record is None:
            return None
        record.data = self._decrypt_data(record.data)
        return record

    def write(self, record: StorageRecord) -> StorageRecord:
        """Encrypt and write a record to the inner adapter."""
        encrypted = StorageRecord(
            domain=record.domain,
            key=record.key,
            layer=record.layer,
            data=self._encrypt_data(record.data),
            user_id=record.user_id,
            version=record.version,
            updated_at=record.updated_at,
            content_type=record.content_type,
        )
        saved = self._inner.write(encrypted)
        record.version = saved.version
        record.updated_at = saved.updated_at
        return record

    def list(self, domain: str, layer: str, user_id: str) -> list[StorageRecord]:
        """List and decrypt all records from the inner adapter."""
        records = self._inner.list(domain, layer, user_id)
        for r in records:
            r.data = self._decrypt_data(r.data)
        return records

    def delete(self, domain: str, key: str, user_id: str) -> bool:
        """Delegate delete to the inner adapter."""
        return self._inner.delete(domain, key, user_id)

    def write_file(
        self, domain: str, file_name: str, content: bytes, mime_type: str, user_id: str
    ) -> str:
        """Encrypt file content before writing to the inner adapter."""
        encrypted_content = self._cipher.encrypt(content)
        return self._inner.write_file(domain, file_name, encrypted_content, mime_type, user_id)

    def read_file(self, file_path: str) -> bytes:
        """Read and decrypt file content from the inner adapter."""
        encrypted_content = self._inner.read_file(file_path)
        return self._cipher.decrypt(encrypted_content)

    def delete_file(self, file_path: str) -> bool:
        """Delegate file delete to the inner adapter."""
        return self._inner.delete_file(file_path)

    def health(self) -> dict:
        """Return inner adapter health with encryption layer info added."""
        h = self._inner.health()
        h["encrypted"] = True
        h["encrypted_fields"] = self._encrypted_fields if self._encrypted_fields else "all"
        return h
