"""
ContextCache — local SQLite cache for ContextSnapshot objects with write-through
invalidation.

TTL: 30 seconds (safety net only).
Primary invalidation: StorageAdapter.write() calls cache.invalidate_domain()
immediately on every write. TTL is NOT the consistency mechanism.

Snapshots are serialised with pickle (BLOB column) for zero-config round-trips
of the ContextSnapshot dataclass and its nested dataclasses.

Used by DomainMemoryRouter to avoid reassembling a full ContextSnapshot on
every agent call when nothing has changed in the relevant domains.
"""

from __future__ import annotations

import json
import pickle
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from mithra.routing.domain_router import ContextSnapshot

_DDL = """
CREATE TABLE IF NOT EXISTS context_cache (
    user_id    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    snapshot   BLOB NOT NULL,
    domains    TEXT NOT NULL,
    cached_at  REAL NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
"""


def _domain_list(snapshot: ContextSnapshot) -> list[str]:
    """Deduplicated ordered list of all domain names present in a snapshot."""
    seen: set[str] = set()
    result: list[str] = []
    for ds in snapshot.priority_domains:
        if ds.domain not in seen:
            seen.add(ds.domain)
            result.append(ds.domain)
    for db in snapshot.domain_briefs:
        if db.domain not in seen:
            seen.add(db.domain)
            result.append(db.domain)
    return result


class ContextCache:
    """Write-through SQLite cache for ContextSnapshot objects.

    TTL=30s is a safety net only. invalidate_domain() is the primary
    consistency mechanism — called by StorageAdapter.write() on every write
    so callers never see stale context for domains that just changed.
    """

    TTL_SECONDS = 30

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    def get(self, user_id: str, device_id: str) -> ContextSnapshot | None:
        """Return a cached snapshot if fresh; None if missing or expired.

        Expired rows are deleted on access (lazy expiry).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot, cached_at FROM context_cache"
                " WHERE user_id=? AND device_id=?",
                (user_id, device_id),
            ).fetchone()
            if not row:
                return None
            if time.time() - row["cached_at"] > self.TTL_SECONDS:
                self._conn.execute(
                    "DELETE FROM context_cache WHERE user_id=? AND device_id=?",
                    (user_id, device_id),
                )
                self._conn.commit()
                return None
            return pickle.loads(row["snapshot"])

    def set(self, snapshot: ContextSnapshot) -> None:
        """Store a snapshot. Overwrites any existing entry for (user_id, device_id)."""
        domains = json.dumps(_domain_list(snapshot))
        blob = pickle.dumps(snapshot)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO context_cache"
                " (user_id, device_id, snapshot, domains, cached_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (snapshot.user_id, snapshot.device_id, blob, domains, time.time()),
            )
            self._conn.commit()

    def invalidate_domain(self, domain: str) -> int:
        """Delete all snapshots that include domain. Return count deleted.

        This is the PRIMARY consistency mechanism. StorageAdapter.write() calls
        this immediately on every write — TTL is only a safety net.

        Uses LIKE '%"<domain>"%' so 'food' won't match 'fast_food'.
        """
        with self._lock:
            cur = self._conn.execute(
                'DELETE FROM context_cache WHERE domains LIKE ?',
                (f'%"{domain}"%',),
            )
            self._conn.commit()
        return cur.rowcount

    def invalidate_user(self, user_id: str) -> int:
        """Delete all snapshots for a user. Return count deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM context_cache WHERE user_id=?", (user_id,)
            )
            self._conn.commit()
        return cur.rowcount

    def health(self) -> dict[str, Any]:
        """Return cache reachability and cached entry count."""
        try:
            with self._lock:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM context_cache"
                ).fetchone()[0]
            return {"status": "ok", "cached_count": count, "db_path": self.db_path}
        except Exception as exc:
            return {"status": "error", "cached_count": 0, "db_path": self.db_path, "error": str(exc)}
