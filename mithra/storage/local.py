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
    household_id TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'record',
    PRIMARY KEY (domain, key, layer, user_id, household_id)
);
CREATE INDEX IF NOT EXISTS idx_records_scope ON records (domain, layer, user_id);
"""

_IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS households (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'UTC',
    locale       TEXT NOT NULL DEFAULT 'en-US',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'member',
    status       TEXT NOT NULL DEFAULT 'active',
    invited_by   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_channels (
    user_id      TEXT NOT NULL,
    household_id TEXT NOT NULL,
    channel      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, channel)
);

CREATE TABLE IF NOT EXISTS member_preferences (
    user_id      TEXT NOT NULL,
    household_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS household_config (
    household_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (household_id, key)
);
"""


_CALENDAR_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS calendar_events (
    event_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    household_id TEXT NOT NULL,
    summary      TEXT,
    start_time   TEXT,
    end_time     TEXT,
    is_recurring INTEGER DEFAULT 0,
    source       TEXT DEFAULT 'mithra',
    scope        TEXT DEFAULT 'personal',
    calendar_id  TEXT DEFAULT 'primary',
    raw_json     TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_events_household
    ON calendar_events(household_id, start_time);
CREATE INDEX IF NOT EXISTS idx_cal_events_user
    ON calendar_events(user_id, start_time);
CREATE INDEX IF NOT EXISTS idx_cal_events_scope
    ON calendar_events(household_id, scope, start_time);
"""

_CONVERSATION_STATE_DDL = """
CREATE TABLE IF NOT EXISTS conversation_state (
    user_id      TEXT PRIMARY KEY,
    last_task_id TEXT,
    capture_step TEXT,
    context_json TEXT,
    updated_at   TEXT NOT NULL
);
"""

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    notes        TEXT,
    scope        TEXT DEFAULT 'personal',
    priority     TEXT DEFAULT 'normal',
    status       TEXT DEFAULT 'inbox',
    due_date     TEXT,
    project_id   TEXT,
    milestone_id TEXT,
    snoozed_until TEXT,
    source       TEXT DEFAULT 'manual',
    source_ref   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_household
    ON tasks(household_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_user
    ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_due
    ON tasks(household_id, due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_priority
    ON tasks(household_id, priority, status);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT,
    scope        TEXT DEFAULT 'personal',
    status       TEXT DEFAULT 'active',
    target_date  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    household_id TEXT NOT NULL,
    title        TEXT NOT NULL,
    order_index  INTEGER NOT NULL,
    status       TEXT DEFAULT 'pending',
    target_date  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routines (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    title        TEXT NOT NULL,
    scope        TEXT DEFAULT 'household',
    reset_time   TEXT DEFAULT '00:00',
    active       INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routine_completions (
    id              TEXT PRIMARY KEY,
    routine_id      TEXT NOT NULL,
    household_id    TEXT NOT NULL,
    completed_by    TEXT NOT NULL,
    completion_date TEXT NOT NULL,
    completed_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_routine_day
    ON routine_completions(routine_id, completion_date);
"""

_FILES_DDL = """
CREATE TABLE IF NOT EXISTS files (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    domain       TEXT NOT NULL,
    filename     TEXT NOT NULL,
    storage      TEXT NOT NULL,
    path         TEXT NOT NULL,
    size_bytes   INTEGER,
    mime_type    TEXT,
    uploaded_at  TEXT NOT NULL,
    metadata     TEXT,
    chunk_ids    TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_household
    ON files(household_id);
CREATE INDEX IF NOT EXISTS idx_files_domain
    ON files(domain, household_id);
CREATE INDEX IF NOT EXISTS idx_files_user
    ON files(user_id, household_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add household_id column and update PRIMARY KEY if missing (handles existing databases)."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()]
    if "household_id" not in cols:
        conn.executescript("""
            ALTER TABLE records RENAME TO records_v1;
            CREATE TABLE records (
                domain       TEXT NOT NULL,
                key          TEXT NOT NULL,
                layer        TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                household_id TEXT NOT NULL DEFAULT '',
                data         TEXT NOT NULL,
                version      INTEGER NOT NULL DEFAULT 1,
                updated_at   TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'record',
                PRIMARY KEY (domain, key, layer, user_id, household_id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_scope ON records (domain, layer, user_id);
            INSERT INTO records (domain, key, layer, user_id, household_id, data, version, updated_at, content_type)
                SELECT domain, key, layer, user_id, '', data, version, updated_at, content_type
                FROM records_v1;
            DROP TABLE records_v1;
        """)
        conn.commit()


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
        household_id=row["household_id"],
        content_type=row["content_type"],
    )


class LocalStorageAdapter(StorageAdapter):
    """SQLite + filesystem storage adapter that never makes network calls."""

    def __init__(self, base_path: str, db_path: str, cache=None) -> None:
        """
        Args:
            base_path: Root directory for binary file storage.
            db_path: Path to the SQLite database file.
            cache: Optional ContextCache. If set, write() calls invalidate_domain().
        """
        self.base_path = base_path
        self.db_path = db_path
        self._cache = cache
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()
        _migrate(self._conn)
        self._conn.executescript(_IDENTITY_DDL)
        self._conn.commit()
        self._conn.executescript(_FILES_DDL)
        self._conn.commit()
        self._conn.executescript(_CALENDAR_EVENTS_DDL)
        self._conn.commit()
        self._conn.executescript(_CONVERSATION_STATE_DDL)
        self._conn.commit()
        self._conn.executescript(_TASKS_DDL)
        self._conn.commit()
        # Additive migrations for existing installs
        for _col, _type in [("managed_by", "TEXT"), ("date_of_birth", "TEXT")]:
            try:
                self._conn.execute(
                    f"ALTER TABLE members ADD COLUMN {_col} {_type}"
                )
                self._conn.commit()
            except Exception:
                pass  # Column already exists

    def read(
        self, domain: str, key: str, layer: str, user_id: str, household_id: str = ""
    ) -> StorageRecord | None:
        """Return a record by (domain, key, layer, user_id, household_id), or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM records WHERE domain=? AND key=? AND layer=? AND user_id=? AND household_id=?",
            (domain, key, layer, user_id, household_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def write(self, record: StorageRecord) -> StorageRecord:
        """Upsert a record; increments version and stamps updated_at."""
        existing = self.read(
            record.domain, record.key, record.layer, record.user_id, record.household_id
        )
        new_version = (existing.version + 1) if existing else 1
        now = _now()
        self._conn.execute(
            """
            INSERT INTO records (domain, key, layer, user_id, household_id, data, version, updated_at, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, key, layer, user_id, household_id) DO UPDATE SET
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
                record.household_id,
                json.dumps(record.data),
                new_version,
                now,
                record.content_type,
            ),
        )
        self._conn.commit()
        if self._cache is not None:
            self._cache.invalidate_domain(record.domain)
        record.version = new_version
        record.updated_at = now
        return record

    def list(self, domain: str, layer: str, user_id: str, household_id: str = "") -> list[StorageRecord]:
        """Return all records in a (domain, layer, user_id, household_id) scope."""
        rows = self._conn.execute(
            "SELECT * FROM records WHERE domain=? AND layer=? AND user_id=? AND household_id=? ORDER BY updated_at DESC",
            (domain, layer, user_id, household_id),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def delete(self, domain: str, key: str, user_id: str, household_id: str = "") -> bool:
        """Delete a record across all layers. Return True if any row was deleted."""
        cur = self._conn.execute(
            "DELETE FROM records WHERE domain=? AND key=? AND user_id=? AND household_id=?",
            (domain, key, user_id, household_id),
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
            record_count = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            file_count = sum(1 for _ in Path(self.base_path).rglob("*") if _.is_file()) if Path(self.base_path).exists() else 0
            return {
                "status": "ok",
                "backend": "sqlite",
                "db_path": self.db_path,
                "record_count": record_count,
                "file_count": file_count,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
