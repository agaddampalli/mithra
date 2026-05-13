"""
Push notification output adapter — PWA Web Push.

Implements OutputAdapter. Sends browser push notifications to enrolled devices
using the Web Push protocol (VAPID). Used for time-sensitive alerts that don't
warrant a Telegram message or voice announcement.

Undo strategy: cannot undo a delivered push (Reversibility.NONE).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from pywebpush import webpush, WebPushException

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PushAdapter(OutputAdapter):
    """Sends Web Push notifications to enrolled PWA devices."""

    def __init__(self, vapid_private_key: str, vapid_email: str) -> None:
        self.vapid_private_key = vapid_private_key
        self.vapid_email = vapid_email
        # user_id → list of push subscription dicts (endpoint, keys)
        self._subscriptions: dict[str, list[dict]] = {}

    def register_subscription(self, user_id: str, subscription: dict) -> None:
        """Register a browser push subscription for a user."""
        self._subscriptions.setdefault(user_id, [])
        if subscription not in self._subscriptions[user_id]:
            self._subscriptions[user_id].append(subscription)

    def _send_push(self, subscription: dict, data: dict) -> bool:
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(data),
                vapid_private_key=self.vapid_private_key,
                vapid_claims={"sub": f"mailto:{self.vapid_email}"},
            )
            return True
        except WebPushException:
            return False

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a push notification to all subscriptions for the user."""
        subs = self._subscriptions.get(user_id, [])
        if not subs:
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                undo_strategy=None,
                error=f"No push subscription registered for {user_id}",
            )
        payload = {"title": "Mithra", "body": message.text, "domain": message.domain}
        sent = any(self._send_push(sub, payload) for sub in subs)
        return DeliveryReceipt(
            channel="push",
            message_id=str(uuid.uuid4()),
            delivered_at=_now(),
            success=sent,
            undoable=False,
            undo_strategy=None,
            error=None if sent else "Push delivery failed for all subscriptions",
        )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Include action labels in the push payload."""
        action_labels = [{"action": a.action, "title": a.label} for a in actions]
        subs = self._subscriptions.get(user_id, [])
        if not subs:
            return DeliveryReceipt(
                channel="push",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                undo_strategy=None,
                error=f"No push subscription registered for {user_id}",
            )
        payload = {
            "title": "Mithra",
            "body": message.text,
            "domain": message.domain,
            "actions": action_labels,
        }
        sent = any(self._send_push(sub, payload) for sub in subs)
        return DeliveryReceipt(
            channel="push",
            message_id=str(uuid.uuid4()),
            delivered_at=_now(),
            success=sent,
            undoable=False,
            undo_strategy=None,
        )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Collect stream and send as a single push."""
        text = ""
        async for chunk in stream:
            text += chunk
        return await self.send(user_id, OutboundMessage(text=text))

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Push notifications cannot be recalled (Reversibility.NONE)."""
        return False

    def health(self) -> dict:
        """Return push adapter health."""
        return {
            "status": "ok",
            "registered_users": len(self._subscriptions),
            "total_subscriptions": sum(len(v) for v in self._subscriptions.values()),
        }
