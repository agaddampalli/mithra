"""
StructuredStore — SQLite wrapper for facts, state, action history, and preferences.

Four logical tables in one SQLite database:
  - facts       keyed facts per domain (e.g. "person_a.age = 32")
  - state       current state snapshots per domain
  - history     time-ordered action log (append-only reads)
  - prefs       user and household preferences

All writes are idempotent by (domain, key). History is append-only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    domain     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, key)
);

CREATE TABLE IF NOT EXISTS state (
    domain     TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, key)
);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    domain     TEXT NOT NULL,
    entry      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_domain ON history (domain, created_at DESC);

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
    """SQLite-backed store for facts, state, history, and preferences."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    async def set_fact(self, domain: str, key: str, value: Any) -> None:
        """Upsert a fact."""
        self._conn.execute(
            "INSERT INTO facts (domain, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(domain, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (domain, key, json.dumps(value), _now()),
        )
        self._conn.commit()

    async def get_fact(self, domain: str, key: str) -> Any:
        """Read a fact by domain and key."""
        row = self._conn.execute(
            "SELECT value FROM facts WHERE domain=? AND key=?", (domain, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    async def set_state(self, domain: str, key: str, value: Any) -> None:
        """Upsert a state snapshot."""
        self._conn.execute(
            "INSERT INTO state (domain, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(domain, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (domain, key, json.dumps(value), _now()),
        )
        self._conn.commit()

    async def get_state(self, domain: str, key: str) -> Any:
        """Read a state snapshot."""
        row = self._conn.execute(
            "SELECT value FROM state WHERE domain=? AND key=?", (domain, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None

    async def append_history(self, domain: str, entry: dict[str, Any]) -> None:
        """Append an entry to the domain's action history (write-only)."""
        self._conn.execute(
            "INSERT INTO history (domain, entry, created_at) VALUES (?, ?, ?)",
            (domain, json.dumps(entry), _now()),
        )
        self._conn.commit()

    async def get_history(self, domain: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent history entries for a domain, newest first."""
        rows = self._conn.execute(
            "SELECT entry FROM history WHERE domain=? ORDER BY created_at DESC LIMIT ?",
            (domain, limit),
        ).fetchall()
        return [json.loads(r["entry"]) for r in rows]

    async def set_pref(self, user_id: str, key: str, value: Any) -> None:
        """Upsert a user or household preference."""
        self._conn.execute(
            "INSERT INTO prefs (user_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (user_id, key, json.dumps(value), _now()),
        )
        self._conn.commit()

    async def get_pref(self, user_id: str, key: str) -> Any:
        """Read a preference by user_id and key."""
        row = self._conn.execute(
            "SELECT value FROM prefs WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else None
