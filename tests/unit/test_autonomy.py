"""Unit tests for ActionLog and DeadLetterQueue."""

import base64
import os
import uuid
from datetime import datetime, timezone

import pytest

from mithra.autonomy.action_log import ActionLog
from mithra.autonomy.dead_letter import DeadLetter, DeadLetterQueue
from mithra.autonomy.engine import ActionLogEntry, Reversibility
from mithra.storage.local import LocalStorageAdapter


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "test.db"))


@pytest.fixture
def action_log(storage):
    return ActionLog(storage)


@pytest.fixture
def dlq(storage):
    return DeadLetterQueue(storage)


def _entry(**kwargs):
    defaults = dict(
        action_id=str(uuid.uuid4()),
        request_id="req-1",
        user_id="person_a",
        domain="people",
        action_type="send_birthday_message",
        payload={"phone": "+1555"},
        status="executed",
        reversibility=Reversibility.PARTIAL,
        timestamp=_now(),
        intent="people.send_birthday_message",
        autonomy_level=2,
        output_channel="whatsapp",
        executed_at=_now(),
        success=True,
    )
    defaults.update(kwargs)
    return ActionLogEntry(**defaults)


class TestActionLog:
    def test_append_and_get(self, action_log):
        entry = _entry()
        action_log.append(entry)
        fetched = action_log.get(entry.action_id, "person_a")
        assert fetched is not None
        assert fetched.action_id == entry.action_id
        assert fetched.domain == "people"

    def test_get_missing_returns_none(self, action_log):
        assert action_log.get("nonexistent", "person_a") is None

    def test_list_recent(self, action_log):
        for _ in range(3):
            action_log.append(_entry())
        entries = action_log.list_recent("person_a")
        assert len(entries) == 3

    def test_list_recent_domain_filter(self, action_log):
        action_log.append(_entry(domain="people"))
        action_log.append(_entry(domain="money"))
        people_entries = action_log.list_recent("person_a", domain="people")
        assert all(e.domain == "people" for e in people_entries)
        assert len(people_entries) == 1

    def test_list_recent_limit(self, action_log):
        for _ in range(10):
            action_log.append(_entry())
        assert len(action_log.list_recent("person_a", limit=5)) == 5


class TestDeadLetterQueue:
    def _letter(self, **kwargs):
        defaults = dict(
            letter_id=str(uuid.uuid4()),
            action_id=str(uuid.uuid4()),
            user_id="household",
            action_type="send",
            payload={"text": "hello"},
            error="Connection refused",
            attempt_count=3,
            first_attempted_at=_now(),
            last_attempted_at=_now(),
        )
        defaults.update(kwargs)
        return DeadLetter(**defaults)

    def test_enqueue_and_list_unresolved(self, dlq):
        letter = self._letter()
        dlq.enqueue(letter)
        unresolved = dlq.list_unresolved()
        assert any(l.letter_id == letter.letter_id for l in unresolved)

    def test_resolve(self, dlq):
        letter = self._letter()
        dlq.enqueue(letter)
        result = dlq.resolve(letter.letter_id)
        assert result is True
        unresolved = dlq.list_unresolved()
        assert not any(l.letter_id == letter.letter_id for l in unresolved)

    def test_resolve_nonexistent_returns_false(self, dlq):
        assert dlq.resolve("ghost-id") is False

    def test_health(self, dlq):
        h = dlq.health()
        assert h["status"] == "ok"
        assert "unresolved_count" in h
