"""Tests for DeadLetterQueue — terminal failure store backed by StorageAdapter."""

from __future__ import annotations

import pytest

from mithra.autonomy.dead_letter import DeadLetter, DeadLetterQueue
from mithra.storage.local import LocalStorageAdapter


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))


@pytest.fixture
def dlq(storage):
    return DeadLetterQueue(storage)


def _letter(
    letter_id: str = "l1",
    action_type: str = "send_message",
    resolved: bool = False,
) -> DeadLetter:
    return DeadLetter(
        letter_id=letter_id,
        action_id="a1",
        user_id="household",
        action_type=action_type,
        payload={"text": "hello"},
        error="Connection refused after 3 attempts",
        attempt_count=3,
        first_attempted_at="2026-01-01T10:00:00+00:00",
        last_attempted_at="2026-01-01T10:05:00+00:00",
        resolved=resolved,
    )


# ---------------------------------------------------------------------------
# enqueue + list_unresolved round-trip
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_stores_letter_retrievable_by_list_unresolved(self, dlq):
        dlq.enqueue(_letter(letter_id="lt1"))
        unresolved = dlq.list_unresolved()
        assert any(l.letter_id == "lt1" for l in unresolved)

    def test_enqueue_returns_the_letter(self, dlq):
        letter = _letter(letter_id="lt2")
        returned = dlq.enqueue(letter)
        assert returned.letter_id == "lt2"

    def test_enqueue_multiple_letters_all_listed(self, dlq):
        for i in range(3):
            dlq.enqueue(_letter(letter_id=f"l{i}"))
        assert len(dlq.list_unresolved()) == 3


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_marks_letter_as_resolved(self, dlq):
        dlq.enqueue(_letter(letter_id="res1"))
        result = dlq.resolve("res1")
        assert result is True
        unresolved = dlq.list_unresolved()
        assert not any(l.letter_id == "res1" for l in unresolved)

    def test_resolve_returns_false_for_unknown_letter_id(self, dlq):
        result = dlq.resolve("no-such-letter")
        assert result is False

    def test_resolve_idempotent_second_call_returns_true(self, dlq):
        dlq.enqueue(_letter(letter_id="idem1"))
        dlq.resolve("idem1")
        result = dlq.resolve("idem1")
        assert result is True


# ---------------------------------------------------------------------------
# list_unresolved filtering
# ---------------------------------------------------------------------------


class TestListUnresolved:
    def test_list_unresolved_excludes_resolved_letters(self, dlq):
        dlq.enqueue(_letter(letter_id="r1"))
        dlq.enqueue(_letter(letter_id="r2"))
        dlq.resolve("r1")

        unresolved = dlq.list_unresolved()
        ids = [l.letter_id for l in unresolved]
        assert "r1" not in ids
        assert "r2" in ids

    def test_list_unresolved_newest_first(self, dlq):
        dlq.enqueue(_letter(letter_id="old"))
        # overwrite last_attempted_at by enqueuing a newer one
        newer = DeadLetter(
            letter_id="new",
            action_id="a2",
            user_id="household",
            action_type="send_message",
            payload={},
            error="timeout",
            attempt_count=3,
            first_attempted_at="2026-01-01T10:00:00+00:00",
            last_attempted_at="2026-01-01T11:00:00+00:00",
        )
        dlq.enqueue(newer)
        letters = dlq.list_unresolved()
        ids = [l.letter_id for l in letters]
        assert ids.index("new") < ids.index("old")

    def test_list_unresolved_respects_limit(self, dlq):
        for i in range(10):
            dlq.enqueue(_letter(letter_id=f"lim{i}"))
        result = dlq.list_unresolved(limit=4)
        assert len(result) == 4


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok_status(self, dlq):
        h = dlq.health()
        assert h["status"] == "ok"

    def test_health_returns_unresolved_count(self, dlq):
        dlq.enqueue(_letter(letter_id="h1"))
        dlq.enqueue(_letter(letter_id="h2"))
        dlq.enqueue(_letter(letter_id="h3"))
        dlq.resolve("h1")
        h = dlq.health()
        assert h["unresolved_count"] == 2

    def test_health_unresolved_count_zero_when_all_resolved(self, dlq):
        dlq.enqueue(_letter(letter_id="z1"))
        dlq.resolve("z1")
        h = dlq.health()
        assert h["unresolved_count"] == 0
