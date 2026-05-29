"""Tests for TimeDomain class."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from mithra.domains.time.domain import TimeDomain

from tests.unit.domains.time.conftest import MockCalendarService, _FakeEvent


class TestTimeDomainIdentity:
    def test_domain_id(self):
        d = TimeDomain()
        assert d.domain_id == "time"

    def test_version(self):
        d = TimeDomain()
        assert d.version == "1.0.0"

    def test_display_name(self):
        d = TimeDomain()
        assert d.display_name == "Time & Calendar"

    def test_no_event_subscriptions(self):
        d = TimeDomain()
        assert d.get_event_subscriptions() == []


class TestTimeDomainTools:
    def test_get_tools_returns_eight(self, ctx):
        d = TimeDomain(oauth_manager=ctx.oauth_manager, household_timezone=ctx.household_timezone)
        bundles = d.get_tools(ctx)
        assert len(bundles) == 8

    def test_tools_are_bound_to_context(self, ctx):
        d = TimeDomain(oauth_manager=ctx.oauth_manager)
        bundles = d.get_tools(ctx)
        for b in bundles:
            assert b.tool._ctx.user_id == ctx.user_id


class TestTimeDomainDashboard:
    @pytest.mark.asyncio
    async def test_dashboard_no_oauth_returns_alert(self):
        d = TimeDomain(oauth_manager=None, household_timezone="America/Chicago")
        data = await d.get_dashboard_data("usr_ba6d9199", "hh_b76fef08")
        assert data.domain == "time"
        assert len(data.alerts) > 0

    @pytest.mark.asyncio
    async def test_dashboard_with_events(self, mock_oauth):
        from mithra.domains.time.tools import _range_from_period
        today_start, _ = _range_from_period("today", "America/Chicago")
        events = [
            _FakeEvent(
                "e1", "Dentist",
                today_start + timedelta(hours=10),
                today_start + timedelta(hours=11),
            ),
        ]
        svc = MockCalendarService(events=events)
        d = TimeDomain(oauth_manager=mock_oauth, household_timezone="America/Chicago")
        with patch("mithra.domains.time.domain.GoogleCalendarService", return_value=svc):
            data = await d.get_dashboard_data("usr_ba6d9199", "hh_b76fef08")
        assert data.domain == "time"
        assert "1 event" in data.summary

    @pytest.mark.asyncio
    async def test_dashboard_empty_calendar(self, mock_oauth):
        svc = MockCalendarService(events=[])
        d = TimeDomain(oauth_manager=mock_oauth, household_timezone="America/Chicago")
        with patch("mithra.domains.time.domain.GoogleCalendarService", return_value=svc):
            data = await d.get_dashboard_data("usr_ba6d9199", "hh_b76fef08")
        assert "Nothing" in data.summary

    @pytest.mark.asyncio
    async def test_dashboard_error_returns_safe_fallback(self, mock_oauth):
        bad_svc = MockCalendarService()
        bad_svc.list_events = lambda *a, **k: (_ for _ in ()).throw(Exception("boom"))
        d = TimeDomain(oauth_manager=mock_oauth, household_timezone="America/Chicago")
        with patch("mithra.domains.time.domain.GoogleCalendarService", return_value=bad_svc):
            data = await d.get_dashboard_data("usr_ba6d9199", "hh_b76fef08")
        assert data.domain == "time"
        assert "unavailable" in data.summary.lower()

    @pytest.mark.asyncio
    async def test_dashboard_has_quick_actions(self, mock_oauth):
        svc = MockCalendarService(events=[])
        d = TimeDomain(oauth_manager=mock_oauth, household_timezone="America/Chicago")
        with patch("mithra.domains.time.domain.GoogleCalendarService", return_value=svc):
            data = await d.get_dashboard_data("usr_ba6d9199", "hh_b76fef08")
        assert len(data.quick_actions) >= 1
