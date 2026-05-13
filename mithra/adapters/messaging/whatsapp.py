"""
WhatsAppAdapter — outbound-only social messaging via Baileys (Node.js bridge).

Implements OutputAdapter. Used for social outbound actions only — birthday
messages, event RSVPs, etc. Not used for household ↔ agent interaction
(that goes through Telegram).

The Baileys bridge runs as a separate Docker container (whatsapp-bridge).
This adapter communicates with it over an internal HTTP API.

Undo strategy: send a follow-up correction message (PARTIAL reversibility).

Env var: WHATSAPP_SESSION_PATH
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WhatsAppAdapter(OutputAdapter):
    """Outbound WhatsApp adapter backed by the Baileys Node.js bridge."""

    def __init__(self, bridge_url: str = "http://whatsapp-bridge:3000") -> None:
        self.bridge_url = bridge_url.rstrip("/")

    async def _post(self, endpoint: str, payload: dict) -> dict:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.bridge_url}{endpoint}",
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a text message to a WhatsApp number.

        user_id is expected to contain the E.164 phone number for social sends
        (e.g. "+447700900000") or a Mithra user_id that the bridge resolves.
        """
        try:
            result = await self._post("/send", {"to": user_id, "text": message.text})
            return DeliveryReceipt(
                channel="whatsapp",
                message_id=result.get("message_id", str(uuid.uuid4())),
                delivered_at=_now(),
                success=True,
                undoable=True,
                undo_strategy="send_correction",
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="whatsapp",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                undo_strategy=None,
                error=str(exc),
            )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """WhatsApp has no native buttons — append action labels as text."""
        action_text = "\n".join(f"• Reply *{a.label}* to {a.action}" for a in actions)
        full_text = f"{message.text}\n\n{action_text}"
        return await self.send(user_id, OutboundMessage(
            text=full_text,
            format=message.format,
            domain=message.domain,
            priority=message.priority,
        ))

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Collect stream and send as a single message (WhatsApp has no streaming)."""
        full_text = ""
        async for chunk in stream:
            full_text += chunk
        return await self.send(user_id, OutboundMessage(text=full_text))

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """WhatsApp undo = send a correction follow-up (PARTIAL reversibility)."""
        if not receipt.undoable or not receipt.message_id:
            return False
        try:
            await self._post("/send_correction", {"original_message_id": receipt.message_id})
            return True
        except Exception:
            return False

    def health(self) -> dict:
        """Ping the Baileys bridge."""
        try:
            import httpx as _httpx
            resp = _httpx.get(f"{self.bridge_url}/health", timeout=5)
            bridge_ok = resp.status_code == 200
            return {"status": "ok" if bridge_ok else "degraded", "bridge_url": self.bridge_url}
        except Exception as exc:
            return {"status": "error", "error": str(exc), "bridge_url": self.bridge_url}
