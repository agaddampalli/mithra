"""Tests for CalendarAdapter -- Google Calendar API output adapter."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mithra.adapters.messaging import DeliveryReceipt, OutboundMessage
from mithra.adapters.output.calendar import CalendarAdapter


_BASE = "https://www.googleapis.com/calendar/v3"


def _make_adapter() -> CalendarAdapter:
    """Create an adapter with no Nango client (token auth skipped in unit tests)."""
    return CalendarAdapter()


# ---------------------------------------------------------------------------
# create_event()
# ---------------------------------------------------------------------------

class TestCreateEvent:
    @pytest.mark.asyncio
    async def test_create_event_calls_google_api_with_correct_body(self):
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "evt-001"})

        with respx.mock:
            respx.post(f"{_BASE}/calendars/primary/events").mock(side_effect=capture)
            adapter = _make_adapter()
            event_id = await adapter.create_event("primary", {"summary": "Dentist"})

        assert event_id == "evt-001"
        assert captured[0]["summary"] == "Dentist"

    @pytest.mark.asyncio
    async def test_create_event_returns_event_id(self):
        with respx.mock:
            respx.post(f"{_BASE}/calendars/primary/events").mock(
                return_value=httpx.Response(200, json={"id": "evt-abc"})
            )
            adapter = _make_adapter()
            eid = await adapter.create_event("primary", {"summary": "Test"})
        assert eid == "evt-abc"


# ---------------------------------------------------------------------------
# send() -- parse JSON from message.text
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.mark.asyncio
    async def test_send_returns_receipt_with_event_id_in_message_id(self):
        with respx.mock:
            calendar_id = "primary"
            respx.post(f"{_BASE}/calendars/{calendar_id}/events").mock(
                return_value=httpx.Response(200, json={"id": "evt-99"})
            )
            adapter = _make_adapter()
            event_payload = json.dumps({
                "summary": "Meeting",
                "start_datetime": "2026-06-01T10:00:00+00:00",
                "end_datetime": "2026-06-01T11:00:00+00:00",
            })
            receipt = await adapter.send("person_a", OutboundMessage(text=event_payload))

        assert receipt.success is True
        assert receipt.message_id == "evt-99"
        assert receipt.channel == "calendar"
        assert receipt.undo_strategy == "delete_event"

    @pytest.mark.asyncio
    async def test_send_falls_back_to_plain_text_as_summary(self):
        with respx.mock:
            calendar_id = "primary"
            captured: list[dict] = []

            def capture(request, route):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "evt-plain"})

            respx.post(f"{_BASE}/calendars/{calendar_id}/events").mock(
                side_effect=capture
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Doctor appointment"))

        assert captured[0]["summary"] == "Doctor appointment"
        assert receipt.success is True

    @pytest.mark.asyncio
    async def test_send_returns_failure_receipt_on_api_error(self):
        with respx.mock:
            calendar_id = "primary"
            respx.post(f"{_BASE}/calendars/{calendar_id}/events").mock(
                return_value=httpx.Response(500, json={"error": "server error"})
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Test event"))

        assert receipt.success is False
        assert receipt.undoable is False


# ---------------------------------------------------------------------------
# undo()
# ---------------------------------------------------------------------------

class TestUndo:
    @pytest.mark.asyncio
    async def test_undo_calls_delete_event_with_stored_event_id(self):
        with respx.mock:
            respx.delete(f"{_BASE}/calendars/primary/events/evt-to-delete").mock(
                return_value=httpx.Response(204)
            )
            adapter = _make_adapter()
            receipt = DeliveryReceipt(
                channel="calendar",
                message_id="evt-to-delete",
                delivered_at="2026-01-01T00:00:00+00:00",
                success=True,
                undoable=True,
                undo_strategy="delete_event",
            )
            result = await adapter.undo(receipt)
        assert result is True

    @pytest.mark.asyncio
    async def test_undo_returns_false_when_not_undoable(self):
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="calendar",
            message_id="evt-x",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=False,
        )
        result = await adapter.undo(receipt)
        assert result is False

    @pytest.mark.asyncio
    async def test_undo_returns_false_when_no_message_id(self):
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="calendar",
            message_id=None,
            delivered_at="2026-01-01T00:00:00+00:00",
            success=False,
            undoable=True,
            undo_strategy="delete_event",
        )
        result = await adapter.undo(receipt)
        assert result is False


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok(self):
        adapter = _make_adapter()
        h = adapter.health()
        assert h["status"] == "ok"
        assert "nango_configured" in h
