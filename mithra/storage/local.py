"""
LocalStorageAdapter — SQLite + filesystem storage that never leaves the device.

Used for:
  - Health domain (Tier 1, hardcoded via StorageRouter — never overridable)
  - Local development and unit tests
  - SQLite cache layer backing OneDriveAdapter reads

Data lives inside a Docker volume mounted at base_path.
No network calls. Suitable for data that must stay local.

EncryptedStorageAdapter wraps this for health domain writes via StorageRouter.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mithra.storage.adapter import StorageAdapter, StorageRecord

_DDL = """
CREATE TABLE IF NOT EXISTS records (
    domain       TEXT NOT NULL,
    key          TEXT NOT NULL,
    layer        TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    data         TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'record',
    PRIMARY KEY (domain, key, layer, user_id)
);
CREATE INDEX IF NOT EXISTS idx_records_scope ON records (domain, layer, user_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> StorageRecord:
    return StorageRecord(
        domain=row["domain"],
        key=row["key"],
        layer=row["layer"],
        data=json.loads(row["data"]),
        user_id=row["user_id"],
        version=row["version"],
        updated_at=row["updated_at"],
        content_type=row["content_type"],
    )


class LocalStorageAdapter(StorageAdapter):
    """SQLite + filesystem storage adapter that never makes network calls."""

    def __init__(self, base_path: str, db_path: str) -> None:
        """
        Args:
            base_path: Root directory for binary file storage.
            db_path: Path to the SQLite database file.
        """
        self.base_path = base_path
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def read(self, domain: str, key: str, layer: str, user_id: str) -> StorageRecord | None:
        """Return a record by (domain, key, layer, user_id), or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM records WHERE domain=? AND key=? AND layer=? AND user_id=?",
            (domain, key, layer, user_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def write(self, record: StorageRecord) -> StorageRecord:
        """Upsert a record; increments version and stamps updated_at."""
        existing = self.read(record.domain, record.key, record.layer, record.user_id)
        new_version = (existing.version + 1) if existing else 1
        now = _now()
        self._conn.execute(
            """
            INSERT INTO records (domain, key, layer, user_id, data, version, updated_at, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, key, layer, user_id) DO UPDATE SET
                data=excluded.data,
                version=excluded.version,
                updated_at=excluded.updated_at,
                content_type=excluded.content_type
            """,
            (
                record.domain,
                record.key,
                record.layer,
                record.user_id,
                json.dumps(record.data),
                new_version,
                now,
                record.content_type,
            ),
        )
        self._conn.commit()
        record.version = new_version
        record.updated_at = now
        return record

    def list(self, domain: str, layer: str, user_id: str) -> list[StorageRecord]:
        """Return all records in a (domain, layer, user_id) scope."""
        rows = self._conn.execute(
            "SELECT * FROM records WHERE domain=? AND layer=? AND user_id=? ORDER BY updated_at DESC",
            (domain, layer, user_id),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def delete(self, domain: str, key: str, user_id: str) -> bool:
        """Delete a record across all layers. Return True if any row was deleted."""
        cur = self._conn.execute(
            "DELETE FROM records WHERE domain=? AND key=? AND user_id=?",
            (domain, key, user_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def write_file(
        self, domain: str, file_name: str, content: bytes, mime_type: str, user_id: str
    ) -> str:
        """Write bytes to base_path/<domain>/<user_id>/<file_name> and return the path."""
        dir_path = Path(self.base_path) / domain / user_id
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / file_name
        file_path.write_bytes(content)
        return str(file_path)

    def read_file(self, file_path: str) -> bytes:
        """Read bytes from a path returned by write_file."""
        return Path(file_path).read_bytes()

    def delete_file(self, file_path: str) -> bool:
        """Delete a file. Return True if it existed."""
        p = Path(file_path)
        if p.exists():
            p.unlink()
            return True
        return False

    def health(self) -> dict:
        """Return basic health info for the health dashboard."""
        try:
            count = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            return {"status": "ok", "backend": "sqlite", "record_count": count, "db_path": self.db_path}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
