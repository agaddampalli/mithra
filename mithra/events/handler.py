"""
EventHandler ABC — ADR section 14.3.

Handlers subscribe to topic patterns and react to published events.
They must never raise — all errors are caught by AsyncioSQLiteEventBus
and logged without affecting sibling handlers (error isolation, ADR 14.7).

Handlers are responsible for idempotency (ADR 14.6):
  - Use event.event_id as the dedup key
  - INSERT OR IGNORE for SQLite writes
  - Check existence before external API calls
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mithra.events.base import BaseEvent
    from mithra.domains.context import DomainContext


class EventHandler(ABC):
    """Processes one published event.

    topic_patterns declares which topics this handler reacts to.
    Wildcards are supported: 'domain.time.*' matches all time events.

    handle() receives context=None in Phase 0 — handlers that need
    services (storage, LLM) must inject them at construction time.
    Phase 1 will wire DomainContext through once handlers are registered.
    """

    @property
    @abstractmethod
    def topic_patterns(self) -> list[str]:
        """Topics this handler subscribes to. Wildcards allowed ('*' per segment)."""

    @abstractmethod
    async def handle(
        self,
        event: "BaseEvent",
        context: "DomainContext | None",
    ) -> None:
        """Process the event.

        Must never raise — exceptions are caught by the EventBus and
        logged as errors. Returning normally indicates success.
        Idempotency is the handler's responsibility.
        """
