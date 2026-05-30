"""Tests for 5 root cause fixes — confirmation flow, timezone helper,
force tool for delete intents, text yes/no triggers confirmation,
and human-readable confirmation message generation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mithra.agent.interface import (
    AgentAction,
    AgentIntent,
    AgentRequest,
    AgentResult,
    InputChannel,
    MCPTool,
)
from mithra.agent.langgraph_adapter import LangGraphAdapter
from mithra.autonomy.default_engine import DefaultAutonomyEngine
from mithra.autonomy.engine import AutonomyLevel
from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult
from mithra.routing.llm_router import IntentClassification, LLMRouter, RouterHealth
from mithra.storage.local import LocalStorageAdapter
from mithra.autonomy.action_log import ActionLog


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class MockLLMRouter(LLMRouter):
    """Minimal LLMRouter double for agent tests."""

    def __init__(self, intent: str = AgentIntent.FOOD_UPDATE_PANTRY, confidence: float = 0.90) -> None:
        self._intent = intent
        self._confidence = confidence

    def complete(self, task_type, domain, messages, tools=None, stream=False):
        resp = MagicMock()
        resp.content = "LLM response text."
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

    def call_with_tools(self, messages, tools, model=None, tool_choice="auto"):
        return {"name": None, "content": "LLM text response."}

    def classify_intent(self, text, context):
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
    return LangGraphAdapter(llm_router=MockLLMRouter(**kwargs))


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))


@pytest.fixture
def action_log(storage):
    return ActionLog(storage)


@pytest.fixture
def config_path(tmp_path):
    import yaml
    cfg = {
        "default_level": 1,
        "never_actions": [],
        "domain_overrides": {"time": 1},
    }
    path = tmp_path / "autonomy.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return str(path)


@pytest.fixture
def engine(storage, action_log, config_path):
    return DefaultAutonomyEngine(storage, action_log, config_path)


def _surface_action(action_type: str = "calendar_create_event", payload: dict | None = None) -> AgentAction:
    return AgentAction(
        action_type=action_type,
        domain="time",
        payload=payload or {"title": "Dentist", "start_time": "2026-06-01T15:00:00"},
        output_channel="telegram",
        autonomy_level=1,
        reversibility="full",
    )


# ===========================================================================
# FIX 1: _node_generate_response returns summary for pending_actions
# ===========================================================================


class TestFix1PendingActionsResponse:
    """_node_generate_response must return human-readable text when pending_actions
    are present — not fall through to 'I'm not sure how to help'.
    """

    def _make_state_with_pending(self, pending_actions: list[dict]) -> dict:
        request = AgentRequest(
            user_id="person_a",
            device_id="phone_a",
            input_channel=InputChannel.TELEGRAM,
            text="add dentist 3pm",
            request_id="req-fix1-test",
        )
        return {
            "request": request,
            "pending_actions": pending_actions,
            "tool_result": None,
            "error": None,
            "domain_bundles": [],
            "llm_response": None,
            "intent": "time.create_event",
            "tool_calls": [],
        }

    def test_pending_with_title_and_start_returns_create_summary(self):
        adapter = _make_adapter()
        state = self._make_state_with_pending([{
            "action_type": "calendar_create_event",
            "payload": {"title": "Dentist", "start_time": "2026-06-01T15:00:00"},
        }])
        result = adapter._node_generate_response(state)
        text = result["response_text"]
        assert "Dentist" in text
        assert "I'm not sure" not in text

    def test_pending_with_event_query_returns_delete_summary(self):
        adapter = _make_adapter()
        state = self._make_state_with_pending([{
            "action_type": "calendar_delete_event",
            "payload": {"event_query": "dentist appointment"},
        }])
        result = adapter._node_generate_response(state)
        text = result["response_text"]
        assert "dentist appointment" in text
        assert "I'm not sure" not in text

    def test_pending_with_prebuilt_summary_uses_it_directly(self):
        adapter = _make_adapter()
        state = self._make_state_with_pending([{
            "action_type": "calendar_create_event",
            "payload": {},
            "summary": "Book my dentist for Monday 3pm",
        }])
        result = adapter._node_generate_response(state)
        assert result["response_text"] == "Book my dentist for Monday 3pm"

    def test_pending_without_recognisable_payload_uses_tool_name_fallback(self):
        adapter = _make_adapter()
        state = self._make_state_with_pending([{
            "action_type": "calendar_custom_thing",
            "payload": {},
        }])
        result = adapter._node_generate_response(state)
        text = result["response_text"]
        # Must not expose raw internal name with underscores
        assert text != "calendar_custom_thing"
        assert "I'm not sure" not in text


# ===========================================================================
# FIX 2: _make_event() helper — timezone on every creation tool
# ===========================================================================


class TestFix2MakeEventHelper:
    """_make_event() must produce timezone-aware gcsa Events for every caller."""

    def test_make_event_sets_timezone_field(self):
        from mithra.domains.time.tools import _make_event
        start = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
        event = _make_event("Dentist", start, 60, "America/Chicago")
        assert hasattr(event, "timezone")
        assert event.timezone == "America/Chicago"

    def test_make_event_end_is_start_plus_duration(self):
        from mithra.domains.time.tools import _make_event
        # 10:00 UTC → Europe/London BST (UTC+1) = 11:00; +90 min = 12:30 naive
        start = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
        event = _make_event("Meeting", start, 90, "Europe/London")
        # event.start/end are naive local time — no tzinfo
        assert event.start.tzinfo is None
        assert event.end.tzinfo is None
        assert event.start.hour == 11
        assert event.end.hour == 12
        assert event.end.minute == 30

    def test_make_event_localises_naive_datetime(self):
        from mithra.domains.time.tools import _make_event
        naive_start = datetime(2026, 6, 1, 15, 0)  # no tzinfo
        event = _make_event("Gym", naive_start, 60, "America/New_York")
        # event.start is stripped to naive local time — tzinfo must be None
        assert event.start.tzinfo is None
        assert event.end.tzinfo is None
        assert event.start.hour == 15

    def test_make_event_title_set_as_summary(self):
        from mithra.domains.time.tools import _make_event
        start = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        event = _make_event("Weekly Standup", start, 30, "UTC")
        assert event.summary == "Weekly Standup"

    def test_make_event_description_none_when_empty(self):
        from mithra.domains.time.tools import _make_event
        start = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        event = _make_event("Standup", start, 30, "UTC", description="")
        assert event.description is None

    def test_create_event_tool_uses_make_event_no_end_dt_error(self):
        """CalendarCreateEventTool._run() must not raise NameError for end_dt."""
        from mithra.domains.time.tools import CalendarCreateEventTool
        from tests.unit.domains.time.conftest import MockCalendarService
        from mithra.domains.context import DomainContext

        oauth = MagicMock()
        oauth._store.load.return_value = {
            "access_token": "token",
            "refresh_token": "refresh",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        ctx = DomainContext(
            user_id="person_a",
            household_id="hh_001",
            household_timezone="America/Chicago",
            oauth_manager=oauth,
            storage_adapter=None,
            file_router=None,
            member_registry=None,
            file_search=None,
            request_id="req_test",
            channel="telegram",
            language="en-US",
            autonomy_override=None,
        )
        tool = CalendarCreateEventTool(context=ctx)
        svc = MockCalendarService()
        with patch("mithra.domains.time.tools.GoogleCalendarService", return_value=svc):
            result = tool._run(
                title="Dentist",
                start_time="2026-06-01T15:00:00",
                duration_minutes=60,
            )
        assert result.success is True
        assert result.data["title"] == "Dentist"

    def test_create_recurring_tool_uses_make_event(self):
        """CalendarCreateRecurringTool._run() must not raise AttributeError for timezone."""
        from mithra.domains.time.tools import CalendarCreateRecurringTool
        from tests.unit.domains.time.conftest import MockCalendarService
        from mithra.domains.context import DomainContext

        oauth = MagicMock()
        oauth._store.load.return_value = {
            "access_token": "token",
            "refresh_token": "refresh",
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        ctx = DomainContext(
            user_id="person_a",
            household_id="hh_001",
            household_timezone="America/Chicago",
            oauth_manager=oauth,
            storage_adapter=None,
            file_router=None,
            member_registry=None,
            file_search=None,
            request_id="req_rec_001",
            channel="telegram",
            language="en-US",
            autonomy_override=None,
        )
        tool = CalendarCreateRecurringTool(context=ctx)
        svc = MockCalendarService()
        with patch("mithra.domains.time.tools.GoogleCalendarService", return_value=svc):
            result = tool._run(
                title="Gym",
                start_time="2026-06-01T07:00:00",
                duration_minutes=60,
                frequency="weekly",
                by_week_day="MO",
            )
        assert result.success is True
        assert result.data["title"] == "Gym"


# ===========================================================================
# FIX 3: tool_choice forces calendar_delete_event for cancel/delete text
# ===========================================================================


class TestFix3ForceDeleteTool:
    """_node_llm_tool_selection must force calendar_delete_event when the user
    clearly intends to cancel/delete and the tool is available.
    """

    def _make_state_with_delete_bundle(self, text: str) -> dict:
        from mithra.domains.bundle import PermissionTier, ToolBundle, ToolMetadata
        request = AgentRequest(
            user_id="person_a",
            device_id="phone_a",
            input_channel=InputChannel.TELEGRAM,
            text=text,
            request_id="req-fix3",
        )
        meta = ToolMetadata(
            name="calendar_delete_event",
            domain_id="time",
            permission=PermissionTier.DESTRUCTIVE,
            is_reversible=False,
            autonomy_level=1,
        )
        fake_tool = MagicMock()
        fake_tool.name = "calendar_delete_event"
        fake_tool.description = "Delete a calendar event"
        fake_tool.parameters = {}
        bundle = ToolBundle(tool=fake_tool, metadata=meta)

        return {
            "request": request,
            "domain_bundles": [bundle],
            "intent": "time.delete_event",
        }

    def test_cancel_keyword_forces_delete_tool(self):
        captured = {}

        class CapturingRouter(MockLLMRouter):
            def call_with_tools(self, messages, tools, model=None, tool_choice="auto"):
                captured["tool_choice"] = tool_choice
                return {"name": "calendar_delete_event", "args": {"event_query": "dentist"}}

        adapter = LangGraphAdapter(llm_router=CapturingRouter())
        state = self._make_state_with_delete_bundle("cancel my dentist appointment")
        adapter._node_llm_tool_selection(state)
        tc = captured.get("tool_choice")
        assert isinstance(tc, dict), f"Expected dict tool_choice, got {tc!r}"
        assert tc["function"]["name"] == "calendar_delete_event"

    def test_delete_keyword_forces_delete_tool(self):
        captured = {}

        class CapturingRouter(MockLLMRouter):
            def call_with_tools(self, messages, tools, model=None, tool_choice="auto"):
                captured["tool_choice"] = tool_choice
                return {"name": "calendar_delete_event", "args": {"event_query": "gym"}}

        adapter = LangGraphAdapter(llm_router=CapturingRouter())
        state = self._make_state_with_delete_bundle("delete gym tomorrow")
        adapter._node_llm_tool_selection(state)
        tc = captured.get("tool_choice")
        assert isinstance(tc, dict)
        assert tc["function"]["name"] == "calendar_delete_event"

    def test_non_cancel_text_uses_auto_tool_choice(self):
        captured = {}

        class CapturingRouter(MockLLMRouter):
            def call_with_tools(self, messages, tools, model=None, tool_choice="auto"):
                captured["tool_choice"] = tool_choice
                return {"name": None, "content": "sure, I'll add that"}

        adapter = LangGraphAdapter(llm_router=CapturingRouter())
        state = self._make_state_with_delete_bundle("add dentist next tuesday 3pm")
        adapter._node_llm_tool_selection(state)
        assert captured.get("tool_choice") == "auto"

    def test_llm_router_call_with_tools_passes_tool_choice_to_body(self):
        """LiteLLMRouterAdapter._call() must include tool_choice in the HTTP body."""
        from mithra.routing.llm_router import LiteLLMRouterAdapter

        captured_body: dict = {}

        class FakeHTTP:
            def post(self, url, json_body=None, headers=None):
                captured_body.update(json_body or {})
                # Return minimal OpenAI-format response (no tool_calls → text)
                fake = MagicMock()
                fake.json.return_value = {
                    "choices": [{"message": {"content": "ok", "tool_calls": None}}]
                }
                return fake

        router = LiteLLMRouterAdapter.__new__(LiteLLMRouterAdapter)
        router._base_url = "http://localhost:4000"
        router._litellm_key = ""
        router._http = FakeHTTP()
        router._storage = None

        force_choice = {"type": "function", "function": {"name": "calendar_delete_event"}}
        router.call_with_tools(
            messages=[{"role": "user", "content": "cancel dentist"}],
            tools=[{"type": "function", "function": {"name": "calendar_delete_event", "description": "d", "parameters": {}}}],
            tool_choice=force_choice,
        )
        assert "tool_choice" in captured_body
        assert captured_body["tool_choice"]["function"]["name"] == "calendar_delete_event"

    def test_auto_tool_choice_not_included_in_body(self):
        """When tool_choice='auto', body must NOT include the field (avoids API confusion)."""
        from mithra.routing.llm_router import LiteLLMRouterAdapter

        captured_body: dict = {}

        class FakeHTTP:
            def post(self, url, json_body=None, headers=None):
                captured_body.update(json_body or {})
                fake = MagicMock()
                fake.json.return_value = {
                    "choices": [{"message": {"content": "ok", "tool_calls": None}}]
                }
                return fake

        router = LiteLLMRouterAdapter.__new__(LiteLLMRouterAdapter)
        router._base_url = "http://localhost:4000"
        router._litellm_key = ""
        router._http = FakeHTTP()
        router._storage = None

        router.call_with_tools(
            messages=[{"role": "user", "content": "show my schedule"}],
            tools=[],
            tool_choice="auto",
        )
        assert "tool_choice" not in captured_body


# ===========================================================================
# FIX 4: text "y"/"yeah" confirm and has_pending()
# ===========================================================================


class TestFix4TextConfirmation:
    """_handle_confirmation must recognise expanded yes/no word sets.
    has_pending() must return bool.
    """

    @pytest.mark.asyncio
    async def test_has_pending_returns_false_when_none(self, engine):
        assert engine.has_pending("person_a") is False

    @pytest.mark.asyncio
    async def test_has_pending_returns_true_after_surface(self, engine):
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult
        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="test", success=True,
        ))
        executor.notify = AsyncMock()
        result = AgentResult(
            request_id="req-pending-1",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)
        assert engine.has_pending("person_a") is True

    @pytest.mark.asyncio
    async def test_telegram_handler_y_triggers_confirm(self, engine):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult

        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        # Plant a pending action
        result = AgentResult(
            request_id="req-y-confirm",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)

        out = MagicMock()
        out.register_chat = MagicMock()
        out.send = AsyncMock()
        out.send_with_actions = AsyncMock()
        handler = TelegramSignalHandler(
            telegram_output=out,
            engine=engine,
            executor=executor,
        )
        req = AgentRequest(
            user_id="person_a",
            device_id="telegram:12345",
            input_channel=InputChannel.TELEGRAM,
            text="y",
        )
        handled = await handler.handle_signal(req)
        assert handled is True

    @pytest.mark.asyncio
    async def test_telegram_handler_yeah_triggers_confirm(self, engine):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult

        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        result = AgentResult(
            request_id="req-yeah-confirm",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)

        out = MagicMock()
        out.register_chat = MagicMock()
        out.send = AsyncMock()
        out.send_with_actions = AsyncMock()
        handler = TelegramSignalHandler(
            telegram_output=out,
            engine=engine,
            executor=executor,
        )
        req = AgentRequest(
            user_id="person_a",
            device_id="telegram:12345",
            input_channel=InputChannel.TELEGRAM,
            text="yeah",
        )
        handled = await handler.handle_signal(req)
        assert handled is True

    @pytest.mark.asyncio
    async def test_telegram_handler_n_triggers_reject(self, engine):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult

        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        result = AgentResult(
            request_id="req-n-reject",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)

        out = MagicMock()
        out.register_chat = MagicMock()
        out.send = AsyncMock()
        handler = TelegramSignalHandler(
            telegram_output=out,
            engine=engine,
            executor=executor,
        )
        req = AgentRequest(
            user_id="person_a",
            device_id="telegram:12345",
            input_channel=InputChannel.TELEGRAM,
            text="n",
        )
        handled = await handler.handle_signal(req)
        assert handled is True

    @pytest.mark.asyncio
    async def test_telegram_handler_abort_triggers_reject(self, engine):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult

        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        result = AgentResult(
            request_id="req-abort-reject",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)

        out = MagicMock()
        out.register_chat = MagicMock()
        out.send = AsyncMock()
        handler = TelegramSignalHandler(
            telegram_output=out,
            engine=engine,
            executor=executor,
        )
        req = AgentRequest(
            user_id="person_a",
            device_id="telegram:12345",
            input_channel=InputChannel.TELEGRAM,
            text="abort",
        )
        handled = await handler.handle_signal(req)
        assert handled is True

    @pytest.mark.asyncio
    async def test_unrelated_text_not_intercepted_as_confirmation(self, engine):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult

        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        result = AgentResult(
            request_id="req-unrelated",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action()],
            response_text="",
        )
        await engine.process(result, "person_a", executor)

        out = MagicMock()
        out.register_chat = MagicMock()
        out.send = AsyncMock()
        handler = TelegramSignalHandler(
            telegram_output=out,
            engine=engine,
            executor=executor,
        )
        req = AgentRequest(
            user_id="person_a",
            device_id="telegram:12345",
            input_channel=InputChannel.TELEGRAM,
            text="what's the weather today",
        )
        handled = await handler.handle_signal(req)
        assert handled is False


# ===========================================================================
# FIX 5: _format_confirmation_message() and _describe_action()
# ===========================================================================


class TestFix5ConfirmationMessages:
    """_format_confirmation_message() must return human-readable text.
    _describe_action() must use it when action.description is empty.
    """

    def test_calendar_create_event_with_title_and_time(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_create_event",
            {"title": "Dentist", "start_time": "2026-06-01T15:00:00"},
        )
        assert "Dentist" in msg
        assert "2026-06-01" in msg or "15:00" in msg

    def test_calendar_create_event_title_only(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_create_event",
            {"title": "Team sync"},
        )
        assert "Team sync" in msg

    def test_calendar_create_recurring_with_day_and_time(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_create_recurring",
            {"title": "Gym", "by_week_day": "MO", "start_time": "07:00"},
        )
        assert "Gym" in msg
        assert "MO" in msg or "07:00" in msg

    def test_calendar_delete_event(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_delete_event",
            {"event_query": "dentist appointment"},
        )
        assert "dentist appointment" in msg
        assert "delete" in msg.lower() or "calendar" in msg.lower()

    def test_calendar_update_event_with_new_time(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_update_event",
            {"event_query": "gym", "new_time": "2026-06-02T08:00:00"},
        )
        assert "gym" in msg
        assert "2026-06-02" in msg or "08:00" in msg

    def test_calendar_update_event_without_new_time(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "calendar_update_event",
            {"event_query": "team standup"},
        )
        assert "team standup" in msg

    def test_unknown_action_type_uses_readable_fallback(self):
        msg = DefaultAutonomyEngine._format_confirmation_message(
            "do_some_internal_thing",
            {},
        )
        assert "do some internal thing" == msg

    def test_describe_action_uses_format_when_description_empty(self, engine):
        action = AgentAction(
            action_type="calendar_create_event",
            domain="time",
            payload={"title": "Dentist", "start_time": "2026-06-01T15:00:00"},
            output_channel="telegram",
            description="",
        )
        text = engine._describe_action(action)
        assert "Dentist" in text
        assert "calendar_create_event" not in text

    def test_describe_action_prefers_existing_description(self, engine):
        action = AgentAction(
            action_type="calendar_create_event",
            domain="time",
            payload={"title": "Dentist"},
            output_channel="telegram",
            description="Book the dentist for Tuesday",
        )
        text = engine._describe_action(action)
        assert text == "Book the dentist for Tuesday"

    def test_describe_action_delete_event_human_readable(self, engine):
        action = AgentAction(
            action_type="calendar_delete_event",
            domain="time",
            payload={"event_query": "dentist appointment"},
            output_channel="telegram",
            description="",
        )
        text = engine._describe_action(action)
        assert "dentist appointment" in text
        assert "calendar_delete_event" not in text

    @pytest.mark.asyncio
    async def test_engine_process_response_text_is_human_readable(self, engine):
        """receipt.response_text must not contain internal tool name strings."""
        executor = MagicMock(spec=ActionExecutorRegistry)
        executor.execute = AsyncMock(return_value=ExecutionResult(
            action_id=str(uuid.uuid4()), action_type="calendar_create_event", success=True,
        ))
        executor.notify = AsyncMock()

        result = AgentResult(
            request_id="req-human-text",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="time",
            confidence=1.0,
            actions=[_surface_action(
                "calendar_create_event",
                {"title": "Dentist", "start_time": "2026-06-01T15:00:00"},
            )],
            response_text="",
        )
        receipt = await engine.process(result, "person_a", executor)
        assert receipt.response_text is not None
        assert "calendar_create_event" not in receipt.response_text
        assert "Dentist" in receipt.response_text


# ===========================================================================
# FIX 6: _make_event() always converts to household timezone (UTC input)
# ===========================================================================


class TestFix6TimezoneConversion:
    """_make_event() must produce events in household local time regardless
    of whether the input datetime is UTC-aware, locally-aware, or naive.
    """

    def test_utc_aware_datetime_converted_to_local(self):
        """Case (b): correct moment in UTC must be converted to local time.

        dateparser may return 20:00 UTC representing 3pm CDT.
        _make_event() must convert it so the calendar event shows 3pm CDT.
        """
        from mithra.domains.time.tools import _make_event
        import pytz
        from datetime import timezone as dt_timezone

        # 20:00 UTC = 15:00 CDT (America/Chicago, UTC-5 in summer)
        utc_dt = datetime(2026, 6, 1, 20, 0, 0, tzinfo=dt_timezone.utc)
        event = _make_event("Dentist", utc_dt, 60, "America/Chicago")

        # event.start is naive local time — check hour directly, no .astimezone()
        assert event.start.tzinfo is None
        assert event.start.hour == 15, (
            f"Expected naive 15:00 (CDT local) but got {event.start} "
            f"(input was 20:00 UTC = 15:00 CDT)"
        )

    def test_already_local_aware_datetime_unchanged(self):
        """Already-local CDT datetime must produce naive 15:00 event."""
        from mithra.domains.time.tools import _make_event
        import pytz

        tz = pytz.timezone("America/Chicago")
        local_dt = tz.localize(datetime(2026, 6, 1, 15, 0, 0))
        event = _make_event("Dentist", local_dt, 60, "America/Chicago")

        assert event.start.tzinfo is None
        assert event.start.hour == 15

    def test_naive_datetime_localised_to_household_tz(self):
        """Naive 15:00 must be treated as 15:00 local, preserved as naive 15:00."""
        from mithra.domains.time.tools import _make_event

        naive_dt = datetime(2026, 6, 1, 15, 0, 0)
        event = _make_event("Dentist", naive_dt, 60, "America/Chicago")

        assert event.start.tzinfo is None
        assert event.start.hour == 15

    def test_end_dt_computed_after_timezone_conversion(self):
        """end_dt must be start_dt + duration in local time — naive 16:30."""
        from mithra.domains.time.tools import _make_event
        from datetime import timezone as dt_timezone

        # 20:00 UTC = 15:00 CDT
        utc_dt = datetime(2026, 6, 1, 20, 0, 0, tzinfo=dt_timezone.utc)
        event = _make_event("Meeting", utc_dt, 90, "America/Chicago")

        # 15:00 CDT + 90 min = 16:30 CDT — stored as naive local
        assert event.end.tzinfo is None
        assert event.end.hour == 16
        assert event.end.minute == 30

    def test_event_timezone_field_always_set(self):
        """gcsa Event.timezone must always equal the household timezone string."""
        from mithra.domains.time.tools import _make_event
        from datetime import timezone as dt_timezone

        utc_dt = datetime(2026, 6, 1, 20, 0, 0, tzinfo=dt_timezone.utc)
        event = _make_event("Dentist", utc_dt, 60, "America/Chicago")
        assert event.timezone == "America/Chicago"


# ===========================================================================
# FIX 7: healthcheck per-service timeout
# ===========================================================================


class TestFix7HealthcheckTimeout:
    """check_service() must use config['timeout'] not a hardcoded value."""

    def test_check_service_uses_config_timeout(self):
        """check_service() must pass config['timeout'] to urlopen."""
        import sys
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "healthcheck",
            Path(__file__).parents[2] / "scripts" / "healthcheck.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout

        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            mod.check_service("llm-router", {
                "url": "http://llm-router:4000/health",
                "headers": {},
                "timeout": 5,
            })
        finally:
            urllib.request.urlopen = original

        assert captured.get("timeout") == 5

    def test_llm_router_config_has_timeout_5(self):
        """SERVICES dict must have timeout=5 for llm-router."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "healthcheck",
            Path(__file__).parents[2] / "scripts" / "healthcheck.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert "llm-router" in mod.SERVICES
        assert mod.SERVICES["llm-router"].get("timeout") == 5

    def test_api_gateway_config_has_timeout_5(self):
        """SERVICES dict must have timeout=5 for api-gateway."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "healthcheck",
            Path(__file__).parents[2] / "scripts" / "healthcheck.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.SERVICES["api-gateway"].get("timeout") == 5

    def test_check_service_falls_back_to_5s_when_no_timeout_in_config(self):
        """Omitting timeout from config must default to 5s (not error)."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "healthcheck",
            Path(__file__).parents[2] / "scripts" / "healthcheck.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["timeout"] = timeout

        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            mod.check_service("some-service", {
                "url": "http://some-service:9999/health",
                "headers": {},
                # no timeout key
            })
        finally:
            urllib.request.urlopen = original

        assert captured.get("timeout") == 5


# ===========================================================================
# FIX 8: Fuzzy event matching (_event_matches) for delete and update tools
# ===========================================================================


class TestFix8FuzzyEventMatching:
    """_event_matches() must find events even when query contains stop words
    or extra context that is not part of the event title.
    """

    def test_full_phrase_match(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Trash Reminder", "Trash Reminder") is True

    def test_case_insensitive_full_phrase(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("trash reminder", "TRASH REMINDER") is True

    def test_weekly_stop_word_stripped_finds_trash(self):
        """'cancel weekly trash reminder' must match 'Trash Reminder'."""
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Trash Reminder", "cancel weekly trash reminder") is True

    def test_appointment_stop_word_stripped_finds_dentist(self):
        """'cancel dentist appointment' must match 'Dentist'."""
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Dentist", "cancel dentist appointment") is True

    def test_meeting_stop_word_stripped(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Team Standup", "delete team standup meeting") is True

    def test_no_match_returns_false(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Team Standup", "delete yoga class") is False

    def test_empty_query_no_match(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("Dentist", "") is False

    def test_empty_summary_no_match(self):
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("", "dentist") is False

    def test_short_words_below_length_threshold_ignored(self):
        """Words of 2 chars or fewer are filtered — 'my' should not match 'XY'."""
        from mithra.domains.time.tools import _event_matches
        assert _event_matches("XY", "my") is False

    def test_delete_tool_finds_event_with_stop_word_query(self):
        """CalendarDeleteEventTool must find 'Dentist' via 'cancel dentist appointment'."""
        from mithra.domains.time.tools import CalendarDeleteEventTool
        from mithra.domains.context import DomainContext
        from tests.unit.domains.time.conftest import MockCalendarService, _FakeEvent
        from unittest.mock import MagicMock, patch

        oauth = MagicMock()
        oauth._store.load.return_value = {"access_token": "tok", "refresh_token": "ref", "scopes": []}
        ctx = DomainContext(
            user_id="person_a", household_id="hh_001",
            household_timezone="America/Chicago", oauth_manager=oauth,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_del", channel="telegram",
        )
        today = datetime.now(timezone.utc) + timedelta(hours=2)
        svc = MockCalendarService(events=[_FakeEvent("evt_001", "Dentist", today, today + timedelta(hours=1))])

        with patch("mithra.domains.time.tools.GoogleCalendarService", return_value=svc):
            tool = CalendarDeleteEventTool(context=ctx)
            result = tool._run(event_query="cancel dentist appointment")

        assert result.success is True
        assert "Dentist" in result.summary

    def test_update_tool_finds_event_with_stop_word_query(self):
        """CalendarUpdateEventTool must find 'Dentist' via 'dentist appointment'."""
        from mithra.domains.time.tools import CalendarUpdateEventTool
        from mithra.domains.context import DomainContext
        from tests.unit.domains.time.conftest import MockCalendarService, _FakeEvent
        from unittest.mock import MagicMock, patch

        oauth = MagicMock()
        oauth._store.load.return_value = {"access_token": "tok", "refresh_token": "ref", "scopes": []}
        ctx = DomainContext(
            user_id="person_a", household_id="hh_001",
            household_timezone="America/Chicago", oauth_manager=oauth,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_upd", channel="telegram",
        )
        today = datetime.now(timezone.utc) + timedelta(hours=2)
        svc = MockCalendarService(events=[_FakeEvent("evt_001", "Dentist", today, today + timedelta(hours=1))])

        with patch("mithra.domains.time.tools.GoogleCalendarService", return_value=svc):
            tool = CalendarUpdateEventTool(context=ctx)
            result = tool._run(event_query="dentist appointment", new_time="tomorrow 3pm")

        assert result.success is True


# ===========================================================================
# FIX 9: Timezone label in confirmation and success messages
# ===========================================================================


class TestFix9TimezoneMessages:
    """Confirmation message and success message must include timezone abbreviation."""

    def test_success_message_includes_timezone(self):
        """Created event summary must show timezone abbreviation (CST/CDT)."""
        from mithra.domains.time.tools import CalendarCreateEventTool
        from mithra.domains.context import DomainContext
        from tests.unit.domains.time.conftest import MockCalendarService
        from unittest.mock import MagicMock, patch

        oauth = MagicMock()
        oauth._store.load.return_value = {"access_token": "tok", "refresh_token": "ref", "scopes": []}
        ctx = DomainContext(
            user_id="person_a", household_id="hh_001",
            household_timezone="America/Chicago", oauth_manager=oauth,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_tz", channel="telegram",
        )
        svc = MockCalendarService()

        with patch("mithra.domains.time.tools.GoogleCalendarService", return_value=svc):
            tool = CalendarCreateEventTool(context=ctx)
            result = tool._run(title="Dentist", start_time="tomorrow 3pm")

        assert result.success is True
        summary = result.summary
        assert any(tz in summary for tz in ("CST", "CDT", "CT", "UTC")), (
            f"Expected timezone abbreviation in success message, got: {summary!r}"
        )

    def test_format_confirmation_message_uses_start_time_display(self):
        """_format_confirmation_message must prefer start_time_display over start_time."""
        from mithra.autonomy.default_engine import DefaultAutonomyEngine
        payload = {
            "title": "Dentist",
            "start_time": "tuesday 3pm",
            "start_time_display": "Tuesday June 2, 3:00 PM CST",
        }
        msg = DefaultAutonomyEngine._format_confirmation_message("calendar_create_event", payload)
        assert "Tuesday June 2, 3:00 PM CST" in msg
        assert "tuesday 3pm" not in msg

    def test_format_confirmation_message_falls_back_to_start_time(self):
        """If start_time_display absent, fall back to raw start_time gracefully."""
        from mithra.autonomy.default_engine import DefaultAutonomyEngine
        payload = {
            "title": "Dentist",
            "start_time": "tuesday 3pm",
        }
        msg = DefaultAutonomyEngine._format_confirmation_message("calendar_create_event", payload)
        assert "tuesday 3pm" in msg
        assert "Dentist" in msg

    def test_format_confirmation_message_recurring_uses_start_time_display(self):
        """_format_confirmation_message for recurring must also use start_time_display."""
        from mithra.autonomy.default_engine import DefaultAutonomyEngine
        payload = {
            "title": "Gym",
            "frequency": "weekly",
            "by_week_day": "MO",
            "start_time": "monday 7am",
            "start_time_display": "Monday June 2, 7:00 AM CST",
        }
        msg = DefaultAutonomyEngine._format_confirmation_message("calendar_create_recurring", payload)
        assert "Monday June 2, 7:00 AM CST" in msg
        assert "monday 7am" not in msg

    def test_format_start_for_confirmation_returns_formatted_string(self):
        """_format_start_for_confirmation must parse natural language + return tz display."""
        from mithra.agent.langgraph_adapter import LangGraphAdapter
        from mithra.routing.llm_router import IntentClassification, RouterHealth
        from unittest.mock import MagicMock

        class MinimalRouter:
            def complete(self, *a, **kw): return MagicMock()
            def call_with_tools(self, *a, **kw): return {"name": None, "content": ""}
            def classify_intent(self, *a, **kw):
                return IntentClassification(intent="system.unknown", domain="system", confidence=0.5)
            def get_usage(self, *a, **kw): return MagicMock()
            def health(self):
                return RouterHealth(
                    litellm_reachable=True,
                    cloud_model_available=True,
                    local_model_available=True,
                )

        adapter = LangGraphAdapter(
            llm_router=MinimalRouter(), household_timezone="America/Chicago"
        )
        result = adapter._format_start_for_confirmation("tomorrow 3pm", "America/Chicago")
        assert isinstance(result, str)
        if result:
            assert "PM" in result
            assert any(tz in result for tz in ("CST", "CDT", "CT"))
