"""
ContextCache — local SQLite cache for ContextSnapshot objects with write-through
invalidation.

TTL: 30 seconds (safety net only).
Primary invalidation: StorageAdapter.write() calls cache.invalidate_domain()
immediately on every write. TTL is not the consistency mechanism.

Used by DomainMemoryRouter to avoid reassembling a full ContextSnapshot on
every agent call when nothing has changed in the relevant domains.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from mithra.routing.domain_router import ContextSnapshot, DomainBrief, DomainState

_DDL = """
CREATE TABLE IF NOT EXISTS context_cache (
    user_id    TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    snapshot   TEXT NOT NULL,
    domains    TEXT NOT NULL,
    cached_at  REAL NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
"""


def _snapshot_to_json(snapshot: ContextSnapshot) -> str:
    return json.dumps({
        "user_id": snapshot.user_id,
        "device_id": snapshot.device_id,
        "assembled_at": snapshot.assembled_at,
        "known_pii": snapshot.known_pii,
        "priority_domains": [
            {
                "domain": d.domain,
                "user_id": d.user_id,
                "facts": d.facts,
                "recent_actions": d.recent_actions,
                "pending_confirmations": d.pending_confirmations,
                "upcoming_deadlines": d.upcoming_deadlines,
                "preferences": d.preferences,
                "priority_score": d.priority_score,
            }
            for d in snapshot.priority_domains
        ],
        "domain_briefs": [
            {"domain": b.domain, "summary": b.summary, "priority_score": b.priority_score}
            for b in snapshot.domain_briefs
        ],
    })


def _json_to_snapshot(raw: str) -> ContextSnapshot:
    d = json.loads(raw)
    return ContextSnapshot(
        user_id=d["user_id"],
        device_id=d["device_id"],
        assembled_at=d["assembled_at"],
        known_pii=d.get("known_pii", {}),
        priority_domains=[
            DomainState(
                domain=pd["domain"],
                user_id=pd.get("user_id", ""),
                facts=pd.get("facts", {}),
                recent_actions=pd.get("recent_actions", []),
                pending_confirmations=pd.get("pending_confirmations", []),
                upcoming_deadlines=pd.get("upcoming_deadlines", []),
                preferences=pd.get("preferences", {}),
                priority_score=pd.get("priority_score", 0.0),
            )
            for pd in d.get("priority_domains", [])
        ],
        domain_briefs=[
            DomainBrief(
                domain=b["domain"],
                summary=b["summary"],
                priority_score=b["priority_score"],
            )
            for b in d.get("domain_briefs", [])
        ],
    )


def _domains_in_snapshot(snapshot: ContextSnapshot) -> list[str]:
    domains = [d.domain for d in snapshot.priority_domains]
    domains += [b.domain for b in snapshot.domain_briefs]
    return domains


class ContextCache:
    """Write-through SQLite cache for ContextSnapshot objects."""

    TTL_SECONDS = 30

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    def get(self, user_id: str, device_id: str) -> ContextSnapshot | None:
        """Return a cached snapshot if fresh, None if missing or expired."""
        row = self._conn.execute(
            "SELECT snapshot, cached_at FROM context_cache WHERE user_id=? AND device_id=?",
            (user_id, device_id),
        ).fetchone()
        if not row:
            return None
        if time.time() - row["cached_at"] > self.TTL_SECONDS:
            return None
        return _json_to_snapshot(row["snapshot"])

    def set(self, snapshot: ContextSnapshot) -> None:
        """Store a snapshot. Overwrites any existing entry for the same user + device."""
        domains = json.dumps(_domains_in_snapshot(snapshot))
        raw = _snapshot_to_json(snapshot)
        self._conn.execute(
            """
            INSERT INTO context_cache (user_id, device_id, snapshot, domains, cached_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, device_id) DO UPDATE SET
                snapshot=excluded.snapshot,
                domains=excluded.domains,
                cached_at=excluded.cached_at
            """,
            (snapshot.user_id, snapshot.device_id, raw, domains, time.time()),
        )
        self._conn.commit()

    def invalidate_domain(self, domain: str) -> int:
        """Invalidate all cached snapshots that include the given domain. Return count."""
        rows = self._conn.execute(
            "SELECT user_id, device_id, domains FROM context_cache"
        ).fetchall()
        to_delete = [
            (r["user_id"], r["device_id"])
            for r in rows
            if domain in json.loads(r["domains"])
        ]
        for uid, did in to_delete:
            self._conn.execute(
                "DELETE FROM context_cache WHERE user_id=? AND device_id=?", (uid, did)
            )
        self._conn.commit()
        return len(to_delete)

    def invalidate_user(self, user_id: str) -> int:
        """Invalidate all cached snapshots for a user. Return count."""
        cur = self._conn.execute(
            "DELETE FROM context_cache WHERE user_id=?", (user_id,)
        )
        self._conn.commit()
        return cur.rowcount
