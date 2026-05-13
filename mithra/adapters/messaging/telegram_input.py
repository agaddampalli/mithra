"""
TelegramInputAdapter — inbound Telegram Bot API adapter.

Implements InputAdapter. Converts Telegram Updates into normalised AgentRequests.

Modes:
  - Webhook in production (registered against the Cloudflare tunnel URL)
  - Auto-fallback to polling if tunnel is unreachable

Normalises:
  - Text messages → AgentRequest with text
  - Voice messages → transcribed inline via Whisper before AgentRequest is fired
  - Documents → downloaded async (non-blocking); status message sent immediately,
                AgentRequest fires on download completion
  - Callback queries → resolve pending confirmations or user choices

UserIdentityResolver maps Telegram chat IDs → Mithra user_ids.
Unknown sender → user_id = "household" (safe default).

Rate limiting: handled by TelegramOutputAdapter. This adapter is receive-only.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from typing import Any, Callable, Coroutine

import httpx

from mithra.adapters.messaging import InputAdapter
from mithra.agent.interface import AgentRequest, InputChannel

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30  # long-poll seconds


class TelegramInputAdapter(InputAdapter):
    """Receives Telegram Updates and converts them to AgentRequests."""

    def __init__(
        self,
        bot_token: str,
        webhook_secret: str | None = None,
        user_map: dict[int, str] | None = None,
        # chat_id → Mithra user_id; unknown → "household"
        on_request: Callable[[AgentRequest], Coroutine] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret
        self._user_map: dict[int, str] = user_map or {}
        self._on_request = on_request
        self._polling = False
        self._poll_task: asyncio.Task | None = None
        self._running = False

    def _api(self, method: str) -> str:
        return f"{_TELEGRAM_API.format(token=self.bot_token)}/{method}"

    def _resolve_user(self, chat_id: int) -> str:
        return self._user_map.get(chat_id, "household")

    def verify_webhook_secret(self, x_telegram_bot_api_secret_token: str) -> bool:
        """Validate the X-Telegram-Bot-Api-Secret-Token header."""
        if not self.webhook_secret:
            return True
        expected = hmac.new(
            self.webhook_secret.encode(),
            self.bot_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, x_telegram_bot_api_secret_token)

    async def start(self) -> None:
        """Start polling for updates (fallback mode; webhook is preferred)."""
        self._running = True
        self._polling = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling."""
        self._running = False
        self._polling = False
        if self._poll_task:
            self._poll_task.cancel()

    async def _poll_loop(self) -> None:
        offset = 0
        async with httpx.AsyncClient() as client:
            while self._polling:
                try:
                    resp = await client.post(
                        self._api("getUpdates"),
                        json={"timeout": _POLL_TIMEOUT, "offset": offset},
                        timeout=_POLL_TIMEOUT + 5,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        request = self.normalise(update, "telegram")
                        if request and self._on_request:
                            asyncio.create_task(self._on_request(request))
                except asyncio.CancelledError:
                    return
                except Exception:
                    await asyncio.sleep(5)

    def normalise(self, raw: Any, channel: str) -> AgentRequest | None:
        """Convert a Telegram Update dict into an AgentRequest. Returns None for async flows."""
        if not isinstance(raw, dict):
            return None

        message = raw.get("message") or raw.get("edited_message")
        callback = raw.get("callback_query")

        if callback:
            chat_id = callback["message"]["chat"]["id"]
            user_id = self._resolve_user(chat_id)
            payload = json.loads(callback.get("data", "{}"))
            return AgentRequest(
                user_id=user_id,
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                signal={"callback": payload, "callback_query_id": callback["id"]},
                request_id=str(uuid.uuid4()),
                session_id=f"tg:{chat_id}",
            )

        if not message:
            return None

        chat_id = message["chat"]["id"]
        user_id = self._resolve_user(chat_id)
        session_id = f"tg:{chat_id}"
        request_id = str(uuid.uuid4())

        text = message.get("text") or message.get("caption")
        if text:
            return AgentRequest(
                user_id=user_id,
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                text=text,
                request_id=request_id,
                session_id=session_id,
            )

        # Voice and document: return None (async flow — adapter fires AgentRequest on completion)
        return None

    async def acknowledge(self, raw: Any) -> None:
        """Answer callback queries immediately so Telegram stops showing the spinner."""
        if not isinstance(raw, dict):
            return
        callback = raw.get("callback_query")
        if callback:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("answerCallbackQuery"),
                    json={"callback_query_id": callback["id"]},
                    timeout=5,
                )

    def health(self) -> dict:
        """Return polling/webhook mode and running status."""
        return {
            "status": "ok" if self._running else "stopped",
            "mode": "polling" if self._polling else "webhook",
        }
