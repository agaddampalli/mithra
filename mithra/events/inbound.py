"""
InboundAdapter ABC — ADR section 14.3.

Connects an external source (Gmail, HA sensor, Telegram webhook) to the EventBus.
Each adapter normalises raw external payloads into InboundEvents and publishes them.

Phase 0: abstract only. First concrete implementation: GmailInboundAdapter (MES-14).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.events.base import InboundEvent
    from mithra.events.bus import EventBus


class InboundAdapter(ABC):
    """Connects an external source to the EventBus.

    Subclasses implement polling or push-based listening for their source.
    _normalise() converts raw source payloads into InboundEvent objects.
    """

    @property
    @abstractmethod
    def source(self) -> str:
        """Short identifier for the external source, e.g. 'gmail', 'ha_sensor'."""

    @abstractmethod
    async def start(self) -> None:
        """Begin polling or listening for inbound events.

        Called by the gateway lifespan after the EventBus is started.
        Implementations should run as a background task where needed.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop polling cleanly. Called during gateway lifespan teardown."""

    @abstractmethod
    async def _normalise(self, raw: dict) -> "InboundEvent":
        """Convert raw source payload to a typed InboundEvent.

        Sets source, raw_data, and computes the topic string.
        Must NOT call EventBus.publish() — that is the adapter's public path.
        """

    def health(self) -> dict:
        """Return adapter health status for the health dashboard."""
        return {"source": self.source, "status": "unknown"}
