"""Structural and behavioural tests for mithra/autonomy/executor.py."""

import dataclasses
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mithra.agent.interface import AgentAction
from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult


class TestExecutionResult:
    def test_has_action_id_field(self):
        field_names = {f.name for f in dataclasses.fields(ExecutionResult)}
        assert "action_id" in field_names

    def test_has_action_type_field(self):
        field_names = {f.name for f in dataclasses.fields(ExecutionResult)}
        assert "action_type" in field_names

    def test_has_success_field(self):
        field_names = {f.name for f in dataclasses.fields(ExecutionResult)}
        assert "success" in field_names

    def test_has_error_field(self):
        field_names = {f.name for f in dataclasses.fields(ExecutionResult)}
        assert "error" in field_names

    def test_has_undo_payload_field(self):
        field_names = {f.name for f in dataclasses.fields(ExecutionResult)}
        assert "undo_payload" in field_names

    def test_undo_payload_defaults_to_empty_dict(self):
        result = ExecutionResult(
            action_id=str(uuid.uuid4()),
            action_type="send_message",
            success=True,
        )
        assert result.undo_payload == {}
        assert isinstance(result.undo_payload, dict)

    def test_undo_payload_independence_between_instances(self):
        a = ExecutionResult(action_id="a1", action_type="x", success=True)
        b = ExecutionResult(action_id="b1", action_type="x", success=True)
        a.undo_payload["key"] = "val"
        assert "key" not in b.undo_payload

    def test_error_defaults_to_none(self):
        result = ExecutionResult(action_id="a1", action_type="x", success=True)
        assert result.error is None


class TestActionExecutorRegistry:
    def test_register_accepts_action_type_and_adapter(self):
        registry = ActionExecutorRegistry()
        adapter = MagicMock()
        registry.register("send_whatsapp", adapter)
        assert "send_whatsapp" in registry.registered_types()

    def test_registered_types_returns_list_of_str(self):
        registry = ActionExecutorRegistry()
        registry.register("action_a", MagicMock())
        registry.register("action_b", MagicMock())
        types = registry.registered_types()
        assert isinstance(types, list)
        assert all(isinstance(t, str) for t in types)

    def test_registered_types_empty_before_registration(self):
        registry = ActionExecutorRegistry()
        assert registry.registered_types() == []

    def test_has_undo_method(self):
        assert hasattr(ActionExecutorRegistry, "undo")
        assert callable(ActionExecutorRegistry.undo)

    def test_has_notify_method(self):
        assert hasattr(ActionExecutorRegistry, "notify")
        assert callable(ActionExecutorRegistry.notify)

    def test_execute_is_async(self):
        assert inspect.iscoroutinefunction(ActionExecutorRegistry.execute)

    def test_undo_is_async(self):
        assert inspect.iscoroutinefunction(ActionExecutorRegistry.undo)

    def test_notify_is_async(self):
        assert inspect.iscoroutinefunction(ActionExecutorRegistry.notify)

    @pytest.mark.asyncio
    async def test_execute_accepts_agent_action(self):
        registry = ActionExecutorRegistry()
        adapter = MagicMock()
        adapter.execute = AsyncMock(return_value=ExecutionResult(
            action_id="a1", action_type="send_message", success=True,
        ))
        registry.register("send_message", adapter)
        action = AgentAction(
            action_type="send_message",
            domain="people",
            payload={"text": "Happy birthday!"},
        )
        result = await registry.execute(action)
        assert isinstance(result, ExecutionResult)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_unknown_action_type_returns_failure(self):
        registry = ActionExecutorRegistry()
        action = AgentAction(
            action_type="nonexistent_action",
            domain="people",
            payload={},
        )
        result = await registry.execute(action)
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_execute_routes_to_correct_adapter(self):
        registry = ActionExecutorRegistry()
        adapter_a = MagicMock()
        adapter_a.execute = AsyncMock(return_value=ExecutionResult(
            action_id="x", action_type="action_a", success=True,
        ))
        adapter_b = MagicMock()
        adapter_b.execute = AsyncMock(return_value=ExecutionResult(
            action_id="y", action_type="action_b", success=True,
        ))
        registry.register("action_a", adapter_a)
        registry.register("action_b", adapter_b)

        action = AgentAction(action_type="action_a", domain="food", payload={})
        await registry.execute(action)

        adapter_a.execute.assert_called_once()
        adapter_b.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_captures_exception_as_error_result(self):
        registry = ActionExecutorRegistry()
        adapter = MagicMock()
        adapter.execute = AsyncMock(side_effect=RuntimeError("adapter blew up"))
        registry.register("boom_action", adapter)

        action = AgentAction(action_type="boom_action", domain="home", payload={})
        result = await registry.execute(action)

        assert result.success is False
        assert "adapter blew up" in result.error

    @pytest.mark.asyncio
    async def test_undo_calls_adapter_undo_with_log_entry(self):
        from mithra.autonomy.engine import ActionLogEntry, Reversibility

        registry = ActionExecutorRegistry()
        adapter = MagicMock()
        adapter.undo = AsyncMock(return_value=ExecutionResult(
            action_id="u1", action_type="send_message", success=True,
        ))
        registry.register("send_message", adapter)

        entry = ActionLogEntry(
            action_id="a1", request_id="r1", user_id="person_a",
            domain="people", action_type="send_message", payload={},
            status="executed", reversibility=Reversibility.PARTIAL,
            timestamp="2026-01-01T00:00:00+00:00",
            intent="people.send_message", autonomy_level=2,
            output_channel="telegram",
            executed_at="2026-01-01T00:00:00+00:00", success=True,
            undo_payload={"message_id": "m1"},
        )
        result = await registry.undo(entry)

        adapter.undo.assert_called_once_with(entry)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_undo_returns_error_for_unregistered_type(self):
        from mithra.autonomy.engine import ActionLogEntry, Reversibility

        registry = ActionExecutorRegistry()
        entry = ActionLogEntry(
            action_id="a2", request_id="r2", user_id="person_a",
            domain="people", action_type="ghost_action", payload={},
            status="executed", reversibility=Reversibility.FULL,
            timestamp="2026-01-01T00:00:00+00:00",
            intent="people.ghost_action", autonomy_level=2,
            output_channel="none",
            executed_at="2026-01-01T00:00:00+00:00", success=True,
        )
        result = await registry.undo(entry)

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_notify_calls_notification_adapter_send(self):
        registry = ActionExecutorRegistry()
        adapter = MagicMock()
        adapter.send = AsyncMock()
        registry.register_notify("telegram", adapter)

        await registry.notify("person_a", "telegram", "Hello!")

        adapter.send.assert_called_once()
        call_args = adapter.send.call_args[0]
        assert call_args[0] == "person_a"

    @pytest.mark.asyncio
    async def test_notify_silent_when_channel_not_registered(self):
        registry = ActionExecutorRegistry()
        # No adapter registered for "telegram" — should not raise
        await registry.notify("person_a", "telegram", "Hello!")

    @pytest.mark.asyncio
    async def test_fallback_called_when_action_type_not_registered(self):
        """register_fallback() adapter is invoked for unregistered action_types."""
        registry = ActionExecutorRegistry()
        fallback = MagicMock()
        fallback.execute = AsyncMock(return_value=ExecutionResult(
            action_id="fb1", action_type="calendar_create_recurring", success=True,
        ))
        registry.register_fallback(fallback)

        action = AgentAction(
            action_type="calendar_create_recurring",
            domain="time",
            payload={"title": "Standup", "start_time": "monday 9am"},
        )
        result = await registry.execute(action)

        fallback.execute.assert_called_once_with(action)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_fallback_not_called_when_registered_adapter_exists(self):
        """Registered adapters take precedence over the fallback."""
        registry = ActionExecutorRegistry()
        specific = MagicMock()
        specific.execute = AsyncMock(return_value=ExecutionResult(
            action_id="sp1", action_type="calendar_write", success=True,
        ))
        fallback = MagicMock()
        fallback.execute = AsyncMock()
        registry.register("calendar_write", specific)
        registry.register_fallback(fallback)

        action = AgentAction(action_type="calendar_write", domain="time", payload={})
        await registry.execute(action)

        specific.execute.assert_called_once()
        fallback.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_returns_error_for_unregistered_type(self):
        """Without fallback, unregistered action_type still returns error result."""
        registry = ActionExecutorRegistry()
        action = AgentAction(action_type="calendar_create_recurring", domain="time", payload={})
        result = await registry.execute(action)
        assert result.success is False
        assert "No adapter registered" in result.error
