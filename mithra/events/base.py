"""
MES base event dataclasses — ADR section 14.3.

BaseEvent is the root of the MES event hierarchy.
InboundEvent wraps external signals (Gmail, HA sensor, scheduler).
DomainEvent carries internal side effects between domains.

These coexist with the legacy DomainEvent in mithra.domains.result
until Phase 1 migration (MES-09 / MES-10).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_CAUSATION_DEPTH = 5


def _new_event_id() -> str:
    return "evt_" + secrets.token_hex(8)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BaseEvent:
    """Root event type — all MES events inherit from this.

    event_id is globally unique and immutable.
    Used for idempotency — never regenerate on retry.
    causation_depth > MAX_CAUSATION_DEPTH triggers dead-letter.
    """

    event_id: str
    topic: str
    household_id: str
    user_id: str
    timestamp: str
    causation_id: str | None = None
    causation_depth: int = 0

    @classmethod
    def create(
        cls,
        topic: str,
        household_id: str,
        user_id: str,
        causation_id: str | None = None,
        causation_depth: int = 0,
        **kwargs: Any,
    ) -> "BaseEvent":
        return cls(
            event_id=_new_event_id(),
            topic=topic,
            household_id=household_id,
            user_id=user_id,
            timestamp=_now_iso(),
            causation_id=causation_id,
            causation_depth=causation_depth,
            **kwargs,
        )


@dataclass
class InboundEvent(BaseEvent):
    """External signal not yet classified into a domain action.

    Topics follow: inbound.{source}.{signal_type}
    e.g. inbound.gmail.email_received

    classified stays False until the routing layer processes the event
    and calls the appropriate domain tool.
    """

    source: str = ""
    raw_data: dict = field(default_factory=dict)
    classified: bool = False

    @classmethod
    def create(  # type: ignore[override]
        cls,
        topic: str,
        household_id: str,
        user_id: str,
        source: str,
        raw_data: dict | None = None,
        causation_id: str | None = None,
        causation_depth: int = 0,
    ) -> "InboundEvent":
        return cls(
            event_id=_new_event_id(),
            topic=topic,
            household_id=household_id,
            user_id=user_id,
            timestamp=_now_iso(),
            causation_id=causation_id,
            causation_depth=causation_depth,
            source=source,
            raw_data=raw_data or {},
            classified=False,
        )


@dataclass
class NotificationEvent(BaseEvent):
    """Fire-and-forget delivery request to an output channel.

    Topics follow: notification.{channel}.{action}
    e.g. notification.telegram.send

    NOT replayed on restart — replaying notification events causes
    duplicate messages. MES-10 filters these from _load_unprocessed().
    """

    payload: dict = field(default_factory=dict)

    @classmethod
    def create(  # type: ignore[override]
        cls,
        topic: str,
        household_id: str,
        user_id: str,
        payload: dict | None = None,
        causation_id: str | None = None,
        causation_depth: int = 0,
    ) -> "NotificationEvent":
        return cls(
            event_id=_new_event_id(),
            topic=topic,
            household_id=household_id,
            user_id=user_id,
            timestamp=_now_iso(),
            causation_id=causation_id,
            causation_depth=causation_depth,
            payload=payload or {},
        )


@dataclass
class DomainEvent(BaseEvent):
    """Internal side effect emitted after tool execution and autonomy confirmation.

    Topics follow: domain.{source_domain}.{event_type}
    e.g. domain.time.appointment_created

    ONE-WAY DOOR: DomainEvents only fire AFTER the user confirms.
    Firing before confirmation creates phantom cross-domain side effects.
    See ADR section 9.10 and 14.1.

    request_id links back to the originating AgentRequest / A2A task_id.
    """

    event_type: str = ""
    source_domain: str = ""
    payload: dict = field(default_factory=dict)
    request_id: str = ""

    @classmethod
    def create(  # type: ignore[override]
        cls,
        event_type: str,
        source_domain: str,
        household_id: str,
        user_id: str,
        request_id: str,
        payload: dict | None = None,
        causation_id: str | None = None,
        causation_depth: int = 0,
    ) -> "DomainEvent":
        topic = f"domain.{source_domain}.{event_type}"
        return cls(
            event_id=_new_event_id(),
            topic=topic,
            household_id=household_id,
            user_id=user_id,
            timestamp=_now_iso(),
            causation_id=causation_id,
            causation_depth=causation_depth,
            event_type=event_type,
            source_domain=source_domain,
            payload=payload or {},
            request_id=request_id,
        )
