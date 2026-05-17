"""
TelegramInputAdapter -- inbound Telegram Bot API adapter.

Implements InputAdapter. Converts Telegram Updates into normalised AgentRequests.

Modes:
  - Webhook in production (registered against the Cloudflare tunnel URL)
  - Auto-fallback to polling if tunnel is unreachable

Normalises:
  - Text messages   -> AgentRequest with text + session history
  - Voice messages  -> AgentRequest with pending transcription signal
  - Documents       -> returns None immediately; async download via create_task
  - Callback queries -> AgentRequest with confirmation signal

UserIdentityResolver maps Telegram chat IDs -> Mithra user_ids.
Unknown sender -> user_id = "household" (safe default).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

logger = logging.getLogger(__name__)
from typing import Any, Callable, Coroutine

import httpx

from mithra.adapters.messaging import InputAdapter
from mithra.agent.interface import AgentRequest, InputChannel
from mithra.routing.session import ConversationSessionManager

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30


class UserIdentityResolver:
    """Maps Telegram IDs, device IDs, and WhatsApp numbers to Mithra user_ids.

    Built from household_config (loaded from household.yaml at startup).
    Unknown IDs fall back to "household" -- the safe default.
    """

    def __init__(self, household_config: dict) -> None:
        logger.debug("UserIdentityResolver household_config: %s", household_config)
        self._telegram_map: dict[int, str] = {}
        self._device_map: dict[str, str] = {}
        for member in household_config.get("members", []):
            uid = member["user_id"]
            tids = member.get("telegram_ids", [])
            if tids is None:
                tids = []
            if not isinstance(tids, list):
                tids = [tids]
            for tid in tids:
                try:
                    self._telegram_map[int(tid)] = uid
                except (ValueError, TypeError):
                    pass
            for did in member.get("device_ids", []):
                self._device_map[str(did)] = uid
        # Shared devices always map to "household"
        for shared in household_config.get("shared_devices", []):
            did = shared.get("device_id", "")
            if did:
                self._device_map[str(did)] = "household"

    def resolve_telegram(self, telegram_id: int) -> str:
        """Return the Mithra user_id for a Telegram chat ID, or 'household'."""
        return self._telegram_map.get(telegram_id, "household")

    def resolve_device(self, device_id: str) -> str:
        """Return the Mithra user_id for a device ID, or 'household'."""
        return self._device_map.get(str(device_id), "household")


class TelegramInputAdapter(InputAdapter):
    """Receives Telegram Updates and converts them to AgentRequests."""

    def __init__(
        self,
        bot_token: str,
        webhook_secret: str | None = None,
        session_manager: ConversationSessionManager | None = None,
        identity_resolver: UserIdentityResolver | None = None,
        on_request: Callable[[AgentRequest], Coroutine] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret
        self._session_manager = session_manager
        self._identity_resolver = identity_resolver
        self._on_request = on_request
        self._polling = False
        self._poll_task: asyncio.Task | None = None
        self._running = False

    def _api(self, method: str) -> str:
        return f"{_TELEGRAM_API.format(token=self.bot_token)}/{method}"

    def _resolve_user(self, chat_id: int) -> str:
        if self._identity_resolver:
            return self._identity_resolver.resolve_telegram(chat_id)
        return "household"

    async def start(self) -> None:
        """Start polling for updates (webhook is preferred in production)."""
        self._running = True
        self._polling = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop polling and wait for the poll task to finish."""
        self._running = False
        self._polling = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _poll_loop(self) -> None:
        logger.info("Telegram polling started")
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
                        logger.info(f"Telegram update received: {update.get('update_id')} keys={list(update.keys())}")
                        offset = update["update_id"] + 1
                        request = self.normalise(update, "telegram")
                        if request is None:
                            logger.warning(f"normalise() returned None for update {update.get('update_id')} — update={update}")
                            continue
                        if not self._on_request:
                            logger.error("_on_request callback is not set — messages will be dropped")
                            continue
                        logger.info(f"Dispatching request to on_request: user={request.user_id} text={request.text!r}")
                        task = asyncio.create_task(self._on_request(request))
                        task.add_done_callback(
                            lambda t: logger.error("on_request task failed: %s", t.exception(), exc_info=t.exception())
                            if not t.cancelled() and t.exception()
                            else None
                        )
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error(f"Telegram poll error: {e}", exc_info=True)
                    await asyncio.sleep(5)

    def normalise(self, raw: Any, channel: str) -> AgentRequest | None:
        """Convert a Telegram Update dict into an AgentRequest. Returns None for async flows."""
        if not isinstance(raw, dict):
            return None

        # --- Callback query (Level-1 confirmation response) ---
        callback = raw.get("callback_query")
        if callback:
            chat_id = callback["message"]["chat"]["id"]
            user_id = self._resolve_user(chat_id)
            try:
                data = json.loads(callback.get("data", "{}"))
            except (json.JSONDecodeError, TypeError):
                data = {}
            return AgentRequest(
                user_id=user_id,
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                signal={
                    "confirmation": data.get("action"),
                    "request_id": data.get("request_id"),
                    "action_index": data.get("action_index"),
                },
                request_id=str(uuid.uuid4()),
                session_id=f"tg:{chat_id}",
            )

        message = raw.get("message") or raw.get("edited_message")
        if not message:
            return None

        chat_id = message["chat"]["id"]
        user_id = self._resolve_user(chat_id)
        device_id = f"telegram:{chat_id}"
        session_id = f"tg:{chat_id}"
        request_id = str(uuid.uuid4())

        # Load conversation history if session manager is available
        history = []
        if self._session_manager:
            sess = self._session_manager.get_or_create(session_id, user_id, device_id)
            history = self._session_manager.load_history(sess.session_id, user_id)

        # --- Document: non-blocking; schedule async download ---
        if message.get("document"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._download_document(message, user_id, device_id, session_id)
                )
            except RuntimeError:
                pass  # no running loop (e.g. sync test context) — skip scheduling
            return None

        # --- Voice message ---
        if message.get("voice"):
            return AgentRequest(
                user_id=user_id,
                device_id=device_id,
                input_channel=InputChannel.VOICE,
                text="[Voice message - transcription pending]",
                signal={"voice_bytes_pending": True},
                request_id=request_id,
                session_id=session_id,
                conversation_history=history,
            )

        # --- Text message ---
        text = message.get("text") or message.get("caption")
        if text:
            return AgentRequest(
                user_id=user_id,
                device_id=device_id,
                input_channel=InputChannel.TELEGRAM,
                text=text,
                signal={"chat_id": chat_id},
                request_id=request_id,
                session_id=session_id,
                conversation_history=history,
            )

        return None

    async def _download_document(
        self,
        message: dict,
        user_id: str,
        device_id: str,
        session_id: str,
    ) -> None:
        """Async document download -- fires AgentRequest on completion."""
        doc = message.get("document", {})
        file_id = doc.get("file_id", "")
        if self._on_request:
            request = AgentRequest(
                user_id=user_id,
                device_id=device_id,
                input_channel=InputChannel.UPLOAD,
                signal={"file_id": file_id, "document": True},
                request_id=str(uuid.uuid4()),
                session_id=session_id,
            )
            await self._on_request(request)

    async def acknowledge(self, raw: Any) -> None:
        """Send typing indicator; answer callback queries immediately."""
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
            return

        message = raw.get("message") or raw.get("edited_message")
        if message:
            chat_id = message["chat"]["id"]
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendChatAction"),
                    json={"chat_id": chat_id, "action": "typing"},
                    timeout=5,
                )

    def health(self) -> dict:
        """Return polling/webhook mode and running status."""
        return {
            "status": "ok" if self._running else "stopped",
            "mode": "polling" if self._polling else "webhook",
        }
