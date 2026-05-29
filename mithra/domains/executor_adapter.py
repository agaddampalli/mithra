"""DomainToolAdapter — executes domain tools via ActionExecutorRegistry fallback.

Registered as executor.register_fallback() at startup so confirmed domain
tool actions (calendar_create_recurring, calendar_delete_event, etc.) route
here instead of failing with "No adapter registered".

Builds a minimal DomainContext from services already in bootstrap state,
invokes the matching ToolBundle synchronously, and returns ExecutionResult.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

_logger = logging.getLogger(__name__)


class DomainToolAdapter:
    """Fallback adapter that invokes any registered domain tool by action_type."""

    def __init__(
        self,
        domain_registry: Any,
        household_id: str,
        household_timezone: str,
        oauth_manager: Any,
        storage_router: Any,
        member_registry: Any,
        file_router: Any,
        file_search: Any,
    ) -> None:
        self._domain_registry = domain_registry
        self._household_id = household_id
        self._household_timezone = household_timezone
        self._oauth_manager = oauth_manager
        self._storage_router = storage_router
        self._member_registry = member_registry
        self._file_router = file_router
        self._file_search = file_search

    async def execute(self, action: Any) -> Any:
        import asyncio
        return await asyncio.to_thread(self._execute_sync, action)

    def _execute_sync(self, action: Any) -> Any:
        from mithra.autonomy.executor import ExecutionResult
        from mithra.domains.context import DomainContext
        from mithra.domains.result import ToolResult

        action_id = str(uuid.uuid4())

        domain = self._domain_registry.get(action.domain)
        if domain is None:
            _logger.warning(
                "DomainToolAdapter: domain '%s' not registered (action_type=%r)",
                action.domain, action.action_type,
            )
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=False,
                error=f"No adapter or domain registered for action_type '{action.action_type}'",
            )

        storage_adapter = None
        if self._storage_router:
            try:
                storage_adapter = self._storage_router.get_adapter("general")
            except Exception:
                pass

        user_id = action.payload.get("user_id", "household")
        ctx = DomainContext(
            user_id=user_id,
            household_id=self._household_id,
            household_timezone=self._household_timezone,
            oauth_manager=self._oauth_manager,
            storage_adapter=storage_adapter,
            file_router=self._file_router,
            member_registry=self._member_registry,
            file_search=self._file_search,
            request_id=str(uuid.uuid4()),
            channel="telegram",
        )

        try:
            bundles = domain.get_tools(ctx)
        except Exception as exc:
            _logger.error("DomainToolAdapter: get_tools failed: %s", exc, exc_info=True)
            return ExecutionResult(
                action_id=action_id, action_type=action.action_type,
                success=False, error=str(exc),
            )

        bundle = next(
            (b for b in bundles if b.metadata.name == action.action_type), None
        )
        if bundle is None:
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=False,
                error=f"Tool '{action.action_type}' not found in domain '{action.domain}'",
            )

        try:
            result = bundle.tool.invoke(action.payload)
        except Exception as exc:
            _logger.error(
                "DomainToolAdapter: tool %r failed: %s", action.action_type, exc, exc_info=True
            )
            return ExecutionResult(
                action_id=action_id, action_type=action.action_type,
                success=False, error=str(exc),
            )

        if isinstance(result, ToolResult):
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=result.success,
                error=result.error,
                undo_payload={"undo_token": result.undo_token} if result.undo_token else {},
            )
        if isinstance(result, dict):
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=result.get("success", True),
                error=result.get("error"),
            )
        return ExecutionResult(
            action_id=action_id, action_type=action.action_type, success=True,
        )
