"""
OneDriveFileAdapter — Microsoft OneDrive file storage via Graph API.

Files stored at: Mithra/{household_id}/{domain}/{filename}
Metadata cached in the local SQLite files table for fast listing.
Uses OAuthManager for token management (no Nango).

Never used for health domain — FileStorageRouter enforces local-only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from mithra.storage.file_adapter import FileRecord, FileStorageAdapter, generate_file_id

if TYPE_CHECKING:
    from mithra.integrations.microsoft_graph import MicrosoftGraphClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        household_id=row["household_id"],
        user_id=row["user_id"],
        domain=row["domain"],
        filename=row["filename"],
        storage=row["storage"],
        path=row["path"],
        size_bytes=row["size_bytes"],
        mime_type=row["mime_type"],
        uploaded_at=row["uploaded_at"],
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        chunk_ids=json.loads(row["chunk_ids"]) if row["chunk_ids"] else [],
    )


class OneDriveFileAdapter(FileStorageAdapter):
    """OneDrive-backed file storage with local SQLite metadata cache."""

    def __init__(self, graph_client: "MicrosoftGraphClient", db_path: str) -> None:
        """
        Args:
            graph_client: Authenticated MicrosoftGraphClient.
            db_path: Path to the SQLite DB (must have the files table).
        """
        self._graph = graph_client
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    async def save_file(  # type: ignore[override]
        self,
        household_id: str,
        user_id: str,
        domain: str,
        filename: str,
        content: bytes,
        mime_type: str | None = None,
        metadata: dict | None = None,
    ) -> FileRecord:
        file_id = generate_file_id()
        remote_path = f"Mithra/{household_id}/{domain}/{file_id}_{filename}"
        item = await self._graph.upload_file(
            user_id, remote_path, content, mime_type or "application/octet-stream"
        )
        item_id = item.get("id", remote_path)

        record = FileRecord(
            id=file_id,
            household_id=household_id,
            user_id=user_id,
            domain=domain,
            filename=filename,
            storage="onedrive",
            path=item_id,
            size_bytes=len(content),
            mime_type=mime_type,
            uploaded_at=_now(),
            metadata=metadata or {},
            chunk_ids=[],
        )
        self._insert(record)
        return record

    async def get_file(self, file_id: str, household_id: str) -> bytes:  # type: ignore[override]
        record = self.get_record(file_id, household_id)
        if record is None:
            raise FileNotFoundError(f"File not found: {file_id}")
        return await self._graph.download_file(record.user_id, record.path)

    async def delete_file(self, file_id: str, household_id: str) -> bool:  # type: ignore[override]
        record = self.get_record(file_id, household_id)
        if record is None:
            return False
        await self._graph.delete_file(record.user_id, record.path)
        self._conn.execute(
            "DELETE FROM files WHERE id=? AND household_id=?",
            (file_id, household_id),
        )
        self._conn.commit()
        return True

    def list_files(
        self,
        household_id: str,
        domain: str,
        user_id: str | None = None,
    ) -> list[FileRecord]:
        if user_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE household_id=? AND domain=? AND user_id=?"
                " AND storage='onedrive' ORDER BY uploaded_at DESC",
                (household_id, domain, user_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE household_id=? AND domain=?"
                " AND storage='onedrive' ORDER BY uploaded_at DESC",
                (household_id, domain),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_record(self, file_id: str, household_id: str) -> FileRecord | None:
        row = self._conn.execute(
            "SELECT * FROM files WHERE id=? AND household_id=?",
            (file_id, household_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def update_chunk_ids(
        self, file_id: str, household_id: str, chunk_ids: list[str]
    ) -> None:
        self._conn.execute(
            "UPDATE files SET chunk_ids=? WHERE id=? AND household_id=?",
            (json.dumps(chunk_ids), file_id, household_id),
        )
        self._conn.commit()

    def health(self) -> dict:
        try:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE storage='onedrive'"
            ).fetchone()[0]
            return {"status": "ok", "backend": "onedrive", "file_count": count}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _insert(self, record: FileRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO files
              (id, household_id, user_id, domain, filename, storage, path,
               size_bytes, mime_type, uploaded_at, metadata, chunk_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.household_id,
                record.user_id,
                record.domain,
                record.filename,
                record.storage,
                record.path,
                record.size_bytes,
                record.mime_type,
                record.uploaded_at,
                json.dumps(record.metadata),
                json.dumps(record.chunk_ids),
            ),
        )
        self._conn.commit()
