"""
Push notification output adapter -- PWA Web Push (VAPID).

Implements OutputAdapter. Sends browser push notifications to enrolled devices
using the Web Push protocol. Used for time-sensitive alerts that do not warrant
a Telegram message or voice announcement.

pywebpush is an optional dependency. If not installed, send() returns an error
receipt and health() returns degraded.

Undo strategy: cannot undo a delivered push (Reversibility.NONE).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _webpush_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except ImportError:
        return False


class PushAdapter(OutputAdapter):
    """Sends Web Push notifications to enrolled PWA devices."""

    def __init__(
        self,
        vapid_private_key: str = "",
        vapid_email: str = "",
        subscriptions: dict[str, dict] | None = None,
    ) -> None:
        self._vapid_private_key = vapid_private_key
        self._vapid_email = vapid_email
        # user_id -> single Web Push subscription dict
        self._subscriptions: dict[str, dict] = subscriptions or {}

    def register_subscription(self, user_id: str, subscription: dict) -> None:
        """Register (or replace) the browser push subscription for a user."""
        self._subscriptions[user_id] = subscription

    def _send_push(self, subscription: dict, data: dict) -> bool:
        try:
            from pywebpush import webpush, WebPushException
            webpush(
                subscription_info=subscription,
                data=json.dumps(data),
                vapid_private_key=self._vapid_private_key,
                vapid_claims={"sub": f"mailto:{self._vapid_email}"},
            )
            return True
        except Exception:
            return False

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a push notification to the registered subscription for the user."""
        if not _webpush_available():
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error="pywebpush not installed",
            )

        subscription = self._subscriptions.get(user_id)
        if not subscription:
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error=f"No subscription registered for {user_id}",
            )

        payload = {
            "title": "Mithra",
            "body": message.text,
            "priority": message.priority,
        }
        sent = self._send_push(subscription, payload)
        return DeliveryReceipt(
            channel="push",
            message_id=str(uuid.uuid4()) if sent else None,
            delivered_at=_now(),
            success=sent,
            undoable=False,
            error=None if sent else "Push delivery failed",
        )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Include action buttons in the push payload for PWA notification actions."""
        if not _webpush_available():
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error="pywebpush not installed",
            )

        subscription = self._subscriptions.get(user_id)
        if not subscription:
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error=f"No subscription registered for {user_id}",
            )

        payload = {
            "title": "Mithra",
            "body": message.text,
            "priority": message.priority,
            "actions": [{"action": a.action, "title": a.label} for a in actions],
        }
        sent = self._send_push(subscription, payload)
        return DeliveryReceipt(
            channel="push",
            message_id=str(uuid.uuid4()) if sent else None,
            delivered_at=_now(),
            success=sent,
            undoable=False,
        )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Buffer stream then send as a single push when complete."""
        text = ""
        async for chunk in stream:
            text += chunk
        return await self.send(user_id, OutboundMessage(text=text))

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Push notifications cannot be recalled (Reversibility.NONE)."""
        return False

    def health(self) -> dict:
        """Return push adapter health."""
        available = _webpush_available()
        return {
            "status": "ok" if available else "degraded",
            "registered_subscriptions": len(self._subscriptions),
            "pywebpush_available": available,
        }
