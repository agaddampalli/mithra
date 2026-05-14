"""Tests for ConversationSessionManager concrete implementation.

The original 5 scaffold tests live in tests/unit/test_session.py and must
continue to pass unchanged. This file adds tests for all method behaviours
specified in P0-13.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from mithra.routing.session import (
    ConversationSession,
    ConversationSessionManager,
    ConversationTurn,
)
from mithra.storage.adapter import StorageRecord
from mithra.storage.local import LocalStorageAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))


@pytest.fixture
def manager(storage):
    return ConversationSessionManager(storage, known_user_ids=["person_a", "person_b", "household"])


def _turn(role: str = "user", text: str = "hello") -> ConversationTurn:
    return ConversationTurn(role=role, text=text, timestamp=datetime.now(timezone.utc).isoformat())


def _old_session_record(session_id: str, user_id: str, seconds_ago: int = 700) -> StorageRecord:
    """Return a StorageRecord whose last_active_at is seconds_ago in the past."""
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    return StorageRecord(
        domain="sessions",
        key=session_id,
        layer="state",
        data={
            "session_id": session_id,
            "user_id": user_id,
            "device_id": "d1",
            "created_at": old_ts,
            "last_active_at": old_ts,
            "turns": [],
        },
        user_id=user_id,
        version=1,
        updated_at=old_ts,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_turns_is_20(self):
        assert ConversationSessionManager.MAX_TURNS == 20

    def test_ttl_seconds_is_600(self):
        assert ConversationSessionManager.TTL_SECONDS == 600


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------


class TestGetOrCreate:
    def test_creates_new_session_when_not_found(self, manager):
        session = manager.get_or_create("s_new", "person_a", "phone")
        assert isinstance(session, ConversationSession)
        assert session.session_id == "s_new"
        assert session.user_id == "person_a"
        assert session.device_id == "phone"
        assert session.created_at != ""
        assert session.last_active_at != ""

    def test_returns_existing_fresh_session(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        session2 = manager.get_or_create("s1", "person_a", "d1")
        assert session2.session_id == "s1"

    def test_creates_new_when_existing_is_expired(self, storage):
        storage.write(_old_session_record("s1", "person_a", seconds_ago=700))
        manager = ConversationSessionManager(storage)
        session = manager.get_or_create("s1", "person_a", "d1")
        # Fresh session — last_active_at should be within the last few seconds
        ts = datetime.fromisoformat(session.last_active_at).timestamp()
        assert (datetime.now(timezone.utc).timestamp() - ts) < 5

    def test_expired_session_is_deleted_before_creating_new(self, storage):
        storage.write(_old_session_record("s1", "person_a", seconds_ago=700))
        manager = ConversationSessionManager(storage)
        manager.get_or_create("s1", "person_a", "d1")
        # Only one record should exist — the fresh replacement
        records = storage.list("sessions", "state", "person_a")
        assert len(records) == 1
        # The fresh record's last_active_at should be recent (within 5 seconds)
        ts = datetime.fromisoformat(records[0].data["last_active_at"]).timestamp()
        assert (datetime.now(timezone.utc).timestamp() - ts) < 5

    def test_updates_last_active_at_on_fresh_access(self, storage):
        manager = ConversationSessionManager(storage)
        manager.get_or_create("s1", "person_a", "d1")

        # Backdate the stored last_active_at to a known old value
        record = storage.read("sessions", "s1", "state", "person_a")
        outdated = {**record.data, "last_active_at": "2020-01-01T00:00:00+00:00"}
        storage.write(StorageRecord(
            domain="sessions", key="s1", layer="state",
            data=outdated, user_id="person_a",
            version=1, updated_at="2020-01-01T00:00:00+00:00",
        ))

        session2 = manager.get_or_create("s1", "person_a", "d1")
        assert session2.last_active_at != "2020-01-01T00:00:00+00:00"

        # Verify storage was updated too
        stored = storage.read("sessions", "s1", "state", "person_a")
        assert stored.data["last_active_at"] != "2020-01-01T00:00:00+00:00"

    def test_new_session_has_empty_turns(self, manager):
        session = manager.get_or_create("s1", "person_a", "d1")
        assert session.turns == []


# ---------------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------------


class TestAppendTurn:
    def test_adds_turn_to_session(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        session = manager.append_turn("s1", _turn(text="hi there"), user_id="person_a")
        assert len(session.turns) == 1
        assert session.turns[0].text == "hi there"

    def test_accumulates_multiple_turns(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        manager.append_turn("s1", _turn(text="one"), user_id="person_a")
        session = manager.append_turn("s1", _turn(text="two"), user_id="person_a")
        assert len(session.turns) == 2

    def test_evicts_oldest_when_cap_exceeded(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        for i in range(ConversationSessionManager.MAX_TURNS + 3):
            manager.append_turn("s1", _turn(text=f"msg {i}"), user_id="person_a")

        session = manager.get_or_create("s1", "person_a", "d1")
        assert len(session.turns) == ConversationSessionManager.MAX_TURNS
        # Oldest turns evicted — first turn should be msg 3, not msg 0
        assert session.turns[0].text == "msg 3"

    def test_updates_last_active_at(self, storage):
        manager = ConversationSessionManager(storage)
        manager.get_or_create("s1", "person_a", "d1")

        # Backdate last_active_at
        record = storage.read("sessions", "s1", "state", "person_a")
        outdated = {**record.data, "last_active_at": "2020-01-01T00:00:00+00:00"}
        storage.write(StorageRecord(
            domain="sessions", key="s1", layer="state",
            data=outdated, user_id="person_a",
            version=1, updated_at="2020-01-01T00:00:00+00:00",
        ))

        manager.append_turn("s1", _turn(), user_id="person_a")
        stored = storage.read("sessions", "s1", "state", "person_a")
        assert stored.data["last_active_at"] != "2020-01-01T00:00:00+00:00"

    def test_raises_for_unknown_session(self, manager):
        with pytest.raises(ValueError, match="not found"):
            manager.append_turn("no_such_session", _turn(), user_id="person_a")

    def test_persists_turns_across_loads(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        manager.append_turn("s1", _turn(text="stored turn"), user_id="person_a")
        history = manager.load_history("s1", "person_a")
        assert any(t.text == "stored turn" for t in history)


# ---------------------------------------------------------------------------
# load_history
# ---------------------------------------------------------------------------


class TestLoadHistory:
    def test_returns_turns_in_order(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        for text in ("first", "second", "third"):
            manager.append_turn("s1", _turn(text=text), user_id="person_a")
        history = manager.load_history("s1", "person_a")
        assert [t.text for t in history] == ["first", "second", "third"]

    def test_returns_empty_for_unknown_session_id(self, manager):
        assert manager.load_history("no_such_session", "person_a") == []

    def test_returns_correct_turn_fields(self, manager):
        manager.get_or_create("s1", "person_a", "d1")
        turn = ConversationTurn(
            role="user", text="test msg",
            timestamp="2026-05-13T10:00:00+00:00",
            intent="food.query", domain="food",
        )
        manager.append_turn("s1", turn, user_id="person_a")
        history = manager.load_history("s1", "person_a")
        assert history[0].role == "user"
        assert history[0].intent == "food.query"
        assert history[0].domain == "food"


# ---------------------------------------------------------------------------
# expire_inactive
# ---------------------------------------------------------------------------


class TestExpireInactive:
    def test_deletes_sessions_past_ttl(self, storage):
        storage.write(_old_session_record("old_s", "person_a", seconds_ago=700))
        manager = ConversationSessionManager(storage, known_user_ids=["person_a"])
        count = manager.expire_inactive()
        assert count == 1
        assert manager.load_history("old_s", "person_a") == []

    def test_does_not_delete_fresh_sessions(self, storage):
        manager = ConversationSessionManager(storage, known_user_ids=["person_a"])
        manager.get_or_create("fresh_s", "person_a", "d1")
        count = manager.expire_inactive()
        assert count == 0
        # Session record still exists in storage
        assert storage.read("sessions", "fresh_s", "state", "person_a") is not None

    def test_returns_count_of_deleted_sessions(self, storage):
        for i in range(3):
            storage.write(_old_session_record(f"old_{i}", "person_a", seconds_ago=700))
        manager = ConversationSessionManager(storage, known_user_ids=["person_a"])
        count = manager.expire_inactive()
        assert count == 3

    def test_deletes_across_multiple_user_ids(self, storage):
        storage.write(_old_session_record("s_a", "person_a", seconds_ago=700))
        storage.write(_old_session_record("s_b", "person_b", seconds_ago=700))
        manager = ConversationSessionManager(
            storage, known_user_ids=["person_a", "person_b"]
        )
        count = manager.expire_inactive()
        assert count == 2

    def test_explicit_user_ids_overrides_known(self, storage):
        storage.write(_old_session_record("s_a", "person_a", seconds_ago=700))
        storage.write(_old_session_record("s_b", "person_b", seconds_ago=700))
        # known_user_ids includes both, but we only scan person_a
        manager = ConversationSessionManager(
            storage, known_user_ids=["person_a", "person_b"]
        )
        count = manager.expire_inactive(user_ids=["person_a"])
        assert count == 1
        # person_b's session survives
        assert storage.read("sessions", "s_b", "state", "person_b") is not None

    def test_spares_recently_active_mixed_with_expired(self, storage):
        storage.write(_old_session_record("expired", "person_a", seconds_ago=700))
        manager = ConversationSessionManager(storage, known_user_ids=["person_a"])
        manager.get_or_create("fresh", "person_a", "d1")
        count = manager.expire_inactive()
        assert count == 1
        # Fresh session still exists; expired one is gone
        assert storage.read("sessions", "fresh", "state", "person_a") is not None
        assert storage.read("sessions", "expired", "state", "person_a") is None
