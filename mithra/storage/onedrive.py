"""
OneDriveAdapter — Microsoft Graph API storage with local SQLite read-through cache
and an async write queue for resilience under intermittent connectivity.

User data lives in the user's own OneDrive tenancy; Mithra holds indexes only.

Read path:   check SQLite cache (30s TTL) → Graph API on miss
Write path:  write to SQLite cache immediately → enqueue Graph API write
Write queue: survived across restarts; replayed on startup if pending

Must never be used for the health domain — StorageRouter enforces this.

Auth: OAuth 2.0 client-credentials flow.
Env vars required: ONEDRIVE_CLIENT_ID, ONEDRIVE_CLIENT_SECRET, ONEDRIVE_TENANT_ID
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

import requests

from mithra.storage.adapter import StorageAdapter, StorageRecord

_CACHE_TTL_SECONDS = 30

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS cache (
    domain       TEXT NOT NULL,
    key          TEXT NOT NULL,
    layer        TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    data         TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'record',
    cached_at    REAL NOT NULL,
    PRIMARY KEY (domain, key, layer, user_id)
);
CREATE INDEX IF NOT EXISTS idx_cache_scope ON cache (domain, layer, user_id);

CREATE TABLE IF NOT EXISTS write_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    op           TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL
);
"""

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


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


class OneDriveAdapter(StorageAdapter):
    """Microsoft Graph API adapter with local SQLite cache and async write queue."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        cache_db_path: str,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.cache_db_path = cache_db_path

        Path(cache_db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(cache_db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CACHE_DDL)
        self._conn.commit()

        self._token: str | None = None
        self._token_expires: float = 0.0
        self._lock = threading.Lock()

        self._queue: Queue = Queue()
        self._worker = threading.Thread(target=self._drain_queue, daemon=True)
        self._worker.start()
        self._replay_pending()

    # --- Auth ---

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires - 60:
            return self._token
        resp = requests.post(
            _TOKEN_URL.format(tenant_id=self.tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = now + payload.get("expires_in", 3600)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}

    # --- Cache helpers ---

    def _cache_get(self, domain: str, key: str, layer: str, user_id: str) -> StorageRecord | None:
        row = self._conn.execute(
            "SELECT * FROM cache WHERE domain=? AND key=? AND layer=? AND user_id=?",
            (domain, key, layer, user_id),
        ).fetchone()
        if not row:
            return None
        if time.time() - row["cached_at"] > _CACHE_TTL_SECONDS:
            return None
        return _row_to_record(row)

    def _cache_put(self, record: StorageRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO cache (domain, key, layer, user_id, data, version, updated_at, content_type, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(domain, key, layer, user_id) DO UPDATE SET
                data=excluded.data, version=excluded.version,
                updated_at=excluded.updated_at, content_type=excluded.content_type,
                cached_at=excluded.cached_at
            """,
            (
                record.domain, record.key, record.layer, record.user_id,
                json.dumps(record.data), record.version, record.updated_at,
                record.content_type, time.time(),
            ),
        )
        self._conn.commit()

    def _cache_list(self, domain: str, layer: str, user_id: str) -> list[StorageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM cache WHERE domain=? AND layer=? AND user_id=? AND ? - cached_at <= ?",
            (domain, layer, user_id, time.time(), _CACHE_TTL_SECONDS),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    # --- Graph API path helpers ---

    def _item_path(self, domain: str, key: str, layer: str, user_id: str) -> str:
        return f"/me/drive/root:/mithra/{user_id}/{domain}/{layer}/{key}.json"

    # --- Write queue ---

    def _enqueue(self, op: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO write_queue (op, payload, created_at) VALUES (?, ?, ?)",
            (op, json.dumps(payload), time.time()),
        )
        self._conn.commit()
        self._queue.put((op, payload))

    def _replay_pending(self) -> None:
        rows = self._conn.execute("SELECT op, payload FROM write_queue ORDER BY id").fetchall()
        for row in rows:
            self._queue.put((row["op"], json.loads(row["payload"])))

    def _drain_queue(self) -> None:
        while True:
            try:
                op, payload = self._queue.get(timeout=5)
                try:
                    if op == "write":
                        self._graph_write(payload)
                    elif op == "delete":
                        self._graph_delete(payload)
                    self._conn.execute(
                        "DELETE FROM write_queue WHERE op=? AND payload=?",
                        (op, json.dumps(payload)),
                    )
                    self._conn.commit()
                except Exception:
                    pass
            except Empty:
                continue

    def _graph_write(self, payload: dict) -> None:
        path = self._item_path(payload["domain"], payload["key"], payload["layer"], payload["user_id"])
        requests.put(
            f"{_GRAPH_BASE}{path}:/content",
            headers=self._headers(),
            data=json.dumps(payload).encode(),
            timeout=15,
        ).raise_for_status()

    def _graph_delete(self, payload: dict) -> None:
        path = self._item_path(payload["domain"], payload["key"], payload["layer"], payload["user_id"])
        resp = requests.delete(f"{_GRAPH_BASE}{path}", headers=self._headers(), timeout=15)
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    # --- StorageAdapter interface ---

    def read(self, domain: str, key: str, layer: str, user_id: str) -> StorageRecord | None:
        """Return from cache if fresh; fall through to Graph API on miss."""
        cached = self._cache_get(domain, key, layer, user_id)
        if cached:
            return cached
        try:
            path = self._item_path(domain, key, layer, user_id)
            resp = requests.get(f"{_GRAPH_BASE}{path}:/content", headers=self._headers(), timeout=10)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            payload = resp.json()
            record = StorageRecord(
                domain=payload["domain"],
                key=payload["key"],
                layer=payload["layer"],
                data=payload["data"],
                user_id=payload["user_id"],
                version=payload.get("version", 1),
                updated_at=payload.get("updated_at", _now()),
                content_type=payload.get("content_type", "record"),
            )
            self._cache_put(record)
            return record
        except Exception:
            return None

    def write(self, record: StorageRecord) -> StorageRecord:
        """Write to cache immediately; enqueue Graph API write for resilience."""
        existing = self._cache_get(record.domain, record.key, record.layer, record.user_id)
        record.version = (existing.version + 1) if existing else 1
        record.updated_at = _now()
        self._cache_put(record)
        self._enqueue("write", {
            "domain": record.domain, "key": record.key, "layer": record.layer,
            "user_id": record.user_id, "data": record.data, "version": record.version,
            "updated_at": record.updated_at, "content_type": record.content_type,
        })
        return record

    def list(self, domain: str, layer: str, user_id: str) -> list[StorageRecord]:
        """Return cached records for this scope; Graph enumeration not yet implemented."""
        return self._cache_list(domain, layer, user_id)

    def delete(self, domain: str, key: str, user_id: str) -> bool:
        """Remove from cache and enqueue Graph delete."""
        cur = self._conn.execute(
            "DELETE FROM cache WHERE domain=? AND key=? AND user_id=?",
            (domain, key, user_id),
        )
        self._conn.commit()
        if cur.rowcount > 0:
            for layer in ("facts", "state", "history", "preferences"):
                self._enqueue("delete", {"domain": domain, "key": key, "layer": layer, "user_id": user_id})
            return True
        return False

    def write_file(
        self, domain: str, file_name: str, content: bytes, mime_type: str, user_id: str
    ) -> str:
        """Upload a file to OneDrive and return its drive path."""
        path = f"/me/drive/root:/mithra/{user_id}/{domain}/files/{file_name}:/content"
        resp = requests.put(
            f"{_GRAPH_BASE}{path}",
            headers={**self._headers(), "Content-Type": mime_type},
            data=content,
            timeout=30,
        )
        resp.raise_for_status()
        return f"mithra/{user_id}/{domain}/files/{file_name}"

    def read_file(self, file_path: str) -> bytes:
        """Download a file from OneDrive by its drive path."""
        path = f"/me/drive/root:/{file_path}:/content"
        resp = requests.get(f"{_GRAPH_BASE}{path}", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.content

    def delete_file(self, file_path: str) -> bool:
        """Delete a file from OneDrive. Return True if it existed."""
        path = f"/me/drive/root:/{file_path}"
        resp = requests.delete(f"{_GRAPH_BASE}{path}", headers=self._headers(), timeout=15)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def health(self) -> dict:
        """Return health status including Graph API reachability."""
        try:
            resp = requests.get(f"{_GRAPH_BASE}/me/drive", headers=self._headers(), timeout=5)
            graph_ok = resp.status_code == 200
        except Exception as exc:
            return {"status": "degraded", "graph_api": False, "error": str(exc)}
        queue_depth = self._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        return {
            "status": "ok" if graph_ok else "degraded",
            "graph_api": graph_ok,
            "write_queue_depth": queue_depth,
            "cache_db": self.cache_db_path,
        }
