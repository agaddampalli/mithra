"""
WhatsAppAdapter -- outbound-only social messaging via Baileys (Node.js bridge).

Implements OutputAdapter. Used for social outbound actions only -- birthday
messages, event RSVPs, check-ins from personal WhatsApp number. Not used for
household <-> agent interaction (that goes through Telegram).

Baileys runs as a separate Docker container (whatsapp-bridge). This adapter
calls it via HTTP. Reversibility is NONE -- WhatsApp has no unsend API.

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


class _NullContext:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *args: object) -> None:
        pass


class WhatsAppAdapter(OutputAdapter):
    """Outbound WhatsApp adapter backed by the Baileys Node.js bridge."""

    def __init__(
        self,
        bridge_url: str = "http://whatsapp-bridge:3000",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self._http_client = http_client
        self._phone_map: dict[str, str] = {}

    def _client(self):
        if self._http_client is not None:
            return _NullContext(self._http_client)
        return httpx.AsyncClient()

    def register_phone(self, user_id: str, phone: str) -> None:
        """Register a WhatsApp phone number for a Mithra user_id."""
        self._phone_map[user_id] = phone

    def _get_phone(self, user_id: str) -> str:
        """Return the phone number for user_id, or user_id itself as fallback."""
        return self._phone_map.get(user_id, user_id)

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a text message to a WhatsApp number via the Baileys bridge."""
        phone = self._get_phone(user_id)
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self.bridge_url}/send",
                    json={"phone": phone, "text": message.text},
                    timeout=15,
                )
                resp.raise_for_status()
                result = resp.json()
            return DeliveryReceipt(
                channel="whatsapp",
                message_id=result.get("message_id", str(uuid.uuid4())),
                delivered_at=_now(),
                success=True,
                undoable=False,
                undo_strategy=None,
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
        """WhatsApp has no native buttons -- list options as numbered text."""
        options = "\n".join(f"{i + 1}. {a.label}" for i, a in enumerate(actions))
        full_text = f"{message.text}\n\n{options}"
        return await self.send(
            user_id,
            OutboundMessage(
                text=full_text,
                format=message.format,
                domain=message.domain,
                priority=message.priority,
            ),
        )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Buffer entire stream then send as one message (WhatsApp has no edit API)."""
        full_text = ""
        async for chunk in stream:
            full_text += chunk
        return await self.send(user_id, OutboundMessage(text=full_text))

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Cannot undo WhatsApp messages (Reversibility.NONE)."""
        return False

    def health(self) -> dict:
        """Return bridge connectivity status."""
        try:
            resp = httpx.get(f"{self.bridge_url}/health", timeout=5)
            connected = resp.status_code == 200
            data = resp.json() if connected else {}
            return {
                "status": "ok" if connected else "degraded",
                "whatsapp_connected": data.get("connected", connected),
            }
        except Exception as exc:
            return {
                "status": "error",
                "whatsapp_connected": False,
                "error": str(exc),
            }
