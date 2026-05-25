"""
MemberRegistry — single source of truth for household members.

Reads from the normalized identity tables in SQLite:
  households, members, member_channels, member_preferences

Replaces UserIdentityResolver entirely.
All channel resolution (Telegram, WhatsApp, device) goes through here.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Member:
    id: str
    household_id: str
    display_name: str
    role: str                      # "owner" | "admin" | "member"
    status: str                    # "active" | "invited" | "suspended"
    invited_by: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class MemberRegistry:
    """Single source of truth for household members.

    Reads from dedicated member tables in SQLite.
    Replaces UserIdentityResolver entirely.
    """

    def __init__(self, db_path: str, household_id: str) -> None:
        self._db_path = db_path
        self._household_id = household_id

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Member lookups
    # ------------------------------------------------------------------

    def get_member(self, user_id: str) -> Optional[Member]:
        """Return a member by user_id, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE id=? AND household_id=?",
                (user_id, self._household_id),
            ).fetchone()
        return self._row_to_member(row) if row else None

    def get_members(self) -> list[Member]:
        """Return all active members in this household."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM members WHERE household_id=? AND status=?",
                (self._household_id, "active"),
            ).fetchall()
        return [self._row_to_member(r) for r in rows]

    def get_admin(self) -> Optional[Member]:
        """Return the owner, or first admin if no owner exists."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE household_id=? AND role='owner' LIMIT 1",
                (self._household_id,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM members WHERE household_id=? AND role='admin' LIMIT 1",
                    (self._household_id,),
                ).fetchone()
        return self._row_to_member(row) if row else None

    def is_admin(self, user_id: str) -> bool:
        """True if user has owner or admin role."""
        member = self.get_member(user_id)
        return member is not None and member.role in ("owner", "admin")

    def add_member(
        self,
        display_name: str,
        role: str = "member",
        invited_by: Optional[str] = None,
    ) -> Member:
        """Create a new member with a generated ID. Status starts as 'invited'."""
        now = _now()
        new_id = "usr_" + secrets.token_hex(4)
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO members
                   (id, household_id, display_name, role, status,
                    invited_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (new_id, self._household_id, display_name, role,
                 "invited", invited_by, now, now),
            )
        return Member(
            id=new_id,
            household_id=self._household_id,
            display_name=display_name,
            role=role,
            status="invited",
            invited_by=invited_by,
            created_at=now,
            updated_at=now,
        )

    def activate_member(self, user_id: str) -> None:
        """Set member status to active after approval."""
        now = _now()
        with self._conn() as conn:
            conn.execute(
                "UPDATE members SET status='active', updated_at=? WHERE id=?",
                (now, user_id),
            )

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    def resolve_channel(
        self, channel: str, channel_id: str
    ) -> Optional[Member]:
        """Find an active member by channel identity (e.g. telegram chat_id)."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT m.* FROM members m
                   JOIN member_channels mc ON mc.user_id = m.id
                   WHERE mc.channel=? AND mc.channel_id=?
                   AND m.household_id=? AND m.status='active'""",
                (channel, str(channel_id), self._household_id),
            ).fetchone()
        return self._row_to_member(row) if row else None

    def register_channel(
        self, user_id: str, channel: str, channel_id: str
    ) -> None:
        """Link a channel identity to a member. Removes any prior active mapping for this channel_id."""
        now = _now()
        with self._conn() as conn:
            # Remove existing active (verified=1) mappings for this channel_id so it
            # doesn't remain linked to the previous member.
            conn.execute(
                "DELETE FROM member_channels WHERE channel=? AND channel_id=? AND verified=1",
                (channel, str(channel_id)),
            )
            conn.execute(
                """INSERT OR REPLACE INTO member_channels
                   (user_id, household_id, channel, channel_id, verified, created_at)
                   VALUES (?,?,?,?,1,?)""",
                (user_id, self._household_id, channel, str(channel_id), now),
            )

    def get_channel_id(self, user_id: str, channel: str) -> Optional[str]:
        """Return the channel_id for a user on a given channel, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT channel_id FROM member_channels WHERE user_id=? AND channel=?",
                (user_id, channel),
            ).fetchone()
        return row["channel_id"] if row else None

    # ------------------------------------------------------------------
    # Block / authorization
    # ------------------------------------------------------------------

    def block_channel(self, channel: str, channel_id: str) -> None:
        """Permanently block a channel identity.

        Uses a unique pseudo-user_id per (channel, channel_id) pair so multiple
        channel_ids can be blocked on the same channel without PK collisions.
        Also removes any active member mapping for this channel_id.
        """
        block_uid = f"__blocked__{channel_id}"
        now = _now()
        with self._conn() as conn:
            # Remove active member mapping so the channel_id is no longer authorized
            conn.execute(
                "DELETE FROM member_channels WHERE channel=? AND channel_id=? AND verified=1",
                (channel, str(channel_id)),
            )
            conn.execute(
                """INSERT OR REPLACE INTO member_channels
                   (user_id, household_id, channel, channel_id, verified, created_at)
                   VALUES (?,?,?,?,0,?)""",
                (block_uid, self._household_id, channel, str(channel_id), now),
            )

    def is_blocked(self, channel: str, channel_id: str) -> bool:
        """True if this channel_id is permanently blocked."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM member_channels WHERE channel=? AND channel_id=? AND verified=0",
                (channel, str(channel_id)),
            ).fetchone()
        return row is not None

    def is_authorized(self, channel: str, channel_id: str) -> bool:
        """True if channel_id belongs to a known active member and is not blocked."""
        if self.is_blocked(channel, str(channel_id)):
            return False
        member = self.resolve_channel(channel, str(channel_id))
        return member is not None and member.status == "active"

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def set_preference(self, user_id: str, key: str, value: str) -> None:
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO member_preferences
                   (user_id, household_id, key, value, updated_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, self._household_id, key, value, now),
            )

    def get_preference(
        self, user_id: str, key: str, default: str = ""
    ) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM member_preferences WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
        return row["value"] if row else default

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> Member:
        return Member(
            id=row["id"],
            household_id=row["household_id"],
            display_name=row["display_name"],
            role=row["role"],
            status=row["status"],
            invited_by=row["invited_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
