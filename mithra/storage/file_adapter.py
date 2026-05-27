"""
FileStorageAdapter — abstract base for unstructured file storage.

Separate from StorageAdapter which handles structured records.
Implementations: LocalFileAdapter, OneDriveFileAdapter.
Routing: FileStorageRouter enforces health domain → LocalFileAdapter only.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


def generate_file_id() -> str:
    """Return a stable, URL-safe unique file identifier."""
    return f"file_{uuid.uuid4().hex}"


@dataclass
class FileRecord:
    """Metadata for a stored file. Content lives in the adapter's backing store."""

    id: str
    household_id: str
    user_id: str
    domain: str
    filename: str
    storage: str          # "local" | "onedrive"
    path: str             # adapter-specific path or remote ID
    size_bytes: int | None
    mime_type: str | None
    uploaded_at: str      # ISO timestamp
    metadata: dict = field(default_factory=dict)
    chunk_ids: list[str] = field(default_factory=list)


class FileStorageAdapter(ABC):
    """Abstract interface for binary file storage backends."""

    @abstractmethod
    def save_file(
        self,
        household_id: str,
        user_id: str,
        domain: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        metadata: dict | None = None,
    ) -> FileRecord:
        """Persist content and return a FileRecord with the assigned id and path."""

    @abstractmethod
    def get_file(self, file_id: str, household_id: str) -> bytes:
        """Return the raw bytes for a stored file."""

    @abstractmethod
    def delete_file(self, file_id: str, household_id: str) -> bool:
        """Delete a file. Return True if it existed."""

    @abstractmethod
    def list_files(
        self,
        household_id: str,
        domain: str,
        user_id: str | None = None,
    ) -> list[FileRecord]:
        """Return FileRecords filtered by household + domain, optionally by user."""

    @abstractmethod
    def get_record(self, file_id: str, household_id: str) -> FileRecord | None:
        """Return the FileRecord metadata without fetching content."""

    @abstractmethod
    def update_chunk_ids(
        self, file_id: str, household_id: str, chunk_ids: list[str]
    ) -> None:
        """Store ChromaDB chunk IDs after a file has been indexed."""

    @abstractmethod
    def health(self) -> dict:
        """Return status dict for the health dashboard."""
