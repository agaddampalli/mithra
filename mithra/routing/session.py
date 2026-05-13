"""
ConversationSessionManager — manages active conversation sessions.

Sessions are stored in StorageAdapter domain="sessions" layer="state" so
they survive restarts.

Rules:
  - Sessions expire after 10 minutes of inactivity
  - History is capped at 20 turns (oldest turns dropped when cap is reached)
  - Input adapters load the session before calling the agent
  - Output adapters save the turn after the agent returns
  - Agent is stateless — it never owns sessions directly

A session is keyed by session_id, which is set on the AgentRequest by the
input adapter before the agent call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

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


def _session_to_dict(session: ConversationSession) -> dict:
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


def _dict_to_session(d: dict) -> ConversationSession:
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


class ConversationSessionManager:
    """Loads, saves, and expires conversation sessions from the storage layer."""

    MAX_TURNS = 20
    TTL_SECONDS = 600  # 10 minutes

    def __init__(self, storage_adapter: StorageAdapter) -> None:
        """
        Args:
            storage_adapter: A StorageAdapter instance used for session persistence.
        """
        self._storage = storage_adapter

    def _load(self, session_id: str, user_id: str) -> ConversationSession | None:
        record = self._storage.read("sessions", session_id, "state", user_id)
        if not record:
            return None
        return _dict_to_session(record.data)

    def _save(self, session: ConversationSession) -> None:
        record = StorageRecord(
            domain="sessions",
            key=session.session_id,
            layer="state",
            data=_session_to_dict(session),
            user_id=session.user_id,
            version=1,
            updated_at=_now(),
        )
        self._storage.write(record)

    def get_or_create(self, session_id: str, user_id: str, device_id: str) -> ConversationSession:
        """Return an existing session or create a new one."""
        session = self._load(session_id, user_id)
        if session:
            return session
        now = _now()
        session = ConversationSession(
            session_id=session_id,
            user_id=user_id,
            device_id=device_id,
            created_at=now,
            last_active_at=now,
        )
        self._save(session)
        return session

    def append_turn(self, session_id: str, turn: ConversationTurn) -> ConversationSession:
        """Add a turn to the session, evicting the oldest if the cap is reached."""
        session = self._load(session_id, turn.role and "household" or "household")
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        session.turns.append(turn)
        if len(session.turns) > self.MAX_TURNS:
            session.turns = session.turns[-self.MAX_TURNS:]
        session.last_active_at = _now()
        self._save(session)
        return session

    def load_history(self, session_id: str, user_id: str) -> list[ConversationTurn]:
        """Return the conversation turns for a session.
        Called by input adapters to populate AgentRequest.conversation_history.
        Returns empty list if session not found."""
        session = self._load(session_id, user_id)
        return session.turns if session else []

    def expire_inactive(self) -> int:
        """Delete sessions that have been inactive beyond TTL. Return count deleted."""
        deleted = 0
        cutoff = datetime.now(timezone.utc).timestamp() - self.TTL_SECONDS
        for user_id in ("person_a", "person_b", "household"):
            records = self._storage.list("sessions", "state", user_id)
            for record in records:
                last_active = record.data.get("last_active_at", "")
                try:
                    ts = datetime.fromisoformat(last_active).timestamp()
                    if ts < cutoff:
                        self._storage.delete("sessions", record.key, user_id)
                        deleted += 1
                except Exception:
                    pass
        return deleted

    def health(self) -> dict:
        """Return session store health for the health dashboard."""
        try:
            total = sum(
                len(self._storage.list("sessions", "state", uid))
                for uid in ("person_a", "person_b", "household")
            )
            return {"status": "ok", "active_sessions": total}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
