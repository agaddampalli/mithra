"""
TelegramOutputAdapter -- outbound Telegram Bot API adapter.

Implements OutputAdapter. Sends AgentResults back to Telegram users.

Streaming: delivered via message edits, flushed every 500 ms.

Rate limits enforced by RateLimiter on every send:
  1 message/second
  500 messages/day
  15 message edits/minute

Level-1 confirmations rendered as Telegram inline keyboard buttons.
Undo strategy: delete the message (valid within Telegram 48-hour window).

Env var: TELEGRAM_BOT_TOKEN
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

import httpx

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    MessageFormat,
    OutboundMessage,
    OutputAdapter,
)

_TELEGRAM_API = "https://api.telegram.org/bot{token}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RateLimiter:
    """Token-bucket rate limiter for Telegram API constraints."""

    def __init__(self) -> None:
        self._msg_timestamps: list[float] = []
        self._edit_timestamps: list[float] = []
        self._daily_count = 0
        self._day_start = time.time()

    async def acquire_send(self) -> None:
        now = time.time()
        # 1 msg/s
        self._msg_timestamps = [t for t in self._msg_timestamps if now - t < 1.0]
        if self._msg_timestamps:
            await asyncio.sleep(1.0 - (now - self._msg_timestamps[-1]))
        self._msg_timestamps.append(time.time())

        # 500/day
        if time.time() - self._day_start > 86400:
            self._daily_count = 0
            self._day_start = time.time()
        if self._daily_count >= 500:
            raise RuntimeError("Telegram daily message limit (500) reached")
        self._daily_count += 1

    async def acquire_edit(self) -> None:
        now = time.time()
        self._edit_timestamps = [t for t in self._edit_timestamps if now - t < 60.0]
        if len(self._edit_timestamps) >= 15:
            wait = 60.0 - (now - self._edit_timestamps[0])
            if wait > 0:
                await asyncio.sleep(wait)
        self._edit_timestamps.append(time.time())


class _NullContext:
    """Wraps a pre-created client so it can be used in an async-with block."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *args: object) -> None:
        pass


class TelegramOutputAdapter(OutputAdapter):
    """Sends messages to Telegram users with rate limiting and streaming support."""

    STREAM_FLUSH_MS = 500

    def __init__(
        self,
        bot_token: str,
        chat_id_map: dict[str, int] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_token = bot_token
        # Mithra user_id -> Telegram chat_id
        self._chat_id_map: dict[str, int] = chat_id_map or {}
        self._rate = RateLimiter()
        self._http_client = http_client  # injectable for tests

    def _api(self, method: str) -> str:
        return f"{_TELEGRAM_API.format(token=self.bot_token)}/{method}"

    def register_chat(self, user_id: str, chat_id: int) -> None:
        """Register the Telegram chat_id for a Mithra user_id."""
        self._chat_id_map[user_id] = chat_id

    def _get_chat_id(self, user_id: str) -> int:
        chat_id = self._chat_id_map.get(user_id)
        if chat_id is None:
            raise ValueError(f"No Telegram chat_id registered for user_id '{user_id}'")
        return chat_id

    def _parse_mode(self, message: OutboundMessage) -> str | None:
        return "Markdown" if message.format == MessageFormat.MARKDOWN else None

    def _client(self):
        if self._http_client is not None:
            return _NullContext(self._http_client)
        return httpx.AsyncClient()

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send a text message to a Telegram user."""
        await self._rate.acquire_send()
        chat_id = self._get_chat_id(user_id)
        body: dict = {"chat_id": chat_id, "text": message.text}
        pm = self._parse_mode(message)
        if pm:
            body["parse_mode"] = pm
        try:
            async with self._client() as client:
                resp = await client.post(self._api("sendMessage"), json=body, timeout=10)
                resp.raise_for_status()
                result = resp.json()["result"]
                msg_id = str(result["message_id"])
            return DeliveryReceipt(
                channel="telegram",
                message_id=msg_id,
                delivered_at=_now(),
                success=True,
                undoable=True,
                undo_strategy="delete_message",
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="telegram",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error=str(exc),
            )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Send a message with an inline keyboard for Level-1 confirmations."""
        await self._rate.acquire_send()
        chat_id = self._get_chat_id(user_id)
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": a.label,
                        # Flat structure so normalise() can read top-level keys directly.
                        # Telegram enforces a 64-byte limit on callback_data.
                        "callback_data": json.dumps(
                            {"action": a.action, **(a.payload or {})}
                        ),
                    }
                    for a in actions
                ]
            ]
        }
        body: dict = {
            "chat_id": chat_id,
            "text": message.text,
            "reply_markup": keyboard,
        }
        pm = self._parse_mode(message)
        if pm:
            body["parse_mode"] = pm
        try:
            async with self._client() as client:
                resp = await client.post(self._api("sendMessage"), json=body, timeout=10)
                resp.raise_for_status()
                msg_id = str(resp.json()["result"]["message_id"])
            return DeliveryReceipt(
                channel="telegram",
                message_id=msg_id,
                delivered_at=_now(),
                success=True,
                undoable=True,
                undo_strategy="delete_message",
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="telegram",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error=str(exc),
            )

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Send streaming text via message edits flushed every 500 ms."""
        await self._rate.acquire_send()
        chat_id = self._get_chat_id(user_id)
        accumulated = ""
        msg_id: str | None = None
        last_flush = time.time()

        async with self._client() as client:
            async for chunk in stream:
                accumulated += chunk
                now = time.time()
                if now - last_flush >= self.STREAM_FLUSH_MS / 1000:
                    if msg_id is None:
                        resp = await client.post(
                            self._api("sendMessage"),
                            json={"chat_id": chat_id, "text": accumulated},
                            timeout=10,
                        )
                        resp.raise_for_status()
                        msg_id = str(resp.json()["result"]["message_id"])
                    else:
                        await self._rate.acquire_edit()
                        await client.post(
                            self._api("editMessageText"),
                            json={
                                "chat_id": chat_id,
                                "message_id": int(msg_id),
                                "text": accumulated,
                            },
                            timeout=10,
                        )
                    last_flush = time.time()

            # Final flush
            if accumulated and msg_id:
                await self._rate.acquire_edit()
                await client.post(
                    self._api("editMessageText"),
                    json={
                        "chat_id": chat_id,
                        "message_id": int(msg_id),
                        "text": accumulated,
                    },
                    timeout=10,
                )
            elif accumulated and not msg_id:
                resp = await client.post(
                    self._api("sendMessage"),
                    json={"chat_id": chat_id, "text": accumulated},
                    timeout=10,
                )
                resp.raise_for_status()
                msg_id = str(resp.json()["result"]["message_id"])

        return DeliveryReceipt(
            channel="telegram",
            message_id=msg_id,
            delivered_at=_now(),
            success=True,
            undoable=bool(msg_id),
            undo_strategy="delete_message" if msg_id else None,
        )

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Delete the message (valid within 48-hour Telegram window)."""
        if not receipt.message_id or not receipt.undoable:
            return False
        if receipt.undo_strategy != "delete_message":
            return False
        try:
            msg_id_str = receipt.message_id
            if ":" in msg_id_str:
                # Format "chat_id:msg_id" stored by executor
                chat_id_str, actual_msg_id = msg_id_str.split(":", 1)
                chat_id = int(chat_id_str)
                msg_id = int(actual_msg_id)
            else:
                chat_id = 0
                msg_id = int(msg_id_str)
            async with self._client() as client:
                resp = await client.post(
                    self._api("deleteMessage"),
                    json={"chat_id": chat_id, "message_id": msg_id},
                    timeout=10,
                )
                return resp.status_code in (200, 204)
        except Exception:
            return False

    def health(self) -> dict:
        """Return adapter status."""
        return {
            "status": "ok",
            "registered_users": len(self._chat_id_map),
        }
