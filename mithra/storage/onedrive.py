"""
OneDriveAdapter — Microsoft Graph API storage with local SQLite read-through cache
and a persistent write queue for resilience under intermittent connectivity.

Read path:   check SQLite cache (30s TTL) → Graph API on miss
Write path:  write to SQLite cache immediately → enqueue Graph API write
Write queue: persisted in SQLite, replayed on startup if items remain

Must never be used for the health domain — StorageRouter enforces this.

Inject graph_client for unit tests to avoid real network calls.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mithra.storage.adapter import StorageAdapter, StorageRecord

_CACHE_TTL_SECONDS = 30
_MAX_ATTEMPTS = 3
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_PLOS_ROOT = "PLOS"

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
    cached_at    REAL NOT NULL,
    PRIMARY KEY (domain, key, layer, user_id, household_id)
);
CREATE INDEX IF NOT EXISTS idx_records_scope ON records (domain, layer, user_id);

CREATE TABLE IF NOT EXISTS write_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    op           TEXT NOT NULL,
    domain       TEXT NOT NULL,
    key          TEXT NOT NULL,
    layer        TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}',
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'record',
    queued_at    TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op         TEXT NOT NULL,
    domain     TEXT NOT NULL,
    key        TEXT NOT NULL,
    layer      TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    data       TEXT NOT NULL,
    queued_at  TEXT NOT NULL,
    failed_at  TEXT NOT NULL,
    error      TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add household_id column and update PRIMARY KEY if missing (handles existing databases)."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()]
    if cols and "household_id" not in cols:
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
                cached_at    REAL NOT NULL,
                PRIMARY KEY (domain, key, layer, user_id, household_id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_scope ON records (domain, layer, user_id);
            INSERT INTO records (domain, key, layer, user_id, household_id, data, version, updated_at, content_type, cached_at)
                SELECT domain, key, layer, user_id, '', data, version, updated_at, content_type, cached_at
                FROM records_v1;
            DROP TABLE records_v1;
        """)
        conn.commit()


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


# ---------------------------------------------------------------------------
# Real Graph API client (used in production, not in unit tests)
# ---------------------------------------------------------------------------

class RealGraphClient:
    """Microsoft Graph API client using MSAL client-credentials flow."""

    def __init__(self, client_id: str, client_secret: str, tenant_id: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._token: str | None = None
        self._token_expires: float = 0.0

    def _get_token(self) -> str:
        import requests as _req
        now = time.time()
        if self._token and now < self._token_expires - 60:
            return self._token
        resp = _req.post(
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = now + body.get("expires_in", 3600)
        return self._token

    def _auth_headers(self, content_type: str = "application/json") -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": content_type,
        }

    def get_record(self, path: str) -> dict | None:
        import requests as _req
        resp = _req.get(f"{_GRAPH_BASE}{path}:/content", headers=self._auth_headers(), timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def put_record(self, path: str, data: dict) -> None:
        import requests as _req
        _req.put(
            f"{_GRAPH_BASE}{path}:/content",
            headers=self._auth_headers(),
            data=json.dumps(data).encode(),
            timeout=15,
        ).raise_for_status()

    def delete_record(self, path: str) -> None:
        import requests as _req
        resp = _req.delete(f"{_GRAPH_BASE}{path}", headers=self._auth_headers(), timeout=15)
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    def get_file(self, path: str) -> bytes:
        import requests as _req
        resp = _req.get(f"{_GRAPH_BASE}{path}:/content", headers=self._auth_headers(), timeout=30)
        resp.raise_for_status()
        return resp.content

    def put_file(self, path: str, content: bytes, mime_type: str) -> None:
        import requests as _req
        _req.put(
            f"{_GRAPH_BASE}{path}:/content",
            headers=self._auth_headers(content_type=mime_type),
            data=content,
            timeout=30,
        ).raise_for_status()

    def delete_file(self, path: str) -> bool:
        import requests as _req
        resp = _req.delete(f"{_GRAPH_BASE}{path}", headers=self._auth_headers(), timeout=15)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def list_records(self, domain: str, layer: str, user_id: str) -> list[dict]:
        """List all JSON record files in the folder for (domain, layer, user_id)."""
        import requests as _req
        folder = f"/me/drive/root:/{_PLOS_ROOT}/{domain}/{layer}/{user_id}"
        resp = _req.get(
            f"{_GRAPH_BASE}{folder}:/children",
            headers=self._auth_headers(),
            timeout=10,
        )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        results = []
        for item in resp.json().get("value", []):
            if not item.get("name", "").endswith(".json"):
                continue
            item_resp = _req.get(
                f"{_GRAPH_BASE}/me/drive/items/{item['id']}/content",
                headers=self._auth_headers(),
                timeout=10,
            )
            if item_resp.status_code == 200:
                try:
                    results.append(item_resp.json())
                except Exception:
                    pass
        return results

    def check_connectivity(self) -> bool:
        import requests as _req
        try:
            resp = _req.get(
                f"{_GRAPH_BASE}/me/drive",
                headers=self._auth_headers(),
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# OneDriveAdapter
# ---------------------------------------------------------------------------

class OneDriveAdapter(StorageAdapter):
    """Graph API adapter with local SQLite cache and persistent write queue."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        tenant_id: str = "",
        cache_db_path: str = ":memory:",
        graph_client: Any = None,
        cache: Any = None,
    ) -> None:
        """
        Args:
            client_id: Azure app client ID.
            client_secret: Azure app client secret.
            tenant_id: Azure tenant ID.
            cache_db_path: SQLite file path (':memory:' for tests).
            graph_client: Injectable graph client for unit tests. When provided,
                          the background worker thread is not started.
            cache: Optional ContextCache. If set, write() calls invalidate_domain().
        """
        self.cache_db_path = cache_db_path
        self._cache = cache

        if cache_db_path != ":memory:":
            Path(cache_db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(cache_db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()
        _migrate(self._conn)
        self._lock = threading.Lock()

        self._auth_valid: bool = False
        self._last_sync_at: str | None = None
        self._last_connectivity_check: float = 0.0
        self._cached_connectivity: bool = False

        if graph_client is not None:
            self._graph = graph_client
            self._auth_valid = True
            self._worker: threading.Thread | None = None
            self._stop: threading.Event | None = None
        else:
            self._graph = RealGraphClient(client_id, client_secret, tenant_id)
            self._stop = threading.Event()
            self._worker = threading.Thread(target=self._drain_loop, daemon=True)
            self._worker.start()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _record_path(self, domain: str, key: str, layer: str, user_id: str) -> str:
        return f"/me/drive/root:/{_PLOS_ROOT}/{domain}/{layer}/{user_id}/{key}.json"

    def _file_graph_path(self, domain: str, file_name: str) -> str:
        return f"/me/drive/root:/{_PLOS_ROOT}/files/{domain}/{file_name}"

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_get(
        self, domain: str, key: str, layer: str, user_id: str, household_id: str = ""
    ) -> StorageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE domain=? AND key=? AND layer=? AND user_id=? AND household_id=?",
                (domain, key, layer, user_id, household_id),
            ).fetchone()
        if row is None:
            return None
        if time.time() - row["cached_at"] > _CACHE_TTL_SECONDS:
            return None
        return _row_to_record(row)

    def _cache_put(self, record: StorageRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO records
                   (domain, key, layer, user_id, household_id, data, version, updated_at, content_type, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(domain, key, layer, user_id, household_id) DO UPDATE SET
                       data=excluded.data, version=excluded.version,
                       updated_at=excluded.updated_at, content_type=excluded.content_type,
                       cached_at=excluded.cached_at""",
                (
                    record.domain, record.key, record.layer, record.user_id, record.household_id,
                    json.dumps(record.data), record.version, record.updated_at,
                    record.content_type, time.time(),
                ),
            )
            self._conn.commit()

    def _cache_list(
        self, domain: str, layer: str, user_id: str, household_id: str = ""
    ) -> list[StorageRecord]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM records
                   WHERE domain=? AND layer=? AND user_id=? AND household_id=?
                     AND ? - cached_at <= ?
                   ORDER BY updated_at DESC""",
                (domain, layer, user_id, household_id, now, _CACHE_TTL_SECONDS),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def _cache_delete(self, domain: str, key: str, user_id: str, household_id: str = "") -> list[str]:
        """Delete all layers for (domain, key, user_id, household_id) from cache.
        Returns list of layer names that existed."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT layer FROM records WHERE domain=? AND key=? AND user_id=? AND household_id=?",
                (domain, key, user_id, household_id),
            ).fetchall()
            layers = [r["layer"] for r in rows]
            self._conn.execute(
                "DELETE FROM records WHERE domain=? AND key=? AND user_id=? AND household_id=?",
                (domain, key, user_id, household_id),
            )
            self._conn.commit()
        return layers

    # ------------------------------------------------------------------
    # Write queue helpers
    # ------------------------------------------------------------------

    def _enqueue_write(self, record: StorageRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO write_queue
                   (op, domain, key, layer, user_id, data, version, updated_at, content_type, queued_at)
                   VALUES ('write', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.domain, record.key, record.layer, record.user_id,
                    json.dumps(record.data), record.version, record.updated_at,
                    record.content_type, _now(),
                ),
                # household_id is not stored in write_queue (operational table)
            )
            self._conn.commit()

    def _enqueue_delete(
        self, domain: str, key: str, layer: str, user_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO write_queue
                   (op, domain, key, layer, user_id, queued_at)
                   VALUES ('delete', ?, ?, ?, ?, ?)""",
                (domain, key, layer, user_id, _now()),
            )
            self._conn.commit()

    def _execute_queue_item(self, row: sqlite3.Row) -> None:
        """Process one write_queue row. Increments attempts on failure; moves to dead_letter at max."""
        item_id = row["id"]
        op = row["op"]
        try:
            path = self._record_path(row["domain"], row["key"], row["layer"], row["user_id"])
            if op == "write":
                self._graph.put_record(
                    path,
                    {
                        "domain": row["domain"], "key": row["key"],
                        "layer": row["layer"], "user_id": row["user_id"],
                        "data": json.loads(row["data"]),
                        "version": row["version"], "updated_at": row["updated_at"],
                        "content_type": row["content_type"],
                    },
                )
            elif op == "delete":
                self._graph.delete_record(path)

            with self._lock:
                self._conn.execute("DELETE FROM write_queue WHERE id=?", (item_id,))
                self._conn.commit()
            self._last_sync_at = _now()

        except Exception as exc:
            attempts = row["attempts"] + 1
            with self._lock:
                if attempts >= _MAX_ATTEMPTS:
                    self._conn.execute(
                        """INSERT INTO dead_letter
                           (op, domain, key, layer, user_id, data, queued_at, failed_at, error)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            op, row["domain"], row["key"], row["layer"], row["user_id"],
                            row["data"], row["queued_at"], _now(), str(exc),
                        ),
                    )
                    self._conn.execute("DELETE FROM write_queue WHERE id=?", (item_id,))
                else:
                    self._conn.execute(
                        "UPDATE write_queue SET attempts=? WHERE id=?",
                        (attempts, item_id),
                    )
                self._conn.commit()

    def _process_pending(self) -> int:
        """Synchronously process all pending write_queue items. Returns count attempted.

        Called by the background worker and directly in unit tests.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM write_queue ORDER BY id"
            ).fetchall()
        for row in rows:
            self._execute_queue_item(row)
        return len(rows)

    def _drain_loop(self) -> None:
        """Background thread: drain write queue every 5 seconds."""
        while not self._stop.is_set():  # type: ignore[union-attr]
            try:
                self._process_pending()
            except Exception:
                pass
            self._stop.wait(timeout=5)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # StorageAdapter interface
    # ------------------------------------------------------------------

    def read(
        self, domain: str, key: str, layer: str, user_id: str, household_id: str = ""
    ) -> StorageRecord | None:
        """Cache hit: return immediately. Miss: fetch from Graph API, populate cache."""
        cached = self._cache_get(domain, key, layer, user_id, household_id)
        if cached:
            return cached
        try:
            path = self._record_path(domain, key, layer, user_id)
            payload = self._graph.get_record(path)
        except Exception:
            return None
        if payload is None:
            return None
        record = StorageRecord(
            domain=payload["domain"], key=payload["key"], layer=payload["layer"],
            data=payload["data"], user_id=payload["user_id"],
            version=payload.get("version", 1),
            updated_at=payload.get("updated_at", _now()),
            household_id=payload.get("household_id", household_id),
            content_type=payload.get("content_type", "record"),
        )
        self._cache_put(record)
        return record

    def write(self, record: StorageRecord) -> StorageRecord:
        """Write to cache immediately; enqueue Graph API write asynchronously."""
        existing = self._cache_get(
            record.domain, record.key, record.layer, record.user_id, record.household_id
        )
        record.version = (existing.version + 1) if existing else 1
        record.updated_at = _now()
        self._cache_put(record)
        if self._cache is not None:
            self._cache.invalidate_domain(record.domain)
        self._enqueue_write(record)
        return record

    def list(self, domain: str, layer: str, user_id: str, household_id: str = "") -> list[StorageRecord]:
        """Return cached records; fall back to Graph API when cache is cold."""
        cached = self._cache_list(domain, layer, user_id, household_id)
        if cached:
            return cached
        try:
            payloads = self._graph.list_records(domain, layer, user_id)
        except Exception:
            return []
        records = []
        for payload in payloads:
            record = StorageRecord(
                domain=payload["domain"], key=payload["key"], layer=payload["layer"],
                data=payload["data"], user_id=payload["user_id"],
                version=payload.get("version", 1),
                updated_at=payload.get("updated_at", _now()),
                household_id=payload.get("household_id", household_id),
                content_type=payload.get("content_type", "record"),
            )
            self._cache_put(record)
            records.append(record)
        return records

    def delete(self, domain: str, key: str, user_id: str, household_id: str = "") -> bool:
        """Remove from cache and enqueue Graph API deletes for all affected layers."""
        layers = self._cache_delete(domain, key, user_id, household_id)
        if not layers:
            return False
        for layer in layers:
            self._enqueue_delete(domain, key, layer, user_id)
        return True

    def write_file(
        self, domain: str, file_name: str, content: bytes, mime_type: str, user_id: str
    ) -> str:
        """Upload file to OneDrive; return the Graph path string."""
        path = self._file_graph_path(domain, file_name)
        self._graph.put_file(path, content, mime_type)
        return path

    def read_file(self, file_path: str) -> bytes:
        """Download file from OneDrive by its Graph path."""
        return self._graph.get_file(file_path)

    def delete_file(self, file_path: str) -> bool:
        """Delete file from OneDrive. Return True if it existed."""
        return self._graph.delete_file(file_path)

    def health(self) -> dict:
        """Return health status including auth validity, cache size, and queue depth."""
        now = time.time()
        if now - self._last_connectivity_check >= _CACHE_TTL_SECONDS:
            try:
                self._cached_connectivity = self._graph.check_connectivity()
            except Exception:
                self._cached_connectivity = False
            self._last_connectivity_check = now
        self._auth_valid = self._cached_connectivity

        with self._lock:
            cache_count = self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            queue_depth = self._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]

        return {
            "status": "ok" if self._auth_valid else "degraded",
            "cache_record_count": cache_count,
            "queue_depth": queue_depth,
            "last_sync_at": self._last_sync_at,
            "auth_valid": self._auth_valid,
        }
