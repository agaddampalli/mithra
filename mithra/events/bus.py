"""
EventBus ABC — ADR section 14.3.

Two-way door: swap backends without touching publishers or handlers.

  Phase 0: AsyncioSQLiteEventBus (in-process, durable via SQLite)
  Phase 2: RedisEventBus         (Redis Streams, when needed)
  Phase 3: KafkaEventBus         (multi-tenant, product scale)

Migration: swap one class in bootstrap.py. All callers unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.events.base import BaseEvent
    from mithra.events.handler import EventHandler


class EventBus(ABC):
    """Abstract event bus interface.

    subscribe/publish/unsubscribe operate synchronously on subscriptions.
    publish and replay are async because they may do I/O (SQLite, network).
    """

    @abstractmethod
    async def publish(self, event: "BaseEvent") -> None:
        """Publish event to all subscribers of its topic.

        Persists the event before delivery (at-least-once guarantee).
        Causation depth > MAX_CAUSATION_DEPTH → dead-lettered, not delivered.
        Each handler runs in an isolated try/except (error isolation, ADR 14.7).
        """

    @abstractmethod
    def subscribe(self, topic_pattern: str, handler: "EventHandler") -> None:
        """Register handler for topic_pattern.

        Pattern supports '*' as a wildcard matching exactly one segment:
          'domain.time.*'  matches any time domain event
          'domain.*.*'     matches any domain event
          'inbound.gmail.*' matches all Gmail inbound signals
        """

    @abstractmethod
    def unsubscribe(self, topic_pattern: str, handler: "EventHandler") -> None:
        """Deregister handler for topic_pattern. No-op if not subscribed."""

    @abstractmethod
    async def replay(self, topic_pattern: str, since: str) -> None:
        """Re-deliver unprocessed events matching topic_pattern since ISO8601 timestamp.

        Called on startup to recover events that arrived while the process
        was restarting. Only events with processed=0 are replayed.
        """

    @abstractmethod
    async def start(self) -> None:
        """Initialise the bus and replay any unprocessed events.

        Called once by the gateway lifespan after all handlers are registered.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cleanly shut down the bus. Called in gateway lifespan teardown."""

    @abstractmethod
    def health(self) -> dict:
        """Return bus status for the health dashboard."""
