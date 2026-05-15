"""Structural and behavioural tests for mithra/adapters/voice/wyoming.py."""

import asyncio
import inspect
import json

import pytest

from mithra.adapters.messaging import InputAdapter
from mithra.adapters.voice.wyoming import WyomingAdapter


class TestWyomingAdapterStructure:
    def test_is_subclass_of_input_adapter(self):
        assert issubclass(WyomingAdapter, InputAdapter)

    def test_default_port_is_10300(self):
        adapter = WyomingAdapter()
        assert adapter.port == 10300

    def test_default_host_is_0_0_0_0(self):
        adapter = WyomingAdapter()
        assert adapter.host == "0.0.0.0"

    def test_has_start_method(self):
        assert hasattr(WyomingAdapter, "start")
        assert callable(WyomingAdapter.start)

    def test_has_stop_method(self):
        assert hasattr(WyomingAdapter, "stop")
        assert callable(WyomingAdapter.stop)

    def test_has_normalise_method(self):
        assert hasattr(WyomingAdapter, "normalise")
        assert callable(WyomingAdapter.normalise)

    def test_has_acknowledge_method(self):
        assert hasattr(WyomingAdapter, "acknowledge")
        assert callable(WyomingAdapter.acknowledge)

    def test_has_health_method(self):
        assert hasattr(WyomingAdapter, "health")
        assert callable(WyomingAdapter.health)

    def test_acknowledge_is_async(self):
        assert inspect.iscoroutinefunction(WyomingAdapter.acknowledge)

    def test_start_is_async(self):
        assert inspect.iscoroutinefunction(WyomingAdapter.start)

    def test_stop_is_async(self):
        assert inspect.iscoroutinefunction(WyomingAdapter.stop)


class TestWyomingAdapterBehaviour:
    def test_normalise_returns_agent_request(self):
        from mithra.agent.interface import AgentRequest, InputChannel
        adapter = WyomingAdapter()
        request = adapter.normalise("person_a", "device:1", "turn on the lights")
        assert isinstance(request, AgentRequest)
        assert request.input_channel == InputChannel.VOICE
        assert request.text == "turn on the lights"
        assert request.user_id == "person_a"

    def test_health_returns_dict_with_status(self):
        adapter = WyomingAdapter()
        h = adapter.health()
        assert isinstance(h, dict)
        assert "status" in h
        assert "port" in h

    def test_normalise_sets_session_id_from_device(self):
        adapter = WyomingAdapter()
        request = adapter.normalise("person_a", "esp32_main", "hello")
        assert request.session_id == "voice:esp32_main"

    def test_normalise_unknown_device_falls_back_to_household(self):
        from mithra.adapters.messaging.telegram_input import UserIdentityResolver
        resolver = UserIdentityResolver({"members": [], "shared_devices": []})
        adapter = WyomingAdapter(identity_resolver=resolver)
        request = adapter.normalise("household", "unknown_device", "test")
        assert request.user_id == "household"

    def test_normalise_maps_known_device_to_user(self):
        from mithra.adapters.messaging.telegram_input import UserIdentityResolver
        config = {
            "members": [
                {"user_id": "person_a", "device_ids": ["phone_nikitha"], "telegram_ids": []}
            ],
            "shared_devices": [],
        }
        resolver = UserIdentityResolver(config)
        device_user = resolver.resolve_device("phone_nikitha")
        assert device_user == "person_a"


class _FakeWriter:
    def close(self):
        pass


def _make_reader(event: dict) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(event) + "\n").encode())
    reader.feed_eof()
    return reader


class TestWyomingHandleConnection:
    @pytest.mark.asyncio
    async def test_handle_connection_dispatches_transcript(self):
        received: list = []

        async def on_request(req):
            received.append(req)

        adapter = WyomingAdapter(on_request=on_request)
        event = {"type": "transcript", "device_id": "dev-1", "text": "play music", "session_id": ""}

        await adapter._handle_connection(_make_reader(event), _FakeWriter())
        await asyncio.sleep(0)  # yield so background create_task can run
        assert len(received) == 1
        assert received[0].text == "play music"

    @pytest.mark.asyncio
    async def test_handle_connection_unknown_device_maps_to_household(self):
        received: list = []

        async def on_request(req):
            received.append(req)

        from mithra.adapters.messaging.telegram_input import UserIdentityResolver
        resolver = UserIdentityResolver({"members": [], "shared_devices": []})
        adapter = WyomingAdapter(on_request=on_request, identity_resolver=resolver)

        event = {"type": "transcript", "device_id": "unknown_xyz", "text": "hello", "session_id": ""}
        await adapter._handle_connection(_make_reader(event), _FakeWriter())
        await asyncio.sleep(0)
        assert len(received) == 1
        assert received[0].user_id == "household"
