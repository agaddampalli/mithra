"""
StorageRouter — resolves which StorageAdapter to use per domain.

Hard routing rules (cannot be changed at runtime):
  health      → EncryptedStorageAdapter(LocalStorageAdapter())   ALWAYS
  money       → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
  documents   → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
  people      → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
  default     → OneDriveAdapter()

The health exception is non-negotiable. It is enforced here and not configurable
via YAML, environment variables, or any runtime mechanism.

StorageAdapter.write() implementations must call cache.invalidate_domain()
immediately on every write so ContextCache is kept consistent.
"""

from __future__ import annotations

from mithra.storage.adapter import StorageAdapter

_HEALTH_DOMAIN = "health"
_ENCRYPTED_CLOUD_DOMAINS = frozenset({"money", "documents", "people"})


class StorageRouter:
    """Returns the correct StorageAdapter for a given domain."""

    def __init__(
        self,
        local_adapter: StorageAdapter,
        onedrive_adapter: StorageAdapter,
        encrypted_local_adapter: StorageAdapter,
        encrypted_onedrive_adapter: StorageAdapter,
    ) -> None:
        self._local = local_adapter
        self._onedrive = onedrive_adapter
        self._encrypted_local = encrypted_local_adapter
        self._encrypted_onedrive = encrypted_onedrive_adapter

    def get_adapter(self, domain: str) -> StorageAdapter:
        """Return the adapter that must be used for the given domain.

        health always returns the encrypted local adapter — this is a hard rule
        that cannot be overridden by configuration or runtime arguments.
        """
        if domain == _HEALTH_DOMAIN:
            return self._encrypted_local
        if domain in _ENCRYPTED_CLOUD_DOMAINS:
            return self._encrypted_onedrive
        return self._onedrive
