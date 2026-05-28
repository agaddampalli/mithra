"""Tests for DashboardData and DashboardItem."""

from __future__ import annotations

import pytest

from mithra.domains.dashboard import DashboardData, DashboardItem


class TestDashboardItem:
    def test_creation(self):
        item = DashboardItem(
            id="evt_1",
            title="Dentist appointment",
            subtitle="Tuesday 3pm",
            icon="mdi:calendar",
            priority="high",
            action="calendar_read_events",
        )
        assert item.id == "evt_1"
        assert item.priority == "high"
        assert item.icon == "mdi:calendar"

    def test_no_action(self):
        item = DashboardItem(
            id="info_1",
            title="No action",
            subtitle=None,
            icon="mdi:info",
            priority="low",
            action=None,
        )
        assert item.action is None
        assert item.subtitle is None


class TestDashboardData:
    def test_creation(self):
        data = DashboardData(
            domain="time",
            updated_at="2026-05-27T12:00:00+00:00",
            summary="2 events today",
        )
        assert data.domain == "time"
        assert data.summary == "2 events today"
        assert data.items == []
        assert data.alerts == []
        assert data.quick_actions == []

    def test_with_items_and_alerts(self):
        item = DashboardItem(
            id="1", title="Event", subtitle=None,
            icon="mdi:calendar", priority="high", action=None,
        )
        data = DashboardData(
            domain="time",
            updated_at="2026-05-27T12:00:00+00:00",
            summary="1 event today",
            items=[item],
            alerts=["Dentist in 1 hour"],
            quick_actions=[{"label": "Add event", "tool": "calendar_create_event"}],
        )
        assert len(data.items) == 1
        assert data.alerts[0] == "Dentist in 1 hour"
        assert data.quick_actions[0]["label"] == "Add event"

    def test_domain_field_matches_domain_id(self):
        data = DashboardData(
            domain="money",
            updated_at="2026-05-27T12:00:00+00:00",
            summary="$250 spent this week",
        )
        assert data.domain == "money"
