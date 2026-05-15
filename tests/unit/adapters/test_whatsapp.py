"""Tests for WhatsAppAdapter -- outbound Baileys bridge adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from mithra.adapters.messaging import ActionButton, DeliveryReceipt, OutboundMessage
from mithra.adapters.messaging.whatsapp import WhatsAppAdapter

_BRIDGE = "http://whatsapp-bridge:3000"


def _make_adapter(**kwargs) -> WhatsAppAdapter:
    return WhatsAppAdapter(bridge_url=_BRIDGE, **kwargs)


class TestSend:
    @pytest.mark.asyncio
    async def test_send_calls_bridge_send_with_phone_and_text(self):
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"message_id": "wa-001"})

        with respx.mock:
            respx.post(f"{_BRIDGE}/send").mock(side_effect=capture)
            adapter = _make_adapter()
            adapter.register_phone("person_a", "+15551234567")
            await adapter.send("person_a", OutboundMessage(text="Happy birthday!"))

        assert len(captured) == 1
        assert captured[0]["phone"] == "+15551234567"
        assert captured[0]["text"] == "Happy birthday!"

    @pytest.mark.asyncio
    async def test_send_returns_delivery_receipt_on_success(self):
        with respx.mock:
            respx.post(f"{_BRIDGE}/send").mock(
                return_value=httpx.Response(200, json={"message_id": "wa-002"})
            )
            adapter = _make_adapter()
            adapter.register_phone("person_a", "+15551234567")
            receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))

        assert isinstance(receipt, DeliveryReceipt)
        assert receipt.success is True
        assert receipt.channel == "whatsapp"

    @pytest.mark.asyncio
    async def test_send_returns_failure_on_bridge_error(self):
        with respx.mock:
            respx.post(f"{_BRIDGE}/send").mock(
                return_value=httpx.Response(500, json={"error": "bridge down"})
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))

        assert receipt.success is False
        assert receipt.error is not None

    @pytest.mark.asyncio
    async def test_send_undoable_is_false(self):
        with respx.mock:
            respx.post(f"{_BRIDGE}/send").mock(
                return_value=httpx.Response(200, json={"message_id": "wa-003"})
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Hi"))

        assert receipt.undoable is False
        assert receipt.undo_strategy is None


class TestSendWithActions:
    @pytest.mark.asyncio
    async def test_send_with_actions_lists_options_as_numbered_text(self):
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"message_id": "wa-act"})

        with respx.mock:
            respx.post(f"{_BRIDGE}/send").mock(side_effect=capture)
            adapter = _make_adapter()
            adapter.register_phone("person_a", "+15551234567")
            await adapter.send_with_actions(
                "person_a",
                OutboundMessage(text="Send birthday message?"),
                [
                    ActionButton(label="Yes", action="confirm"),
                    ActionButton(label="No", action="reject"),
                ],
            )

        body = captured[0]["text"]
        assert "1. Yes" in body
        assert "2. No" in body
        assert "Send birthday message?" in body


class TestUndo:
    @pytest.mark.asyncio
    async def test_undo_always_returns_false(self):
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="whatsapp",
            message_id="wa-x",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=True,
        )
        result = await adapter.undo(receipt)
        assert result is False

    @pytest.mark.asyncio
    async def test_undo_returns_false_even_when_undoable_true(self):
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="whatsapp",
            message_id="wa-y",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=True,
        )
        assert await adapter.undo(receipt) is False


class TestRegisterPhone:
    def test_register_phone_stores_phone_for_user(self):
        adapter = _make_adapter()
        adapter.register_phone("person_a", "+15559876543")
        assert adapter._get_phone("person_a") == "+15559876543"

    def test_unregistered_user_falls_back_to_user_id(self):
        adapter = _make_adapter()
        # No phone registered -- user_id is returned as-is
        assert adapter._get_phone("person_b") == "person_b"


class TestHealth:
    def test_health_returns_whatsapp_connected_from_bridge(self):
        with respx.mock:
            respx.get(f"{_BRIDGE}/health").mock(
                return_value=httpx.Response(200, json={"connected": True})
            )
            adapter = _make_adapter()
            h = adapter.health()

        assert h["whatsapp_connected"] is True
        assert h["status"] == "ok"

    def test_health_returns_error_when_bridge_unreachable(self):
        with respx.mock:
            respx.get(f"{_BRIDGE}/health").mock(
                side_effect=httpx.ConnectError("refused")
            )
            adapter = _make_adapter()
            h = adapter.health()

        assert h["status"] == "error"
        assert h["whatsapp_connected"] is False
