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
