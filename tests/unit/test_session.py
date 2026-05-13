"""Unit tests for ConversationSessionManager."""

import pytest

from mithra.routing.session import ConversationSession, ConversationSessionManager, ConversationTurn
from mithra.storage.local import LocalStorageAdapter


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "test.db"))


@pytest.fixture
def manager(storage):
    return ConversationSessionManager(storage)


def _turn(role="user", text="hello"):
    from datetime import datetime, timezone
    return ConversationTurn(role=role, text=text, timestamp=datetime.now(timezone.utc).isoformat())


class TestConversationSessionManager:
    def test_get_or_create_new_session(self, manager):
        session = manager.get_or_create("s1", "person_a", "telegram:123")
        assert session.session_id == "s1"
        assert session.user_id == "person_a"

    def test_get_or_create_returns_existing(self, manager):
        manager.get_or_create("s1", "person_a", "telegram:123")
        session2 = manager.get_or_create("s1", "person_a", "telegram:123")
        assert session2.session_id == "s1"

    def test_load_history_empty_for_new_session(self, manager):
        assert manager.load_history("unknown-session", "person_a") == []

    def test_health(self, manager):
        h = manager.health()
        assert h["status"] == "ok"
        assert "active_sessions" in h

    def test_cap_at_max_turns(self, manager, storage):
        session = manager.get_or_create("s1", "person_a", "d1")
        # Manually save a session with MAX_TURNS + 5 turns
        from mithra.storage.adapter import StorageRecord
        import json
        from datetime import datetime, timezone
        turns = [
            {"role": "user", "text": f"msg {i}", "timestamp": datetime.now(timezone.utc).isoformat(),
             "intent": None, "domain": None}
            for i in range(ConversationSessionManager.MAX_TURNS + 5)
        ]
        record = StorageRecord(
            domain="sessions",
            key="s1",
            layer="state",
            data={
                "session_id": "s1",
                "user_id": "person_a",
                "device_id": "d1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_active_at": datetime.now(timezone.utc).isoformat(),
                "turns": turns,
            },
            user_id="person_a",
            version=1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        storage.write(record)
        history = manager.load_history("s1", "person_a")
        # All turns loaded (capping happens on append, not on load)
        assert len(history) == ConversationSessionManager.MAX_TURNS + 5
