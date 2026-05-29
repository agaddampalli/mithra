"""Shared fixtures for Time domain unit tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from mithra.domains.context import DomainContext


# ---------------------------------------------------------------------------
# Fake gcsa event
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(
        self,
        event_id: str = "evt_001",
        summary: str = "Test Event",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        self.id = event_id
        self.summary = summary
        self.start = start or datetime.now(timezone.utc) + timedelta(hours=2)
        self.end = end or self.start + timedelta(hours=1)
        self.location = None
        self.description = None
        self.recurrence = []


# ---------------------------------------------------------------------------
# Mock GoogleCalendarService
# ---------------------------------------------------------------------------

class MockCalendarService:
    """Fake GoogleCalendarService — returns configurable in-memory events."""

    def __init__(self, events: list[_FakeEvent] | None = None) -> None:
        self._events = events or []
        self.created: list[Any] = []
        self.updated: list[Any] = []
        self.deleted: list[str] = []

    def list_events(self, user_id, start, end, calendar_id="primary"):
        return [e for e in self._events if start <= e.start <= end]

    def find_events_by_title(self, user_id, query, start=None, end=None, calendar_id="primary"):
        q = query.lower()
        return [e for e in self._events if q in (e.summary or "").lower()]

    def create_event(self, user_id, event, calendar_id="primary"):
        self.created.append(event)
        # Return a _FakeEvent so tests can read .id without triggering gcsa's read-only property
        ev_start = getattr(event, "start", None) or datetime.now(timezone.utc)
        ev_end = getattr(event, "end", None) or ev_start + timedelta(hours=1)
        return _FakeEvent(
            event_id=f"created_{len(self.created)}",
            summary=getattr(event, "summary", ""),
            start=ev_start,
            end=ev_end,
        )

    def update_event(self, user_id, event, calendar_id="primary"):
        self.updated.append(event)
        ev_start = getattr(event, "start", None) or datetime.now(timezone.utc)
        ev_end = getattr(event, "end", None) or ev_start + timedelta(hours=1)
        return _FakeEvent(
            event_id=getattr(event, "id", f"updated_{len(self.updated)}"),
            summary=getattr(event, "summary", ""),
            start=ev_start,
            end=ev_end,
        )

    def delete_event(self, user_id, event_id, calendar_id="primary"):
        self.deleted.append(event_id)

    def get_free_busy(self, user_ids, start, end):
        result = {}
        for uid in user_ids:
            blocks = []
            for ev in self._events:
                if start <= ev.start <= end:
                    blocks.append({"start": ev.start, "end": ev.end})
            result[uid] = blocks
        return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_oauth() -> MagicMock:
    """OAuthManager stub — has a _store.load() that returns a token dict."""
    oauth = MagicMock()
    oauth._store.load.return_value = {
        "access_token": "fake_access_token",
        "refresh_token": "fake_refresh_token",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
    }
    return oauth


@pytest.fixture()
def ctx(mock_oauth) -> DomainContext:
    return DomainContext(
        user_id="usr_ba6d9199",
        household_id="hh_b76fef08",
        household_timezone="America/Chicago",
        oauth_manager=mock_oauth,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_time_001",
        channel="telegram",
        language="en-US",
        autonomy_override=None,
    )


@pytest.fixture()
def ctx_no_oauth() -> DomainContext:
    return DomainContext(
        user_id="usr_ba6d9199",
        household_id="hh_b76fef08",
        household_timezone="America/Chicago",
        oauth_manager=None,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_time_002",
        channel="telegram",
        language="en-US",
        autonomy_override=None,
    )


@pytest.fixture()
def today_events() -> list[_FakeEvent]:
    """Two events within today's window (Chicago TZ)."""
    from mithra.domains.time.tools import _range_from_period
    today_start, _ = _range_from_period("today", "America/Chicago")
    return [
        _FakeEvent("evt_001", "Dentist", today_start + timedelta(hours=10), today_start + timedelta(hours=11)),
        _FakeEvent("evt_002", "Gym", today_start + timedelta(hours=14), today_start + timedelta(hours=15)),
    ]


@pytest.fixture()
def mock_svc(today_events) -> MockCalendarService:
    return MockCalendarService(events=today_events)
