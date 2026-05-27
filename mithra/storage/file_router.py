"""
FileStorageRouter — routes file operations to the correct FileStorageAdapter.

Health domain ALWAYS routes to LocalFileAdapter. Hardcoded. Non-configurable.
All other domains route to OneDriveFileAdapter when available, local otherwise.

This mirrors the StorageRouter pattern but for unstructured file storage.
"""

from __future__ import annotations

from mithra.storage.file_adapter import FileStorageAdapter

_HEALTH_DOMAIN = "health"


class FileStorageRouter:
    """Routes file storage calls by domain. Enforces health domain isolation."""

    def __init__(
        self,
        local_adapter: FileStorageAdapter,
        onedrive_adapter: FileStorageAdapter | None = None,
    ) -> None:
        """
        Args:
            local_adapter: Always used for health domain. Required.
            onedrive_adapter: Used for all non-health domains. If None, local is used.
        """
        self._local = local_adapter
        self._onedrive = onedrive_adapter

    def get_adapter(self, domain: str) -> FileStorageAdapter:
        """Return the correct FileStorageAdapter for the given domain."""
        if domain == _HEALTH_DOMAIN:
            # Hard rule: health files never leave the device
            return self._local
        if self._onedrive is not None:
            return self._onedrive
        return self._local

    def health(self) -> dict:
        result: dict = {
            "local": self._local.health(),
            "onedrive": self._onedrive.health() if self._onedrive else {"status": "not_configured"},
        }
        all_ok = all(v.get("status") == "ok" for v in result.values() if v.get("status") != "not_configured")
        result["status"] = "ok" if all_ok else "degraded"
        return result
