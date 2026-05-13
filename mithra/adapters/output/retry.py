"""
RetryableOutputAdapter — decorator that wraps any OutputAdapter with automatic
retry and dead-letter routing on exhaustion.

Pattern: decorator — RetryableOutputAdapter(adapter, dead_letter_queue) wraps any
OutputAdapter without changing the caller's interface.

Retry policy:
  3 attempts with exponential backoff: 1s → 2s → 4s (max 30s total delay).

Exhausted: write a DeadLetter to DeadLetterQueue in
  StorageAdapter domain="observability" layer="history".

Dead letters are surfaced on the health dashboard for operator review.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)
from mithra.autonomy.dead_letter import DeadLetter, DeadLetterQueue


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RetryableOutputAdapter(OutputAdapter):
    """Wraps an OutputAdapter with 3-attempt exponential backoff and dead-letter routing."""

    MAX_ATTEMPTS = 3
    BACKOFF_SECONDS = [1, 2, 4]

    def __init__(self, inner: OutputAdapter, dead_letter_queue: DeadLetterQueue) -> None:
        """
        Args:
            inner: The OutputAdapter to wrap.
            dead_letter_queue: DeadLetterQueue instance for failed deliveries.
        """
        self._inner = inner
        self._dlq = dead_letter_queue

    async def _retry(self, coro_factory, action_id: str, action_type: str, user_id: str, payload: dict):
        """Attempt coro_factory up to MAX_ATTEMPTS times with exponential backoff."""
        first_attempted_at = _now()
        last_error = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                return await coro_factory()
            except Exception as exc:
                last_error = str(exc)
                if attempt < self.MAX_ATTEMPTS - 1:
                    await asyncio.sleep(self.BACKOFF_SECONDS[attempt])
        # All attempts exhausted — write to dead-letter queue
        letter = DeadLetter(
            letter_id=str(uuid.uuid4()),
            action_id=action_id,
            user_id=user_id,
            action_type=action_type,
            payload=payload,
            error=last_error,
            attempt_count=self.MAX_ATTEMPTS,
            first_attempted_at=first_attempted_at,
            last_attempted_at=_now(),
        )
        self._dlq.enqueue(letter)
        raise RuntimeError(f"Delivery failed after {self.MAX_ATTEMPTS} attempts: {last_error}")

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send(user_id, message),
            action_id=str(uuid.uuid4()),
            action_type="send",
            user_id=user_id,
            payload={"text": message.text, "domain": message.domain},
        )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send_with_actions(user_id, message, actions),
            action_id=str(uuid.uuid4()),
            action_type="send_with_actions",
            user_id=user_id,
            payload={"text": message.text, "action_count": len(actions)},
        )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send_stream(user_id, stream),
            action_id=str(uuid.uuid4()),
            action_type="send_stream",
            user_id=user_id,
            payload={},
        )

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        return await self._inner.undo(receipt)

    def health(self) -> dict:
        inner_health = self._inner.health()
        dlq_health = self._dlq.health()
        return {
            "status": inner_health.get("status", "unknown"),
            "inner": inner_health,
            "dead_letter_queue": dlq_health,
        }
