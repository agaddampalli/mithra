"""Tests for mithra/adapters/output/push.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mithra.adapters.messaging import DeliveryReceipt, OutboundMessage, OutputAdapter
from mithra.adapters.output.push import PushAdapter


def _make_adapter(**kwargs) -> PushAdapter:
    return PushAdapter(vapid_private_key="fake-key", vapid_email="test@example.com", **kwargs)


_SUB = {
    "endpoint": "https://push.example.com/sub/abc",
    "keys": {"p256dh": "fake-p256dh", "auth": "fake-auth"},
}


class TestPushAdapterStructure:
    def test_is_subclass_of_output_adapter(self):
        assert issubclass(PushAdapter, OutputAdapter)

    def test_has_register_subscription(self):
        assert callable(getattr(PushAdapter, "register_subscription", None))

    def test_health_returns_dict(self):
        adapter = _make_adapter()
        h = adapter.health()
        assert isinstance(h, dict)
        assert "status" in h

    def test_health_reports_subscription_count(self):
        adapter = _make_adapter()
        adapter.register_subscription("person_a", _SUB)
        h = adapter.health()
        assert h["registered_subscriptions"] == 1

    def test_health_reports_pywebpush_available(self):
        adapter = _make_adapter()
        h = adapter.health()
        assert "pywebpush_available" in h


class TestSendWithoutSubscription:
    @pytest.mark.asyncio
    async def test_send_returns_failure_when_no_subscription(self):
        with patch("mithra.adapters.output.push._webpush_available", return_value=True):
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello"))

        assert receipt.success is False
        assert receipt.error is not None
        assert "person_a" in receipt.error

    @pytest.mark.asyncio
    async def test_send_returns_failure_when_pywebpush_missing(self):
        with patch("mithra.adapters.output.push._webpush_available", return_value=False):
            adapter = _make_adapter()
            adapter.register_subscription("person_a", _SUB)
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello"))

        assert receipt.success is False
        assert receipt.channel == "push"
        assert "pywebpush" in (receipt.error or "").lower()


class TestRegisterSubscription:
    def test_register_subscription_stores_entry(self):
        adapter = _make_adapter()
        adapter.register_subscription("person_a", _SUB)
        assert adapter._subscriptions["person_a"] == _SUB

    def test_register_subscription_replaces_existing(self):
        adapter = _make_adapter()
        adapter.register_subscription("person_a", _SUB)
        new_sub = {**_SUB, "endpoint": "https://push.example.com/sub/xyz"}
        adapter.register_subscription("person_a", new_sub)
        assert adapter._subscriptions["person_a"]["endpoint"] == "https://push.example.com/sub/xyz"


class TestUndo:
    @pytest.mark.asyncio
    async def test_undo_always_returns_false(self):
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="push",
            message_id="p-1",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=False,
        )
        assert await adapter.undo(receipt) is False


class TestGracefulDegradation:
    def test_health_shows_degraded_when_pywebpush_missing(self):
        with patch("mithra.adapters.output.push._webpush_available", return_value=False):
            adapter = _make_adapter()
            h = adapter.health()

        assert h["status"] == "degraded"
        assert h["pywebpush_available"] is False

    def test_health_shows_ok_when_pywebpush_available(self):
        with patch("mithra.adapters.output.push._webpush_available", return_value=True):
            adapter = _make_adapter()
            h = adapter.health()

        assert h["status"] == "ok"
        assert h["pywebpush_available"] is True

    @pytest.mark.asyncio
    async def test_send_stream_returns_failure_without_subscription(self):
        with patch("mithra.adapters.output.push._webpush_available", return_value=True):
            adapter = _make_adapter()

            async def _stream():
                yield "Hello"

            receipt = await adapter.send_stream("person_a", _stream())

        assert receipt.success is False
