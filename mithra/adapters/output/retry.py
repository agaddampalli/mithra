"""
RetryableOutputAdapter -- decorator that wraps any OutputAdapter with automatic
retry and dead-letter routing on exhaustion.

Pattern: decorator -- RetryableOutputAdapter(adapter, dead_letter_queue) wraps
any OutputAdapter without changing the caller's interface.

Retry policy: configurable attempts with exponential backoff (default 3x: 1s, 2s, 4s).

Exhausted: enqueue a DeadLetter and return a failure DeliveryReceipt.
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
    """Wraps an OutputAdapter with configurable retry and dead-letter routing."""

    def __init__(
        self,
        inner: OutputAdapter,
        dead_letter_queue: DeadLetterQueue,
        max_attempts: int = 3,
        backoff_seconds: list[float] | None = None,
    ) -> None:
        self._inner = inner
        self._dlq = dead_letter_queue
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds if backoff_seconds is not None else [1, 2, 4]

    async def _retry(
        self,
        coro_factory,
        action_type: str,
        user_id: str,
        payload: dict,
    ) -> DeliveryReceipt:
        """Attempt coro_factory up to max_attempts with backoff. On exhaustion,
        enqueue to dead letter and return a failure DeliveryReceipt."""
        first_attempted_at = _now()
        last_error = ""

        for attempt in range(self._max_attempts):
            try:
                result = await coro_factory()
                # If the inner adapter returns a failure receipt, treat as error
                if isinstance(result, DeliveryReceipt) and not result.success:
                    last_error = result.error or "inner adapter returned failure"
                    raise RuntimeError(last_error)
                return result
            except Exception as exc:
                last_error = str(exc)
                if attempt < self._max_attempts - 1:
                    backoff_idx = min(attempt, len(self._backoff) - 1)
                    await asyncio.sleep(self._backoff[backoff_idx])

        # All attempts exhausted -- write to dead letter queue
        self._dlq.enqueue(
            DeadLetter(
                letter_id=str(uuid.uuid4()),
                action_id=str(uuid.uuid4()),
                user_id=user_id,
                action_type=action_type,
                payload=payload,
                error=last_error,
                attempt_count=self._max_attempts,
                first_attempted_at=first_attempted_at,
                last_attempted_at=_now(),
            )
        )
        return DeliveryReceipt(
            channel="retry",
            message_id=None,
            delivered_at=_now(),
            success=False,
            undoable=False,
            error=last_error,
        )

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send(user_id, message),
            action_type=f"{self._inner.__class__.__name__}.send",
            user_id=user_id,
            payload={"text": message.text},
        )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send_with_actions(user_id, message, actions),
            action_type=f"{self._inner.__class__.__name__}.send_with_actions",
            user_id=user_id,
            payload={"text": message.text, "action_count": len(actions)},
        )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        return await self._retry(
            lambda: self._inner.send_stream(user_id, stream),
            action_type=f"{self._inner.__class__.__name__}.send_stream",
            user_id=user_id,
            payload={},
        )

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Single attempt -- no retry on undo."""
        return await self._inner.undo(receipt)

    def health(self) -> dict:
        inner_health = self._inner.health()
        return {
            "status": inner_health.get("status", "unknown"),
            "adapter": self._inner.__class__.__name__,
            **inner_health,
        }
