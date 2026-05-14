"""Tests for ActionLog — append-only action history backed by StorageAdapter."""

from __future__ import annotations

import pytest

from mithra.autonomy.action_log import ActionLog
from mithra.autonomy.engine import ActionLogEntry, Reversibility
from mithra.storage.local import LocalStorageAdapter


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))


@pytest.fixture
def log(storage):
    return ActionLog(storage)


def _entry(
    action_id: str = "a1",
    domain: str = "food",
    executed_at: str = "2026-01-01T10:00:00+00:00",
    user_id: str = "person_a",
) -> ActionLogEntry:
    return ActionLogEntry(
        action_id=action_id,
        request_id="req-1",
        user_id=user_id,
        domain=domain,
        action_type="do_thing",
        payload={"k": "v"},
        status="executed",
        reversibility=Reversibility.FULL,
        timestamp=executed_at,
        intent=f"{domain}.do_thing",
        autonomy_level=2,
        output_channel="telegram",
        executed_at=executed_at,
        success=True,
    )


# ---------------------------------------------------------------------------
# append + get round-trip
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_stores_entry_retrievable_by_get(self, log):
        entry = _entry(action_id="abc")
        log.append(entry)
        result = log.get("abc", "person_a")
        assert result is not None
        assert result.action_id == "abc"
        assert result.domain == "food"

    def test_append_returns_the_entry(self, log):
        entry = _entry(action_id="ret1")
        returned = log.append(entry)
        assert returned.action_id == "ret1"

    def test_get_returns_none_for_unknown_action_id(self, log):
        result = log.get("no-such-id", "person_a")
        assert result is None


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------


class TestListRecent:
    def test_list_recent_returns_entries_newest_first(self, log):
        log.append(_entry(action_id="old", executed_at="2026-01-01T08:00:00+00:00"))
        log.append(_entry(action_id="mid", executed_at="2026-01-01T09:00:00+00:00"))
        log.append(_entry(action_id="new", executed_at="2026-01-01T10:00:00+00:00"))

        entries = log.list_recent("person_a")
        ids = [e.action_id for e in entries]
        assert ids.index("new") < ids.index("mid") < ids.index("old")

    def test_list_recent_filters_by_domain(self, log):
        log.append(_entry(action_id="food1", domain="food"))
        log.append(_entry(action_id="home1", domain="home"))
        log.append(_entry(action_id="food2", domain="food"))

        food_entries = log.list_recent("person_a", domain="food")
        assert all(e.domain == "food" for e in food_entries)
        assert len(food_entries) == 2

    def test_list_recent_respects_limit(self, log):
        for i in range(10):
            log.append(_entry(action_id=f"e{i}"))

        entries = log.list_recent("person_a", limit=3)
        assert len(entries) == 3

    def test_list_recent_returns_empty_for_unknown_user(self, log):
        log.append(_entry(action_id="x", user_id="person_a"))
        result = log.list_recent("person_b")
        assert result == []

    def test_list_recent_all_entries_when_no_domain_filter(self, log):
        log.append(_entry(action_id="d1", domain="food"))
        log.append(_entry(action_id="d2", domain="money"))
        entries = log.list_recent("person_a")
        assert len(entries) == 2
