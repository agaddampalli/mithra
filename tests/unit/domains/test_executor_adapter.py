"""Tests for mithra/domains/executor_adapter.py — DomainToolAdapter."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from mithra.domains.executor_adapter import DomainToolAdapter
from mithra.agent.interface import AgentAction
from mithra.autonomy.executor import ExecutionResult
from mithra.domains.result import ToolResult


def _make_adapter(domain_registry=None) -> DomainToolAdapter:
    return DomainToolAdapter(
        domain_registry=domain_registry or MagicMock(),
        household_id="hh_001",
        household_timezone="America/Chicago",
        oauth_manager=MagicMock(),
        storage_router=None,
        member_registry=None,
        file_router=None,
        file_search=None,
    )


def _make_action(action_type: str, domain: str = "time", payload: dict | None = None) -> AgentAction:
    return AgentAction(action_type=action_type, domain=domain, payload=payload or {})


class TestDomainToolAdapterSuccess:
    @pytest.mark.asyncio
    async def test_executes_domain_tool_and_returns_success(self):
        """Adapter locates tool by action_type in domain and invokes it."""
        tool_result = ToolResult.ok("Created!", data={"event_id": "abc"}, undo_token="abc")

        bundle = MagicMock()
        bundle.metadata.name = "calendar_create_recurring"
        bundle.tool.invoke = MagicMock(return_value=tool_result)

        domain = MagicMock()
        domain.get_tools = MagicMock(return_value=[bundle])

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring", payload={"title": "Standup"})

        result = adapter._execute_sync(action)

        assert result.success is True
        assert result.undo_payload == {"undo_token": "abc"}
        bundle.tool.invoke.assert_called_once_with(action.payload)

    @pytest.mark.asyncio
    async def test_execute_is_async(self):
        """execute() is a coroutine (wraps sync via asyncio.to_thread)."""
        import inspect
        assert inspect.iscoroutinefunction(DomainToolAdapter.execute)

    @pytest.mark.asyncio
    async def test_execute_calls_execute_sync(self):
        """execute() result matches _execute_sync output."""
        bundle = MagicMock()
        bundle.metadata.name = "calendar_delete_event"
        bundle.tool.invoke = MagicMock(return_value=ToolResult.ok("Deleted!"))

        domain = MagicMock()
        domain.get_tools = MagicMock(return_value=[bundle])

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_delete_event")

        result = await adapter.execute(action)
        assert isinstance(result, ExecutionResult)
        assert result.success is True


class TestDomainToolAdapterErrors:
    def test_returns_error_when_domain_not_registered(self):
        registry = MagicMock()
        registry.get = MagicMock(return_value=None)  # domain not found

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring", domain="time")

        result = adapter._execute_sync(action)

        assert result.success is False
        assert "No adapter or domain" in result.error

    def test_returns_error_when_tool_not_found_in_domain(self):
        other_bundle = MagicMock()
        other_bundle.metadata.name = "calendar_read_events"

        domain = MagicMock()
        domain.get_tools = MagicMock(return_value=[other_bundle])

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring")

        result = adapter._execute_sync(action)

        assert result.success is False
        assert "calendar_create_recurring" in result.error

    def test_returns_error_when_tool_raises(self):
        bundle = MagicMock()
        bundle.metadata.name = "calendar_create_recurring"
        bundle.tool.invoke = MagicMock(side_effect=RuntimeError("API down"))

        domain = MagicMock()
        domain.get_tools = MagicMock(return_value=[bundle])

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring")

        result = adapter._execute_sync(action)

        assert result.success is False
        assert "API down" in result.error

    def test_failed_tool_result_propagates_error(self):
        bundle = MagicMock()
        bundle.metadata.name = "calendar_create_recurring"
        bundle.tool.invoke = MagicMock(
            return_value=ToolResult.fail("Google not connected", error_code="GOOGLE_NOT_CONNECTED")
        )

        domain = MagicMock()
        domain.get_tools = MagicMock(return_value=[bundle])

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring")

        result = adapter._execute_sync(action)

        assert result.success is False
        assert "Google not connected" in result.error

    def test_user_id_taken_from_payload(self):
        """user_id in payload is used to build DomainContext."""
        captured_ctx = {}

        def capture_get_tools(ctx):
            captured_ctx["user_id"] = ctx.user_id
            return []

        domain = MagicMock()
        domain.get_tools = capture_get_tools

        registry = MagicMock()
        registry.get = MagicMock(return_value=domain)

        adapter = _make_adapter(registry)
        action = _make_action("calendar_create_recurring", payload={"user_id": "person_a"})

        adapter._execute_sync(action)

        assert captured_ctx.get("user_id") == "person_a"
