"""Tests for mithra/adapters/output/tts.py."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import json
import pytest
import respx

from mithra.adapters.messaging import ActionButton, OutboundMessage, OutputAdapter
from mithra.adapters.output.tts import TTSAdapter, VoiceConfirmationSession

_HA_URL = "http://homeassistant:8123"


def _make_adapter(**kwargs) -> TTSAdapter:
    return TTSAdapter(ha_url=_HA_URL, ha_token="test-token", **kwargs)


# ---------------------------------------------------------------------------
# VoiceConfirmationSession structure (existing tests)
# ---------------------------------------------------------------------------

class TestVoiceConfirmationSession:
    def test_has_device_id_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "device_id" in field_names

    def test_device_id_is_required(self):
        with pytest.raises(TypeError):
            VoiceConfirmationSession(
                session_id="s1",
                request_id="r1",
                action_index=0,
                user_id="person_a",
                expires_at="2026-01-01T01:00:00+00:00",
            )

    def test_has_session_id_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "session_id" in field_names

    def test_has_request_id_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "request_id" in field_names

    def test_has_action_index_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "action_index" in field_names

    def test_has_user_id_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "user_id" in field_names

    def test_has_expires_at_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "expires_at" in field_names

    def test_can_be_instantiated_with_all_fields(self):
        session = VoiceConfirmationSession(
            session_id="abc",
            request_id="req-1",
            action_index=0,
            user_id="person_a",
            device_id="ha-device-1",
            expires_at="2026-01-01T01:00:00+00:00",
        )
        assert session.device_id == "ha-device-1"
        assert session.user_id == "person_a"


# ---------------------------------------------------------------------------
# TTSAdapter structure (existing tests)
# ---------------------------------------------------------------------------

class TestTTSAdapter:
    def test_is_subclass_of_output_adapter(self):
        assert issubclass(TTSAdapter, OutputAdapter)

    def test_has_speak_method(self):
        assert hasattr(TTSAdapter, "speak")
        assert callable(TTSAdapter.speak)

    def test_speak_is_async(self):
        assert inspect.iscoroutinefunction(TTSAdapter.speak)

    def test_send_is_async(self):
        assert inspect.iscoroutinefunction(TTSAdapter.send)

    def test_send_stream_is_async(self):
        assert inspect.iscoroutinefunction(TTSAdapter.send_stream)

    def test_speak_signature_has_text_and_target_entity(self):
        sig = inspect.signature(TTSAdapter.speak)
        params = set(sig.parameters.keys())
        assert "text" in params
        assert "target_entity" in params

    def test_create_confirmation_session(self):
        adapter = _make_adapter()
        session = adapter.create_confirmation_session(
            request_id="req-1",
            action_index=0,
            user_id="person_a",
            device_id="ha-device-1",
        )
        assert isinstance(session, VoiceConfirmationSession)
        assert session.device_id == "ha-device-1"
        assert session.request_id == "req-1"

    def test_resolve_confirmation_returns_session(self):
        adapter = _make_adapter()
        session = adapter.create_confirmation_session(
            request_id="req-1", action_index=0,
            user_id="person_a", device_id="ha-device-1",
        )
        resolved = adapter.resolve_confirmation(session.session_id)
        assert resolved is not None
        assert resolved.session_id == session.session_id

    def test_resolve_confirmation_removes_session(self):
        adapter = _make_adapter()
        session = adapter.create_confirmation_session(
            request_id="req-1", action_index=0,
            user_id="person_a", device_id="ha-device-1",
        )
        adapter.resolve_confirmation(session.session_id)
        assert adapter.resolve_confirmation(session.session_id) is None

    def test_health_returns_dict(self):
        adapter = _make_adapter()
        h = adapter.health()
        assert isinstance(h, dict)
        assert "status" in h


# ---------------------------------------------------------------------------
# TTSAdapter behavioural tests (new)
# ---------------------------------------------------------------------------

class TestTTSAdapterSend:
    @pytest.mark.asyncio
    async def test_send_calls_ha_tts_speak_with_correct_entity(self):
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={})

        with respx.mock:
            respx.post(f"{_HA_URL}/api/services/tts/speak").mock(side_effect=capture)
            adapter = _make_adapter(media_player_entity="media_player.google_home_mini")
            await adapter.send("person_a", OutboundMessage(text="Dinner is ready."))

        assert len(captured) == 1
        assert "google_home_mini" in captured[0]["media_player_entity_id"]

    @pytest.mark.asyncio
    async def test_send_returns_success_receipt(self):
        with respx.mock:
            respx.post(f"{_HA_URL}/api/services/tts/speak").mock(
                return_value=httpx.Response(200, json={})
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello"))

        assert receipt.success is True
        assert receipt.channel == "tts"
        assert receipt.undoable is False

    @pytest.mark.asyncio
    async def test_send_returns_failure_when_ha_unreachable(self):
        with respx.mock:
            respx.post(f"{_HA_URL}/api/services/tts/speak").mock(
                side_effect=httpx.ConnectError("refused")
            )
            adapter = _make_adapter()
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello"))

        assert receipt.success is False
        assert receipt.error is not None

    @pytest.mark.asyncio
    async def test_undo_returns_false(self):
        from mithra.adapters.messaging import DeliveryReceipt
        adapter = _make_adapter()
        receipt = DeliveryReceipt(
            channel="tts", message_id="x", delivered_at="", success=True, undoable=False
        )
        assert await adapter.undo(receipt) is False


class TestTTSSendStream:
    @pytest.mark.asyncio
    async def test_send_stream_speaks_on_sentence_boundaries(self):
        spoken: list[str] = []
        adapter = _make_adapter()
        adapter.speak = AsyncMock(side_effect=lambda text, target_entity=None: spoken.append(text))

        async def _stream():
            yield "Hello there. "
            yield "How are you? "
            yield "Fine thanks."

        await adapter.send_stream("person_a", _stream())
        assert len(spoken) >= 2
        assert any("Hello there" in s for s in spoken)


class TestTTSSendWithActions:
    @pytest.mark.asyncio
    async def test_send_with_actions_creates_voice_confirmation_session(self):
        adapter = _make_adapter()
        adapter.speak = AsyncMock()

        await adapter.send_with_actions(
            "person_a",
            OutboundMessage(text="Send birthday message?", reply_to_id="req-1"),
            [ActionButton(label="Yes", action="confirm")],
        )

        session = adapter.get_pending_confirmation("person_a")
        assert session is not None
        assert session.user_id == "person_a"

    @pytest.mark.asyncio
    async def test_send_with_actions_appends_say_yes_or_no(self):
        spoken: list[str] = []
        adapter = _make_adapter()
        adapter.speak = AsyncMock(side_effect=lambda text, target_entity=None: spoken.append(text))

        await adapter.send_with_actions(
            "person_a",
            OutboundMessage(text="Confirm deletion?"),
            [ActionButton(label="Yes", action="confirm")],
        )

        assert any("Say yes or no" in s for s in spoken)


class TestGetPendingConfirmation:
    def test_get_pending_confirmation_returns_none_for_unknown_user(self):
        adapter = _make_adapter()
        assert adapter.get_pending_confirmation("person_a") is None

    def test_get_pending_confirmation_returns_none_for_expired_session(self):
        adapter = _make_adapter()
        past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        session = VoiceConfirmationSession(
            session_id="s1",
            request_id="r1",
            action_index=0,
            user_id="person_a",
            device_id="dev",
            expires_at=past,
        )
        adapter._pending["person_a"] = session
        assert adapter.get_pending_confirmation("person_a") is None

    def test_get_pending_confirmation_returns_active_session(self):
        adapter = _make_adapter()
        future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        session = VoiceConfirmationSession(
            session_id="s2",
            request_id="r2",
            action_index=0,
            user_id="person_a",
            device_id="dev",
            expires_at=future,
        )
        adapter._pending["person_a"] = session
        result = adapter.get_pending_confirmation("person_a")
        assert result is not None
        assert result.session_id == "s2"
