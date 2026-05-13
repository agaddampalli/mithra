"""Structural and behavioural tests for mithra/adapters/messaging/__init__.py."""

import pytest

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    InputAdapter,
    MessageFormat,
    OutboundMessage,
    OutputAdapter,
)


class TestMessageFormat:
    def test_text_exists(self):
        assert MessageFormat.TEXT == "text"

    def test_markdown_exists(self):
        assert MessageFormat.MARKDOWN == "markdown"

    def test_voice_exists(self):
        assert MessageFormat.VOICE == "voice"

    def test_card_exists(self):
        assert MessageFormat.CARD == "card"

    def test_exactly_four_formats(self):
        assert len(list(MessageFormat)) == 4


class TestOutboundMessage:
    def test_priority_defaults_to_normal_string(self):
        msg = OutboundMessage(text="hello")
        assert msg.priority == "normal"
        assert isinstance(msg.priority, str)

    def test_domain_defaults_to_none(self):
        msg = OutboundMessage(text="hello")
        assert msg.domain is None

    def test_domain_accepts_string(self):
        msg = OutboundMessage(text="hello", domain="food")
        assert msg.domain == "food"

    def test_reply_to_id_defaults_to_none(self):
        msg = OutboundMessage(text="hello")
        assert msg.reply_to_id is None

    def test_reply_to_id_accepts_string(self):
        msg = OutboundMessage(text="hello", reply_to_id="msg-123")
        assert msg.reply_to_id == "msg-123"

    def test_format_defaults_to_text(self):
        msg = OutboundMessage(text="hello")
        assert msg.format == MessageFormat.TEXT


class TestActionButton:
    def test_action_field_exists(self):
        btn = ActionButton(label="Confirm", action="confirm")
        assert btn.action == "confirm"

    def test_payload_defaults_to_empty_dict(self):
        btn = ActionButton(label="Confirm", action="confirm")
        assert btn.payload == {}
        assert isinstance(btn.payload, dict)

    def test_action_values(self):
        for action in ("confirm", "reject", "modify"):
            btn = ActionButton(label="X", action=action)
            assert btn.action == action

    def test_payload_independence_between_instances(self):
        a = ActionButton(label="A", action="confirm")
        b = ActionButton(label="B", action="reject")
        a.payload["key"] = "val"
        assert "key" not in b.payload


class TestDeliveryReceipt:
    def test_message_id_is_optional(self):
        # Can be constructed with None
        r = DeliveryReceipt(channel="telegram", delivered_at="2026-01-01T00:00:00+00:00", success=True)
        assert r.message_id is None

    def test_message_id_accepts_string(self):
        r = DeliveryReceipt(
            channel="telegram",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            message_id="12345",
        )
        assert r.message_id == "12345"

    def test_undoable_defaults_to_false(self):
        r = DeliveryReceipt(channel="telegram", delivered_at="now", success=True)
        assert r.undoable is False

    def test_undo_strategy_defaults_to_none(self):
        r = DeliveryReceipt(channel="telegram", delivered_at="now", success=True)
        assert r.undo_strategy is None

    def test_undo_strategy_accepts_string(self):
        r = DeliveryReceipt(
            channel="telegram", delivered_at="now", success=True,
            undo_strategy="delete_message",
        )
        assert r.undo_strategy == "delete_message"

    def test_error_defaults_to_none(self):
        r = DeliveryReceipt(channel="telegram", delivered_at="now", success=True)
        assert r.error is None

    def test_error_accepts_string(self):
        r = DeliveryReceipt(
            channel="telegram", delivered_at="now", success=False, error="timeout"
        )
        assert r.error == "timeout"


class TestInputAdapterIsAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            InputAdapter()  # type: ignore[abstract]


class TestOutputAdapterIsAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            OutputAdapter()  # type: ignore[abstract]
