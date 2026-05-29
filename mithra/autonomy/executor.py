"""
ActionExecutorRegistry — maps action_type strings to OutputAdapter instances.

Registered at startup in the gateway. Adding a new output type requires exactly
one new line in the registration block — no changes to core engine code.

AutononomyEngine calls executor.execute(action) after deciding an action may
proceed. The registry dispatches to the correct OutputAdapter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mithra.agent.interface import AgentAction
    from mithra.autonomy.engine import ActionLogEntry


@dataclass
class ExecutionResult:
    """Outcome of a single action execution via an OutputAdapter."""

    action_id: str
    action_type: str
    success: bool
    delivery_receipt: dict | None = None
    error: str | None = None
    undo_payload: dict = field(default_factory=dict)
    # stores what is needed to reverse this action
    # e.g. for calendar write: {"event_id": "abc123"}
    # e.g. for WhatsApp send: {"message_id": "xyz", "phone": "+1555..."}


class ActionExecutorRegistry:
    """Maps action_type → OutputAdapter and executes actions on demand."""

    def __init__(self) -> None:
        self._registry: dict[str, Any] = {}
        self._notify_adapters: dict[str, Any] = {}
        self._fallback: Any = None

    def register(self, action_type: str, adapter: Any) -> None:
        """Register an OutputAdapter for an action_type."""
        self._registry[action_type] = adapter

    def register_fallback(self, adapter: Any) -> None:
        """Register a fallback adapter called when no action_type match exists.

        Used by DomainToolAdapter so domain tools execute via the normal
        confirm() path without needing a registered entry per tool.
        """
        self._fallback = adapter

    def register_notify(self, channel: str, adapter: Any) -> None:
        """Register an OutputAdapter for a notification channel."""
        self._notify_adapters[channel] = adapter

    async def execute(self, action: "AgentAction") -> ExecutionResult:
        """Dispatch an action to the registered OutputAdapter.

        Receives the full AgentAction so output_channel, reversibility,
        payload, and action_type are all available to the executor.
        Falls back to the fallback adapter when no specific adapter is registered.
        """
        adapter = self._registry.get(action.action_type)
        if adapter is None:
            if self._fallback is not None:
                return await self._fallback.execute(action)
            return ExecutionResult(
                action_id=str(uuid.uuid4()),
                action_type=action.action_type,
                success=False,
                error=f"No adapter registered for action_type '{action.action_type}'",
            )
        try:
            result = await adapter.execute(action)
            return result
        except Exception as exc:
            return ExecutionResult(
                action_id=str(uuid.uuid4()),
                action_type=action.action_type,
                success=False,
                error=str(exc),
            )

    async def undo(self, log_entry: "ActionLogEntry") -> ExecutionResult:
        """Undo a previously executed action using its stored undo_payload."""
        adapter = self._registry.get(log_entry.action_type)
        if adapter is None:
            return ExecutionResult(
                action_id=log_entry.action_id,
                action_type=log_entry.action_type,
                success=False,
                error=f"No adapter registered for action_type '{log_entry.action_type}'",
            )
        try:
            result = await adapter.undo(log_entry)
            return result
        except Exception as exc:
            return ExecutionResult(
                action_id=log_entry.action_id,
                action_type=log_entry.action_type,
                success=False,
                error=str(exc),
            )

    async def notify(self, user_id: str, channel: str, message: str) -> None:
        """Send an ACT_TELL notification via the specified channel adapter."""
        adapter = self._notify_adapters.get(channel)
        if adapter is None:
            return
        try:
            from mithra.adapters.messaging import OutboundMessage
            msg = OutboundMessage(
                text=message,
                format="TEXT",
                domain="autonomy",
                priority="normal",
            )
            await adapter.send(user_id, msg)
        except Exception:
            pass

    def registered_types(self) -> list[str]:
        """Return all registered action type names."""
        return list(self._registry.keys())
