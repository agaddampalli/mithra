"""Tests for Time domain tool implementations.

All tests use MockCalendarService — no real Google API calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from mithra.domains.context import DomainContext
from mithra.domains.result import ToolResult
from mithra.domains.time.tools import (
    CalendarCheckAvailabilityTool,
    CalendarCreateEventTool,
    CalendarCreateRecurringTool,
    CalendarDailyBriefingTool,
    CalendarDeleteEventTool,
    CalendarFindTimeTool,
    CalendarReadEventsTool,
    CalendarUpdateEventTool,
    build_time_tools,
)

from tests.unit.domains.time.conftest import MockCalendarService, _FakeEvent

TZ = "America/Chicago"


def _patch_svc(mock_svc: MockCalendarService):
    """Context manager that patches GoogleCalendarService with mock_svc."""
    return patch(
        "mithra.domains.time.tools.GoogleCalendarService",
        return_value=mock_svc,
    )


# ---------------------------------------------------------------------------
# build_time_tools()
# ---------------------------------------------------------------------------

class TestBuildTimeTools:
    def test_returns_eight_bundles(self, ctx):
        bundles = build_time_tools(ctx)
        assert len(bundles) == 8

    def test_all_have_metadata(self, ctx):
        bundles = build_time_tools(ctx)
        for b in bundles:
            assert b.metadata is not None
            assert b.metadata.domain_id == "time"

    def test_all_have_tool(self, ctx):
        bundles = build_time_tools(ctx)
        for b in bundles:
            assert b.tool is not None
            assert hasattr(b.tool, "name")

    def test_tool_names_are_unique(self, ctx):
        bundles = build_time_tools(ctx)
        names = [b.tool.name for b in bundles]
        assert len(names) == len(set(names))

    def test_delete_is_destructive(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_time_tools(ctx)
        delete_bundle = next(b for b in bundles if b.tool.name == "calendar_delete_event")
        assert delete_bundle.metadata.permission == PermissionTier.DESTRUCTIVE

    def test_read_tools_are_read_permission(self, ctx):
        from mithra.domains.bundle import PermissionTier
        read_names = {
            "calendar_read_events", "calendar_check_availability",
            "calendar_daily_briefing", "calendar_find_time",
        }
        bundles = build_time_tools(ctx)
        for b in bundles:
            if b.tool.name in read_names:
                assert b.metadata.permission == PermissionTier.READ

    def test_write_tools_emit_events(self, ctx):
        write_events = {
            "calendar_create_event": "appointment_created",
            "calendar_create_recurring": "recurring_event_created",
            "calendar_update_event": "appointment_updated",
            "calendar_delete_event": "appointment_cancelled",
        }
        bundles = build_time_tools(ctx)
        for b in bundles:
            if b.tool.name in write_events:
                assert b.metadata.event_emitted == write_events[b.tool.name]


# ---------------------------------------------------------------------------
# No OAuth — all tools fail gracefully
# ---------------------------------------------------------------------------

class TestNoOAuth:
    def test_read_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarReadEventsTool(context=ctx_no_oauth)
        result = tool._run(time_range="today")
        assert isinstance(result, ToolResult)
        assert result.success is False
        assert result.error_code == "GOOGLE_NOT_CONNECTED"

    def test_create_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarCreateEventTool(context=ctx_no_oauth)
        result = tool._run(title="Test", start_time="tomorrow 3pm")
        assert result.success is False
        assert result.error_code == "GOOGLE_NOT_CONNECTED"

    def test_delete_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarDeleteEventTool(context=ctx_no_oauth)
        result = tool._run(event_query="dentist")
        assert result.success is False
        assert result.error_code == "GOOGLE_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# calendar_read_events
# ---------------------------------------------------------------------------

class TestCalendarReadEvents:
    def test_returns_events_when_found(self, ctx, mock_svc):
        tool = CalendarReadEventsTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(time_range="today")
        assert result.success is True
        assert "Dentist" in result.summary or "2 events" in result.summary

    def test_empty_calendar_returns_ok(self, ctx):
        tool = CalendarReadEventsTool(context=ctx)
        with _patch_svc(MockCalendarService(events=[])):
            result = tool._run(time_range="today")
        assert result.success is True
        assert "No events" in result.summary

    def test_result_contains_event_count(self, ctx, mock_svc):
        tool = CalendarReadEventsTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(time_range="today")
        assert result.data is not None
        assert result.data["count"] == 2

    def test_service_error_returns_fail(self, ctx):
        bad_svc = MockCalendarService()
        bad_svc.list_events = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no token"))
        tool = CalendarReadEventsTool(context=ctx)
        with _patch_svc(bad_svc):
            result = tool._run(time_range="today")
        assert result.success is False


# ---------------------------------------------------------------------------
# calendar_create_event
# ---------------------------------------------------------------------------

class TestCalendarCreateEvent:
    def test_creates_event_successfully(self, ctx, mock_svc):
        tool = CalendarCreateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(title="Doctor", start_time="tomorrow 3pm")
        assert result.success is True
        assert "Doctor" in result.summary
        assert len(mock_svc.created) == 1

    def test_unparseable_date_returns_fail(self, ctx, mock_svc):
        tool = CalendarCreateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(title="Test", start_time="blurp zork")
        assert result.success is False
        assert result.error_code == "UNPARSEABLE_DATE"

    def test_emits_appointment_created_event(self, ctx, mock_svc):
        tool = CalendarCreateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(title="Meeting", start_time="monday 10am")
        assert result.success is True
        assert len(result.events) == 1
        assert result.events[0].event_type == "appointment_created"

    def test_undo_token_is_event_id(self, ctx, mock_svc):
        tool = CalendarCreateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(title="Dentist", start_time="tomorrow 3pm")
        assert result.undo_token is not None
        assert len(result.undo_token) > 0


# ---------------------------------------------------------------------------
# calendar_create_recurring
# ---------------------------------------------------------------------------

class TestCalendarCreateRecurring:
    def test_creates_weekly_event(self, ctx, mock_svc):
        tool = CalendarCreateRecurringTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(
                title="Trash pickup",
                start_time="tuesday 7am",
                frequency="weekly",
                by_week_day="TU",
            )
        assert result.success is True
        assert "Trash pickup" in result.summary
        assert len(mock_svc.created) == 1

    def test_invalid_frequency_returns_fail(self, ctx, mock_svc):
        tool = CalendarCreateRecurringTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(
                title="Test",
                start_time="monday 9am",
                frequency="hourly",
            )
        assert result.success is False
        assert result.error_code == "INVALID_FREQUENCY"

    def test_emits_recurring_event_created(self, ctx, mock_svc):
        tool = CalendarCreateRecurringTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(
                title="Standup",
                start_time="monday 9am",
                frequency="weekly",
                by_week_day="MO,TU,WE,TH,FR",
            )
        assert result.success is True
        assert len(result.events) == 1
        assert result.events[0].event_type == "recurring_event_created"

    def test_biweekly_recycling(self, ctx, mock_svc):
        tool = CalendarCreateRecurringTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(
                title="Recycling",
                start_time="tuesday 7am",
                frequency="weekly",
                interval=2,
                by_week_day="TU",
            )
        assert result.success is True
        assert "every 2" in result.summary.lower() or "Recycling" in result.summary


# ---------------------------------------------------------------------------
# calendar_update_event
# ---------------------------------------------------------------------------

class TestCalendarUpdateEvent:
    def test_updates_existing_event(self, ctx, mock_svc):
        tool = CalendarUpdateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="Dentist", new_time="Friday 4pm")
        assert result.success is True
        assert len(mock_svc.updated) == 1

    def test_event_not_found_returns_fail(self, ctx, mock_svc):
        tool = CalendarUpdateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="nonexistent_event_xyz")
        assert result.success is False
        assert result.error_code == "EVENT_NOT_FOUND"

    def test_unparseable_new_time_returns_fail(self, ctx, mock_svc):
        tool = CalendarUpdateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="Dentist", new_time="blurp zork")
        assert result.success is False
        assert result.error_code == "UNPARSEABLE_DATE"

    def test_emits_appointment_updated(self, ctx, mock_svc):
        tool = CalendarUpdateEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="Dentist", new_title="Dentist Appointment")
        assert result.success is True
        assert any(e.event_type == "appointment_updated" for e in result.events)


# ---------------------------------------------------------------------------
# calendar_delete_event
# ---------------------------------------------------------------------------

class TestCalendarDeleteEvent:
    def test_deletes_found_event(self, ctx, mock_svc):
        tool = CalendarDeleteEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="Dentist")
        assert result.success is True
        assert len(mock_svc.deleted) == 1

    def test_not_found_returns_fail(self, ctx, mock_svc):
        tool = CalendarDeleteEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="xyz_no_such_event")
        assert result.success is False
        assert result.error_code == "EVENT_NOT_FOUND"

    def test_emits_appointment_cancelled(self, ctx, mock_svc):
        tool = CalendarDeleteEventTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(event_query="Gym")
        assert result.success is True
        assert any(e.event_type == "appointment_cancelled" for e in result.events)


# ---------------------------------------------------------------------------
# calendar_check_availability
# ---------------------------------------------------------------------------

class TestCalendarCheckAvailability:
    def test_returns_free_slots(self, ctx, mock_svc):
        tool = CalendarCheckAvailabilityTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(time_range="today", duration_minutes=30)
        assert result.success is True
        assert result.data is not None
        assert "free_slots" in result.data

    def test_no_calendar_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarCheckAvailabilityTool(context=ctx_no_oauth)
        result = tool._run(time_range="today")
        assert result.success is False

    def test_fully_booked_day_reports_no_slots(self, ctx):
        from mithra.domains.time.tools import _range_from_period
        today_start, today_end = _range_from_period("today", "America/Chicago")
        full_day = [
            _FakeEvent("e1", "Block", today_start, today_end),
        ]
        tool = CalendarCheckAvailabilityTool(context=ctx)
        with _patch_svc(MockCalendarService(events=full_day)):
            result = tool._run(time_range="today", duration_minutes=30)
        assert result.success is True
        assert result.data["free_slots"] == []


# ---------------------------------------------------------------------------
# calendar_daily_briefing
# ---------------------------------------------------------------------------

class TestCalendarDailyBriefing:
    def test_returns_summary_with_events(self, ctx, mock_svc):
        tool = CalendarDailyBriefingTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(period="today")
        assert result.success is True
        assert result.summary != ""

    def test_empty_calendar_returns_ok(self, ctx):
        tool = CalendarDailyBriefingTool(context=ctx)
        with _patch_svc(MockCalendarService(events=[])):
            result = tool._run(period="today")
        assert result.success is True
        assert "Nothing" in result.summary or "nothing" in result.summary.lower()

    def test_no_oauth_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarDailyBriefingTool(context=ctx_no_oauth)
        result = tool._run(period="today")
        assert result.success is False
        assert result.error_code == "GOOGLE_NOT_CONNECTED"


# ---------------------------------------------------------------------------
# calendar_find_time
# ---------------------------------------------------------------------------

class TestCalendarFindTime:
    def test_returns_suggested_slots(self, ctx, mock_svc):
        tool = CalendarFindTimeTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(duration_minutes=60, within_period="today")
        assert result.success is True
        assert result.data is not None
        # May have 0 or more slots — just check no exception

    def test_no_oauth_fails_gracefully(self, ctx_no_oauth):
        tool = CalendarFindTimeTool(context=ctx_no_oauth)
        result = tool._run(duration_minutes=60, within_period="today")
        assert result.success is False

    def test_preferred_time_morning(self, ctx, mock_svc):
        tool = CalendarFindTimeTool(context=ctx)
        with _patch_svc(mock_svc):
            result = tool._run(
                duration_minutes=30,
                within_period="this week",
                preferred_time="morning",
            )
        assert result.success is True
