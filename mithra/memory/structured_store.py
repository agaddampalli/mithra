"""
StructuredStore — SQLite wrapper for facts, state, action history, and preferences.

Four logical tables in one SQLite database:
  - facts       keyed facts per (domain, key, user_id). Shared domains use
                user_id='household'; personal domains use 'person_a' / 'person_b'.
  - state       current state snapshots, same (domain, key, user_id) scope.
  - history     time-ordered action log (append-only). Filtered by user_id.
  - prefs       user and household preferences keyed by (user_id, key).

Default user_id='household' on all facts/state/history methods so callers that
don't need per-user scoping work without changes.

All writes are idempotent by primary key. History is append-only (no delete).
Thread-safe: all DB access serialised through self._lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    domain     TEXT NOT NULL,
    key        TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT 'household',
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, key, user_id)
);

CREATE TABLE IF NOT EXISTS state (
    domain     TEXT NOT NULL,
    key        TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT 'household',
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, key, user_id)
);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    domain     TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT 'household',
    entry      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_domain
    ON history (domain, user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS prefs (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StructuredStore:
    """SQLite-backed store for facts, state, history, and preferences.

    user_id defaults to 'household' on facts/state/history methods — shared
    domains work without specifying it; personal domains pass 'person_a' etc.
    """

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

    # ------------------------------------------------------------------
    # Facts
    # ------------------------------------------------------------------

    async def set_fact(
        self, domain: str, key: str, value: Any, user_id: str = "household"
    ) -> None:
        """Upsert a fact. Same (domain, key, user_id) overwrites; different user_id is a separate row."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts (domain, key, user_id, value, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(domain, key, user_id)"
                " DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (domain, key, user_id, json.dumps(value), _now()),
            )
            self._conn.commit()

    async def get_fact(
        self, domain: str, key: str, user_id: str = "household"
    ) -> Any:
        """Return a fact by (domain, key, user_id), or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM facts WHERE domain=? AND key=? AND user_id=?",
                (domain, key, user_id),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    async def list_facts(
        self, domain: str, user_id: str = "household"
    ) -> dict[str, Any]:
        """Return all facts for (domain, user_id) as {key: value}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM facts WHERE domain=? AND user_id=?",
                (domain, user_id),
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    async def delete_fact(
        self, domain: str, key: str, user_id: str = "household"
    ) -> bool:
        """Delete a fact. Return True if deleted, False if not found."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM facts WHERE domain=? AND key=? AND user_id=?",
                (domain, key, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    async def set_state(
        self, domain: str, key: str, value: Any, user_id: str = "household"
    ) -> None:
        """Upsert a state snapshot. Same (domain, key, user_id) overwrites."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (domain, key, user_id, value, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(domain, key, user_id)"
                " DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (domain, key, user_id, json.dumps(value), _now()),
            )
            self._conn.commit()

    async def get_state(
        self, domain: str, key: str, user_id: str = "household"
    ) -> Any:
        """Return a state snapshot by (domain, key, user_id), or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE domain=? AND key=? AND user_id=?",
                (domain, key, user_id),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    async def list_state(
        self, domain: str, user_id: str = "household"
    ) -> dict[str, Any]:
        """Return all state entries for (domain, user_id) as {key: value}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM state WHERE domain=? AND user_id=?",
                (domain, user_id),
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    async def delete_state(
        self, domain: str, key: str, user_id: str = "household"
    ) -> bool:
        """Delete a state entry. Return True if deleted, False if not found."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM state WHERE domain=? AND key=? AND user_id=?",
                (domain, key, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # History  (append-only — no delete)
    # ------------------------------------------------------------------

    async def append_history(
        self, domain: str, entry: dict[str, Any], user_id: str = "household"
    ) -> None:
        """Append an entry to the domain's action history."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (domain, user_id, entry, created_at) VALUES (?, ?, ?, ?)",
                (domain, user_id, json.dumps(entry), _now()),
            )
            self._conn.commit()

    async def get_history(
        self, domain: str, limit: int = 100, user_id: str = "household"
    ) -> list[dict[str, Any]]:
        """Return recent history entries for (domain, user_id), newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry FROM history"
                " WHERE domain=? AND user_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (domain, user_id, limit),
            ).fetchall()
        return [json.loads(r["entry"]) for r in rows]

    # ------------------------------------------------------------------
    # Preferences  (user_id is already the primary scope key)
    # ------------------------------------------------------------------

    async def set_pref(self, user_id: str, key: str, value: Any) -> None:
        """Upsert a user or household preference."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO prefs (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(user_id, key)"
                " DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (user_id, key, json.dumps(value), _now()),
            )
            self._conn.commit()

    async def get_pref(self, user_id: str, key: str) -> Any:
        """Return a preference by user_id and key, or None if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM prefs WHERE user_id=? AND key=?", (user_id, key)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    async def list_prefs(self, user_id: str) -> dict[str, Any]:
        """Return all preferences for a user_id as {key: value}."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM prefs WHERE user_id=?", (user_id,)
            ).fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Return store reachability and total row counts across all user_ids."""
        try:
            with self._lock:
                fact_count = self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                state_count = self._conn.execute("SELECT COUNT(*) FROM state").fetchone()[0]
                history_count = self._conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                pref_count = self._conn.execute("SELECT COUNT(*) FROM prefs").fetchone()[0]
            return {
                "status": "ok",
                "db_path": self.db_path,
                "fact_count": fact_count,
                "state_count": state_count,
                "history_count": history_count,
                "pref_count": pref_count,
                "error": None,
            }
        except Exception as exc:
            return {
                "status": "error",
                "db_path": self.db_path,
                "fact_count": 0,
                "state_count": 0,
                "history_count": 0,
                "pref_count": 0,
                "error": str(exc),
            }
