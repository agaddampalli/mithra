"""
Mithra Event System (MES) — ADR section 14.

Public API for the event bus, event types, and adapter/handler ABCs.
"""

from mithra.events.base import (
    MAX_CAUSATION_DEPTH,
    BaseEvent,
    DomainEvent,
    InboundEvent,
)
from mithra.events.bus import EventBus
from mithra.events.handler import EventHandler
from mithra.events.inbound import InboundAdapter
from mithra.events.asyncio_sqlite_bus import AsyncioSQLiteEventBus

__all__ = [
    "MAX_CAUSATION_DEPTH",
    "BaseEvent",
    "InboundEvent",
    "DomainEvent",
    "EventBus",
    "EventHandler",
    "InboundAdapter",
    "AsyncioSQLiteEventBus",
]
