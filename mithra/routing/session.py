"""
ConversationSessionManager — manages active conversation sessions.

Sessions are stored in StorageAdapter domain="sessions" layer="state" so
they survive restarts.

Rules:
  - Sessions expire after 10 minutes of inactivity (TTL_SECONDS = 600)
  - History is capped at 20 turns (oldest turns dropped when cap is reached)
  - Input adapters load the session before calling the agent
  - Output adapters save the turn after the agent returns
  - Agent is stateless — it never owns sessions directly

A session is keyed by session_id, which is set on the AgentRequest by the
input adapter before the agent call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from mithra.storage.adapter import StorageAdapter, StorageRecord


@dataclass
class ConversationTurn:
    """A single exchange in a conversation."""

    role: str
    text: str
    timestamp: str
    intent: str | None = None
    domain: str | None = None


@dataclass
class ConversationSession:
    """An active conversation session between a user and the agent."""

    session_id: str
    user_id: str
    device_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    created_at: str = ""
    last_active_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_to_dict(session: ConversationSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "device_id": session.device_id,
        "created_at": session.created_at,
        "last_active_at": session.last_active_at,
        "turns": [
            {
                "role": t.role,
                "text": t.text,
                "timestamp": t.timestamp,
                "intent": t.intent,
                "domain": t.domain,
            }
            for t in session.turns
        ],
    }


def _dict_to_session(d: dict[str, Any]) -> ConversationSession:
    return ConversationSession(
        session_id=d["session_id"],
        user_id=d["user_id"],
        device_id=d["device_id"],
        created_at=d.get("created_at", ""),
        last_active_at=d.get("last_active_at", ""),
        turns=[
            ConversationTurn(
                role=t["role"],
                text=t["text"],
                timestamp=t["timestamp"],
                intent=t.get("intent"),
                domain=t.get("domain"),
            )
            for t in d.get("turns", [])
        ],
    )


def _is_expired(last_active_at: str, ttl: int) -> bool:
    """Return True if last_active_at is older than ttl seconds."""
    try:
        ts = datetime.fromisoformat(last_active_at).timestamp()
        return (datetime.now(timezone.utc).timestamp() - ts) > ttl
    except (ValueError, TypeError):
        return True


class ConversationSessionManager:
    """Loads, saves, and expires conversation sessions from the storage layer."""

    MAX_TURNS = 20
    TTL_SECONDS = 600  # 10 minutes

    def __init__(
        self,
        storage_adapter: StorageAdapter,
        known_user_ids: list[str] | None = None,
        household_id: str = "",
    ) -> None:
        """
        Args:
            storage_adapter: StorageAdapter for session persistence.
            known_user_ids: User IDs to scan in expire_inactive(). Defaults to
                ["household"]. Pass household member IDs from household.yaml at startup.
            household_id: Household namespace from household.yaml.
        """
        self._storage = storage_adapter
        self._known_user_ids: list[str] = known_user_ids or ["household"]
        self._household_id = household_id

    # ------------------------------------------------------------------
    # get_or_create
    # ------------------------------------------------------------------

    def get_or_create(
        self, session_id: str, user_id: str, device_id: str
    ) -> ConversationSession:
        """Return an existing fresh session, or create a new one.

        If the stored session has been inactive longer than TTL_SECONDS it is
        deleted and a new session is created with the same session_id.
        Otherwise, last_active_at is refreshed and the session is returned.
        """
        record = self._storage.read("sessions", session_id, "state", user_id)
        if record is not None:
            session = _dict_to_session(record.data)
            if _is_expired(session.last_active_at, self.TTL_SECONDS):
                self._storage.delete("sessions", session_id, user_id)
                # fall through to create a fresh session
            else:
                session.last_active_at = _now()
                self._save(session)
                return session

        now = _now()
        session = ConversationSession(
            session_id=session_id,
            user_id=user_id,
            device_id=device_id,
            turns=[],
            created_at=now,
            last_active_at=now,
        )
        self._save(session)
        return session

    # ------------------------------------------------------------------
    # append_turn
    # ------------------------------------------------------------------

    def append_turn(
        self,
        session_id: str,
        turn: ConversationTurn,
        user_id: str = "household",
    ) -> ConversationSession:
        """Add a turn to the session and enforce the MAX_TURNS cap.

        user_id must match the user_id under which the session was created.
        Raises ValueError if the session is not found.
        """
        record = self._storage.read("sessions", session_id, "state", user_id)
        if record is None:
            raise ValueError(f"Session {session_id!r} not found for user {user_id!r}")
        session = _dict_to_session(record.data)
        session.turns.append(turn)
        if len(session.turns) > self.MAX_TURNS:
            session.turns = session.turns[-self.MAX_TURNS:]
        session.last_active_at = _now()
        self._save(session)
        return session

    # ------------------------------------------------------------------
    # load_history
    # ------------------------------------------------------------------

    def load_history(self, session_id: str, user_id: str) -> list[ConversationTurn]:
        """Return conversation turns for a session.

        Called by input adapters to populate AgentRequest.conversation_history.
        Returns [] if session not found.
        """
        record = self._storage.read("sessions", session_id, "state", user_id)
        if not record:
            return []
        return [
            ConversationTurn(
                role=t["role"],
                text=t["text"],
                timestamp=t["timestamp"],
                intent=t.get("intent"),
                domain=t.get("domain"),
            )
            for t in record.data.get("turns", [])
        ]

    # ------------------------------------------------------------------
    # expire_inactive
    # ------------------------------------------------------------------

    def expire_inactive(self, user_ids: list[str] | None = None) -> int:
        """Delete sessions past TTL across all known user IDs. Return count deleted.

        Args:
            user_ids: Explicit list to scan. If None, uses self._known_user_ids.
        """
        scan_ids = user_ids if user_ids is not None else self._known_user_ids
        deleted = 0
        for uid in scan_ids:
            records = self._storage.list("sessions", "state", uid)
            for record in records:
                last_active = record.data.get("last_active_at", "")
                if _is_expired(last_active, self.TTL_SECONDS):
                    self._storage.delete("sessions", record.key, uid)
                    deleted += 1
        return deleted

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Return session store health for the health dashboard."""
        try:
            total = sum(
                len(self._storage.list("sessions", "state", uid))
                for uid in self._known_user_ids
            )
            return {"status": "ok", "active_sessions": total}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _save(self, session: ConversationSession) -> None:
        self._storage.write(
            StorageRecord(
                domain="sessions",
                key=session.session_id,
                layer="state",
                data=_session_to_dict(session),
                user_id=session.user_id,
                version=1,
                updated_at=_now(),
                household_id=self._household_id,
            )
        )
