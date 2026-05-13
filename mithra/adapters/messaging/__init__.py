"""
mithra/adapters/messaging/__init__.py

Base types shared by all input and output channel adapters.
InputAdapter and OutputAdapter are the two contracts that
every channel implementation must satisfy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator


class MessageFormat(str, Enum):
    TEXT     = "text"
    MARKDOWN = "markdown"
    VOICE    = "voice"
    CARD     = "card"


@dataclass
class OutboundMessage:
    """Channel-agnostic outbound message. Each OutputAdapter renders this."""

    text: str
    format: MessageFormat = MessageFormat.TEXT
    domain: str | None = None
    priority: str = "normal"        # "urgent" | "normal" | "low"
    reply_to_id: str | None = None


@dataclass
class ActionButton:
    """Interactive button for Level-1 confirmations. Rendered per channel."""

    label: str
    action: str     # "confirm" | "reject" | "modify"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryReceipt:
    """Proof of delivery. Stored in ActionLogEntry.undo_payload."""

    channel: str
    delivered_at: str
    success: bool
    message_id: str | None = None
    undoable: bool = False
    undo_strategy: str | None = None
    # "delete_message" | "send_correction" | None
    error: str | None = None


class InputAdapter(ABC):
    """Contract for all inbound channel adapters."""

    @abstractmethod
    async def start(self) -> None:
        """Start listening on this channel."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the channel listener."""
        ...

    @abstractmethod
    def normalise(self, raw: Any, channel: str) -> Any | None:
        """Convert raw channel payload to AgentRequest. Return None for async flows."""
        ...

    @abstractmethod
    async def acknowledge(self, raw: Any) -> None:
        """Send channel-specific acknowledgement immediately on receipt."""
        ...

    @abstractmethod
    def health(self) -> dict:
        ...


class OutputAdapter(ABC):
    """Contract for all outbound channel adapters."""

    @abstractmethod
    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a message to a user on this channel."""
        ...

    @abstractmethod
    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Send a message with interactive confirmation buttons."""
        ...

    @abstractmethod
    async def send_stream(
        self,
        user_id: str,
        stream: AsyncGenerator[str, None],
    ) -> DeliveryReceipt:
        """Send a streaming response as it arrives."""
        ...

    @abstractmethod
    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Undo a previously sent message where possible. Return True if undone."""
        ...

    @abstractmethod
    def health(self) -> dict:
        ...
