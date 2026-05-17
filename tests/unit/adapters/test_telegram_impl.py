"""Tests for TelegramInputAdapter and TelegramOutputAdapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from mithra.adapters.messaging import ActionButton, DeliveryReceipt, MessageFormat, OutboundMessage
from mithra.adapters.messaging.telegram_input import TelegramInputAdapter, UserIdentityResolver
from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter
from mithra.agent.interface import AgentRequest, InputChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_update(chat_id: int = 111, text: str = "Hello Mithra") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
        },
    }


def _callback_update(
    chat_id: int = 111,
    action: str = "confirm",
    request_id: str = "req-1",
    action_index: int = 0,
) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cq-1",
            "message": {"chat": {"id": chat_id}, "message_id": 99},
            "data": json.dumps(
                {"action": action, "request_id": request_id, "action_index": action_index}
            ),
        },
    }


def _document_update(chat_id: int = 111) -> dict:
    return {
        "update_id": 3,
        "message": {
            "message_id": 43,
            "chat": {"id": chat_id, "type": "private"},
            "document": {"file_id": "file-abc", "file_name": "receipt.pdf"},
        },
    }


def _voice_update(chat_id: int = 111) -> dict:
    return {
        "update_id": 4,
        "message": {
            "message_id": 44,
            "chat": {"id": chat_id, "type": "private"},
            "voice": {"file_id": "voice-xyz", "duration": 3},
        },
    }


def _make_input_adapter(**kwargs) -> TelegramInputAdapter:
    return TelegramInputAdapter(bot_token="test-token", **kwargs)


def _make_output_adapter(http_client=None) -> TelegramOutputAdapter:
    adapter = TelegramOutputAdapter(bot_token="test-token", http_client=http_client)
    adapter.register_chat("person_a", 111)
    return adapter


# ---------------------------------------------------------------------------
# UserIdentityResolver
# ---------------------------------------------------------------------------

class TestUserIdentityResolver:
    def test_known_telegram_id_returns_correct_user_id(self):
        config = {
            "members": [
                {"user_id": "person_a", "telegram_ids": [111]},
                {"user_id": "person_b", "telegram_ids": [222]},
            ]
        }
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(111) == "person_a"
        assert resolver.resolve_telegram(222) == "person_b"

    def test_unknown_telegram_id_returns_household(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(999) == "household"

    def test_empty_config_returns_household(self):
        resolver = UserIdentityResolver({})
        assert resolver.resolve_telegram(42) == "household"

    def test_telegram_ids_as_list(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [123456789]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "person_a"

    def test_telegram_ids_as_bare_int(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": 123456789}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "person_a"

    def test_telegram_ids_as_empty_list(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": []}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "household"

    def test_telegram_ids_as_none(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": None}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "household"


# ---------------------------------------------------------------------------
# TelegramInputAdapter.normalise()
# ---------------------------------------------------------------------------

class TestNormaliseText:
    def test_text_message_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(text="Update pantry"), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.text == "Update pantry"

    def test_text_message_sets_input_channel(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(), "telegram")
        assert result.input_channel == InputChannel.TELEGRAM

    def test_text_message_sets_session_id(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=555), "telegram")
        assert result.session_id == "tg:555"

    def test_text_message_resolves_user_id(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result.user_id == "person_a"

    def test_unknown_chat_id_defaults_to_household(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=9999), "telegram")
        assert result.user_id == "household"


class TestNormaliseCallback:
    def test_callback_query_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(), "telegram")
        assert isinstance(result, AgentRequest)

    def test_callback_query_sets_confirmation_signal(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(
            _callback_update(action="confirm", request_id="r1", action_index=0),
            "telegram",
        )
        assert result.signal["confirmation"] == "confirm"
        assert result.signal["request_id"] == "r1"
        assert result.signal["action_index"] == 0

    def test_callback_query_reject_action(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(
            _callback_update(action="reject", request_id="r2", action_index=1),
            "telegram",
        )
        assert result.signal["confirmation"] == "reject"

    def test_callback_query_text_is_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(), "telegram")
        assert result.text is None


class TestNormaliseDocument:
    def test_document_returns_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_document_update(), "telegram")
        assert result is None

    def test_voice_message_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_voice_update(), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.signal.get("voice_bytes_pending") is True

    def test_unknown_update_returns_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise({"update_id": 99}, "telegram")
        assert result is None


# ---------------------------------------------------------------------------
# TelegramOutputAdapter
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.mark.asyncio
    async def test_send_returns_delivery_receipt(self):
        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                return_value=httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 77}}
                )
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            adapter.register_chat("person_a", 111)
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello!"))
        assert isinstance(receipt, DeliveryReceipt)
        assert receipt.success is True
        assert receipt.message_id == "77"
        assert receipt.channel == "telegram"

    @pytest.mark.asyncio
    async def test_send_with_actions_builds_inline_keyboard(self):
        captured: list[dict] = []

        def capture(request, route):
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=capture
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            adapter.register_chat("person_a", 111)
            await adapter.send_with_actions(
                "person_a",
                OutboundMessage(text="Confirm?"),
                [ActionButton(label="Yes", action="confirm", payload={"x": 1})],
            )

        assert len(captured) == 1
        keyboard = captured[0]["reply_markup"]["inline_keyboard"]
        assert len(keyboard[0]) == 1
        btn = keyboard[0][0]
        assert btn["text"] == "Yes"
        data = json.loads(btn["callback_data"])
        assert data["action"] == "confirm"

    @pytest.mark.asyncio
    async def test_undo_with_delete_message_strategy(self):
        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/deleteMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": True})
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            receipt = DeliveryReceipt(
                channel="telegram",
                message_id="111:42",
                delivered_at="2026-01-01T00:00:00+00:00",
                success=True,
                undoable=True,
                undo_strategy="delete_message",
            )
            result = await adapter.undo(receipt)
        assert result is True

    @pytest.mark.asyncio
    async def test_undo_returns_false_when_not_undoable(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        receipt = DeliveryReceipt(
            channel="telegram",
            message_id="99",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=False,
        )
        result = await adapter.undo(receipt)
        assert result is False

    def test_register_chat_stores_mapping(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        adapter.register_chat("person_a", 123456)
        assert adapter._get_chat_id("person_a") == 123456

    def test_health_returns_ok(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        h = adapter.health()
        assert h["status"] == "ok"
