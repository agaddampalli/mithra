"""
StorageAdapter — universal interface for all domain data.

StorageRecord is the single container for every read and write across all 15
domains. A record is uniquely identified by (domain, key, layer, user_id).

layers:
  "facts"       — keyed facts (e.g. person_a.birthday)
  "state"       — current snapshot for a domain
  "history"     — append-only event log
  "preferences" — user or household preference values

content_type="document_index" stores file metadata:
  data.file_path          where the file lives in OneDrive
  data.extracted          structured fields from OCR + LLM
  data.processing_status  pending | processing | complete | failed

Binary files are stored separately via write_file / read_file / delete_file.

EncryptedStorageAdapter wraps this interface using the decorator pattern.
StorageRouter enforces which adapter is used per domain — callers never
choose the backend themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class StorageRecord:
    """Universal container for all domain data."""

    domain: str
    key: str
    layer: str
    data: dict
    user_id: str
    version: int
    updated_at: str
    content_type: str = "record"


class StorageAdapter(ABC):
    """Abstract base class for all storage backends."""

    # --- Structured records ---

    @abstractmethod
    def read(self, domain: str, key: str, layer: str, user_id: str) -> StorageRecord | None:
        """Return a record by (domain, key, layer, user_id), or None if not found."""
        ...

    @abstractmethod
    def write(self, record: StorageRecord) -> StorageRecord:
        """Upsert a record and return it with updated version and updated_at."""
        ...

    @abstractmethod
    def list(self, domain: str, layer: str, user_id: str) -> list[StorageRecord]:
        """Return all records in a (domain, layer, user_id) scope."""
        ...

    @abstractmethod
    def delete(self, domain: str, key: str, user_id: str) -> bool:
        """Delete a record. Return True if it existed, False otherwise."""
        ...

    # --- Binary files ---

    @abstractmethod
    def write_file(
        self, domain: str, file_name: str, content: bytes, mime_type: str, user_id: str
    ) -> str:
        """Store a binary file and return its addressable file_path."""
        ...

    @abstractmethod
    def read_file(self, file_path: str) -> bytes:
        """Retrieve a binary file by the path returned from write_file."""
        ...

    @abstractmethod
    def delete_file(self, file_path: str) -> bool:
        """Delete a binary file. Return True if it existed, False otherwise."""
        ...

    # --- Health ---

    @abstractmethod
    def health(self) -> dict:
        """Return a health status dict for the health dashboard."""
        ...
