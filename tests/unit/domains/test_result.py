"""Tests for ToolResult and DomainEvent."""

from __future__ import annotations

from mithra.domains.result import DomainEvent, ToolResult


class TestToolResult:
    def test_ok_factory(self):
        r = ToolResult.ok("Created event", data={"id": "evt_1"})
        assert r.success is True
        assert r.summary == "Created event"
        assert r.data == {"id": "evt_1"}
        assert r.error is None
        assert r.error_code is None
        assert r.undo_token is None
        assert r.events == []

    def test_ok_with_undo_token(self):
        r = ToolResult.ok("Wrote record", undo_token="undo_abc")
        assert r.undo_token == "undo_abc"

    def test_ok_with_events(self):
        evt = DomainEvent(
            event_type="appointment_created",
            source_domain="time",
            payload={"date": "2026-06-01"},
            user_id="usr_1",
            household_id="hh_1",
            request_id="req_1",
        )
        r = ToolResult.ok("Created", events=[evt])
        assert len(r.events) == 1
        assert r.events[0].event_type == "appointment_created"

    def test_fail_factory(self):
        r = ToolResult.fail("Could not create event: Google not connected", error_code="GOOGLE_NOT_CONNECTED")
        assert r.success is False
        assert r.summary == "Could not create event: Google not connected"
        assert r.error_code == "GOOGLE_NOT_CONNECTED"
        assert r.events == []

    def test_fail_summary_always_present(self):
        r = ToolResult.fail("Something went wrong")
        assert r.summary  # ONE-WAY DOOR

    def test_summary_is_always_present_on_ok(self):
        r = ToolResult.ok("summary here")
        assert r.summary  # ONE-WAY DOOR


class TestDomainEvent:
    def test_creation_auto_timestamp(self):
        evt = DomainEvent(
            event_type="expense_recorded",
            source_domain="money",
            payload={"amount": 50},
            user_id="usr_1",
            household_id="hh_1",
            request_id="req_1",
        )
        assert evt.timestamp  # auto-set
        assert "T" in evt.timestamp  # ISO8601 format

    def test_explicit_timestamp_preserved(self):
        evt = DomainEvent(
            event_type="test",
            source_domain="test",
            payload={},
            user_id="u",
            household_id="h",
            request_id="r",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        assert evt.timestamp == "2026-01-01T00:00:00+00:00"

    def test_all_required_fields(self):
        evt = DomainEvent(
            event_type="item_ordered",
            source_domain="shopping",
            payload={"item": "milk", "price": 3.50},
            user_id="usr_ba6d9199",
            household_id="hh_b76fef08",
            request_id="req_abc",
        )
        assert evt.source_domain == "shopping"
        assert evt.payload["item"] == "milk"
