"""Structural and behavioural tests for mithra/adapters/voice/wyoming.py."""

import inspect

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
