"""Structural and behavioural tests for mithra/adapters/output/tts.py."""

import dataclasses
import inspect

import pytest

from mithra.adapters.messaging import OutputAdapter
from mithra.adapters.output.tts import TTSAdapter, VoiceConfirmationSession


class TestVoiceConfirmationSession:
    def test_has_device_id_field(self):
        field_names = {f.name for f in dataclasses.fields(VoiceConfirmationSession)}
        assert "device_id" in field_names

    def test_device_id_is_required(self):
        # device_id has no default — omitting it must raise TypeError
        with pytest.raises(TypeError):
            VoiceConfirmationSession(
                session_id="s1",
                request_id="r1",
                action_index=0,
                user_id="person_a",
                # device_id omitted
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
        adapter = TTSAdapter(ha_url="http://localhost:8123", ha_token="token")
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
        adapter = TTSAdapter(ha_url="http://localhost:8123", ha_token="token")
        session = adapter.create_confirmation_session(
            request_id="req-1", action_index=0,
            user_id="person_a", device_id="ha-device-1",
        )
        resolved = adapter.resolve_confirmation(session.session_id)
        assert resolved is not None
        assert resolved.session_id == session.session_id

    def test_resolve_confirmation_removes_session(self):
        adapter = TTSAdapter(ha_url="http://localhost:8123", ha_token="token")
        session = adapter.create_confirmation_session(
            request_id="req-1", action_index=0,
            user_id="person_a", device_id="ha-device-1",
        )
        adapter.resolve_confirmation(session.session_id)
        # Second resolve should return None (already consumed)
        assert adapter.resolve_confirmation(session.session_id) is None

    def test_health_returns_dict(self):
        adapter = TTSAdapter(ha_url="http://localhost:8123", ha_token="token")
        h = adapter.health()
        assert isinstance(h, dict)
        assert "status" in h
