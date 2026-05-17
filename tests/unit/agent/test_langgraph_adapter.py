"""Tests for LangGraphAdapter — LangGraph implementation of AgentInterface."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from mithra.agent.interface import (
    AgentIntent,
    AgentRequest,
    AgentResult,
    AgentStreamEvent,
    InputChannel,
    MCPTool,
    StreamEventType,
)
from mithra.agent.langgraph_adapter import LangGraphAdapter
from mithra.routing.llm_router import IntentClassification, LLMRouter, RouterHealth


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockLLMRouter(LLMRouter):
    """Test double for LLMRouter — returns configurable responses."""

    def __init__(
        self,
        intent: str = AgentIntent.FOOD_UPDATE_PANTRY,
        confidence: float = 0.90,
        classify_returns_none: bool = False,
    ) -> None:
        self._intent = intent
        self._confidence = confidence
        self._classify_returns_none = classify_returns_none

    def complete(self, task_type, domain, messages, tools=None, stream=False):
        resp = MagicMock()
        resp.content = "Here is my response."
        resp.model_used = "phi3-mini"
        resp.route_taken = "local"
        resp.task_type = task_type
        resp.prompt_tokens = 10
        resp.completion_tokens = 5
        resp.latency_ms = 50
        resp.success = True
        resp.error = None
        resp.sanitisation = None
        resp.fallback_triggered = False
        resp.fallback_reason = None
        return resp

    def classify_intent(self, text, context):
        if self._classify_returns_none:
            return None
        return IntentClassification(
            intent=self._intent,
            domain=self._intent.split(".")[0],
            confidence=self._confidence,
        )

    def get_usage(self, period="month"):
        return MagicMock()

    def health(self):
        return RouterHealth(
            litellm_reachable=True,
            cloud_model_available=True,
            local_model_available=True,
        )


def _make_adapter(**kwargs) -> LangGraphAdapter:
    router = MockLLMRouter(**kwargs)
    return LangGraphAdapter(llm_router=router)


def _make_request(text: str = "update pantry", domain_hint: str = "food") -> AgentRequest:
    return AgentRequest(
        user_id="person_a",
        device_id="phone_a",
        input_channel=InputChannel.TELEGRAM,
        text=text,
        domain_hint=domain_hint,
        request_id="req-test-1",
    )


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------

class TestLangGraphAdapterStructure:
    def test_implements_agent_interface(self):
        from mithra.agent.interface import AgentInterface
        adapter = _make_adapter()
        assert isinstance(adapter, AgentInterface)

    def test_run_is_callable(self):
        assert callable(LangGraphAdapter.run)

    def test_run_stream_returns_generator(self):
        adapter = _make_adapter()
        request = _make_request()
        gen = adapter.run_stream(request)
        assert hasattr(gen, "__next__") or inspect.isgenerator(gen)

    def test_register_tool_stores_tool(self):
        adapter = _make_adapter()
        tool = MCPTool(
            name="food.update_pantry",
            domain="food",
            description="Update the pantry",
            parameters={},
            handler=lambda: {"response": "Pantry updated."},
            autonomy_default=1,
            requires_user_id=False,
            tier=3,
        )
        adapter.register_tool(tool)
        assert "food.update_pantry" in adapter.health()["registered_tools"]


# ---------------------------------------------------------------------------
# run() — happy path
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_returns_agent_result(self):
        adapter = _make_adapter()
        result = adapter.run(_make_request())
        assert isinstance(result, AgentResult)

    def test_run_sets_request_id(self):
        adapter = _make_adapter()
        result = adapter.run(_make_request())
        assert result.request_id == "req-test-1"

    def test_run_generates_request_id_when_missing(self):
        adapter = _make_adapter()
        request = AgentRequest(
            user_id="person_a",
            device_id="phone_a",
            input_channel=InputChannel.TELEGRAM,
            text="hello",
        )
        result = adapter.run(request)
        assert result.request_id != ""

    def test_run_high_confidence_returns_intent_from_router(self):
        adapter = _make_adapter(
            intent=AgentIntent.FOOD_UPDATE_PANTRY, confidence=0.92
        )
        result = adapter.run(_make_request())
        assert result.intent == AgentIntent.FOOD_UPDATE_PANTRY

    def test_run_low_confidence_returns_clarification(self):
        adapter = _make_adapter(confidence=0.50)
        result = adapter.run(_make_request())
        assert result.clarification is not None

    def test_run_returns_clarification_when_router_returns_none(self):
        adapter = _make_adapter(classify_returns_none=True)
        result = adapter.run(_make_request())
        assert result.clarification is not None

    def test_run_with_registered_tool_executes_handler(self):
        adapter = _make_adapter(
            intent="food.update_pantry", confidence=0.95
        )
        handler_called = {"value": False}

        def handler():
            handler_called["value"] = True
            return {"response": "Pantry updated."}

        tool = MCPTool(
            name="food.update_pantry",
            domain="food",
            description="Update the pantry",
            parameters={},
            handler=handler,
            autonomy_default=1,
            requires_user_id=False,
            tier=3,
        )
        adapter.register_tool(tool)
        result = adapter.run(_make_request())
        assert handler_called["value"] is True
        assert result.response_text == "Pantry updated."

    def test_run_unknown_tool_falls_through_to_llm(self):
        # No tool registered for this intent — LLM handles it, no error returned
        adapter = _make_adapter(
            intent="food.nonexistent_tool", confidence=0.95
        )
        result = adapter.run(_make_request())
        assert result.success is True
        assert result.error is None
        assert result.response_text

    def test_run_graph_exception_returns_failure(self):
        adapter = _make_adapter()
        with patch.object(adapter, "_graph") as mock_graph:
            mock_graph.invoke.side_effect = RuntimeError("graph boom")
            result = adapter.run(_make_request())
        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# run_stream()
# ---------------------------------------------------------------------------

class TestRunStream:
    def test_run_stream_yields_done_event(self):
        adapter = _make_adapter()
        events = list(adapter.run_stream(_make_request()))
        event_types = [e.event_type for e in events]
        assert StreamEventType.DONE in event_types

    def test_run_stream_yields_text_delta_when_response_present(self):
        adapter = _make_adapter(confidence=0.95)
        events = list(adapter.run_stream(_make_request()))
        text_events = [e for e in events if e.event_type == StreamEventType.TEXT_DELTA]
        assert len(text_events) >= 1


# ---------------------------------------------------------------------------
# run_briefing()
# ---------------------------------------------------------------------------

class TestRunBriefing:
    def test_run_briefing_returns_agent_result(self):
        adapter = _make_adapter()
        result = adapter.run_briefing("person_a")
        assert isinstance(result, AgentResult)

    def test_run_briefing_sets_user_id(self):
        adapter = _make_adapter()
        # run() is used internally; just verify it doesn't raise
        result = adapter.run_briefing("person_a")
        assert result.request_id != ""


# ---------------------------------------------------------------------------
# confirm() / reject()
# ---------------------------------------------------------------------------

class TestConfirmReject:
    def test_confirm_returns_success(self):
        adapter = _make_adapter()
        result = adapter.confirm("req-1", 0, "person_a")
        assert result.success is True
        assert "Confirmed" in result.response_text

    def test_reject_returns_success(self):
        adapter = _make_adapter()
        result = adapter.reject("req-1", 0, "person_a")
        assert result.success is True
        assert "Cancelled" in result.response_text


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_ok_when_llm_reachable(self):
        adapter = _make_adapter()
        h = adapter.health()
        assert h["status"] == "ok"

    def test_health_returns_degraded_when_llm_unreachable(self):
        adapter = _make_adapter()
        adapter._llm_router.health = lambda: RouterHealth(
            litellm_reachable=False,
            cloud_model_available=False,
            local_model_available=False,
        )
        h = adapter.health()
        assert h["status"] == "degraded"

    def test_health_lists_registered_tools(self):
        adapter = _make_adapter()
        tool = MCPTool(
            name="home.log_maintenance",
            domain="home",
            description="Log maintenance",
            parameters={},
            handler=lambda: {},
            autonomy_default=1,
            requires_user_id=False,
            tier=3,
        )
        adapter.register_tool(tool)
        h = adapter.health()
        assert "home.log_maintenance" in h["registered_tools"]
