"""Tests for TimeDomain class."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date

import pytest

from mithra.domains.context import DomainContext
from mithra.domains.time.domain import TimeDomain


def _make_ctx(db_path: str = "", household_id: str = "hh_b76fef08") -> DomainContext:
    return DomainContext(
        user_id="usr_ba6d9199",
        household_id=household_id,
        household_timezone="America/Chicago",
        oauth_manager=None,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_test",
        channel="ha",
        local_db_path=db_path,
    )


def _create_events_db(path: str, hid: str, events: list[dict]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS calendar_events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT,
                household_id TEXT,
                summary TEXT,
                start_time TEXT,
                end_time TEXT,
                is_recurring INTEGER DEFAULT 0,
                source TEXT,
                scope TEXT DEFAULT 'household',
                calendar_id TEXT,
                raw_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        today = date.today().isoformat()
        for ev in events:
            conn.execute(
                """INSERT OR REPLACE INTO calendar_events
                   (event_id, user_id, household_id, summary, start_time, end_time, scope, source, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev["id"], ev.get("user_id", "usr_ba6d9199"), hid,
                    ev["summary"],
                    ev.get("start_time", f"{today}T10:00:00"),
                    ev.get("end_time", f"{today}T11:00:00"),
                    ev.get("scope", "household"),
                    "mithra", today, today,
                ),
            )


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
    def test_dashboard_no_db_returns_error(self):
        d = TimeDomain()
        ctx = _make_ctx(db_path="")
        data = d.get_dashboard_data(ctx)
        assert data.domain == "time"
        assert data.error is not None

    def test_dashboard_with_events(self):
        d = TimeDomain(household_id="hh_b76fef08")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        _create_events_db(db_path, "hh_b76fef08", [
            {"id": "evt_001", "summary": "Dentist"},
            {"id": "evt_002", "summary": "Gym"},
        ])
        ctx = _make_ctx(db_path=db_path)
        data = d.get_dashboard_data(ctx)
        assert data.domain == "time"
        assert "2 events" in data.summary
        assert len(data.items) == 2

    def test_dashboard_empty_calendar(self):
        d = TimeDomain()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        _create_events_db(db_path, "hh_b76fef08", [])
        ctx = _make_ctx(db_path=db_path)
        data = d.get_dashboard_data(ctx)
        assert "Nothing" in data.summary

    def test_dashboard_bad_db_returns_safe_fallback(self):
        d = TimeDomain()
        ctx = _make_ctx(db_path="/tmp/does_not_exist_mithra_test.db")
        data = d.get_dashboard_data(ctx)
        assert data.domain == "time"
        assert "unavailable" in data.summary.lower()

    def test_dashboard_has_quick_actions(self):
        d = TimeDomain()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        _create_events_db(db_path, "hh_b76fef08", [])
        ctx = _make_ctx(db_path=db_path)
        data = d.get_dashboard_data(ctx)
        assert len(data.quick_actions) >= 1

    def test_dashboard_items_have_domain_and_scope(self):
        d = TimeDomain()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        _create_events_db(db_path, "hh_b76fef08", [
            {"id": "evt_001", "summary": "Meeting", "scope": "household"},
        ])
        ctx = _make_ctx(db_path=db_path)
        data = d.get_dashboard_data(ctx)
        assert len(data.items) == 1
        assert data.items[0].domain == "time"
        assert data.items[0].scope == "household"
