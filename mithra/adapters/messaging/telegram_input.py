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

Authorization (Option B — admin approval required):
  - Unknown Telegram IDs are rejected before any processing.
  - /start is always allowed so unknown users can request access.
  - /start for already-authorized users returns AgentRequest with text="hi".
  - /start for unknown users: sends holding message to user + approval request to admin.
  - /start rate-limited to 3 attempts/hour per chat_id (unknown users only); silently ignored when over limit.
  - Admin approves via inline button → user registered as the chosen household member.
  - Admin denies via inline button → user blocked permanently.
  - MITHRA_ADMIN_TELEGRAM_ID bootstraps the admin's channel into MemberRegistry on start().

MemberRegistry is the single source of truth for member identity.
Unknown sender -> user_id = "household" (safe default).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, MITHRA_ADMIN_TELEGRAM_ID,
  TELEGRAM_WEBHOOK_URL (set to enable webhook mode; leave empty for polling)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Coroutine

import httpx

from mithra.adapters.messaging import InputAdapter
from mithra.agent.interface import AgentRequest, InputChannel
from mithra.routing.session import ConversationSessionManager

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30
_START_RATE_LIMIT = 3
_START_RATE_WINDOW = 3600


class TelegramInputAdapter(InputAdapter):
    """Receives Telegram Updates and converts them to AgentRequests."""

    def __init__(
        self,
        bot_token: str,
        webhook_secret: str | None = None,
        session_manager: ConversationSessionManager | None = None,
        member_registry: Any | None = None,
        on_request: Callable[[AgentRequest], Coroutine] | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.webhook_secret = webhook_secret
        self._session_manager = session_manager
        self._member_registry = member_registry
        self._on_request = on_request
        self._polling = False
        self._poll_task: asyncio.Task | None = None
        self._running = False
        self._webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        self._start_attempts: dict[int, list[float]] = {}

    def _api(self, method: str) -> str:
        return f"{_TELEGRAM_API.format(token=self.bot_token)}/{method}"

    def _resolve_user(self, chat_id: int) -> str:
        if self._member_registry:
            member = self._member_registry.resolve_channel("telegram", str(chat_id))
            return member.id if member else "household"
        return "household"

    def _is_authorized(self, chat_id: int) -> bool:
        if self._member_registry:
            return self._member_registry.is_authorized("telegram", str(chat_id))
        return True

    def _seed_admin_channel(self) -> None:
        """Register MITHRA_ADMIN_TELEGRAM_ID to the owner/admin member if not already in registry."""
        admin_id_str = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")
        if not admin_id_str or not self._member_registry:
            return
        try:
            admin_id = int(admin_id_str)
        except (ValueError, TypeError):
            logger.warning("Invalid MITHRA_ADMIN_TELEGRAM_ID: %r", admin_id_str)
            return
        if self._member_registry.is_authorized("telegram", str(admin_id)):
            return
        admin = self._member_registry.get_admin()
        if not admin:
            logger.warning("_seed_admin_channel: no admin member found in registry")
            return
        self._member_registry.register_channel(admin.id, "telegram", str(admin_id))
        logger.info("Seeded admin Telegram channel: %d -> %s", admin_id, admin.id)

    async def start(self) -> None:
        """Start in webhook mode if TELEGRAM_WEBHOOK_URL is set, else start polling."""
        self._seed_admin_channel()
        if self._webhook_url:
            self._running = True
            self._polling = False
            logger.info("Telegram webhook mode active — polling disabled")
            return
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
                        logger.info(
                            "Telegram update received: %s keys=%s",
                            update.get("update_id"), list(update.keys()),
                        )
                        offset = update["update_id"] + 1
                        request = self.normalise(update, "telegram")
                        if request is None:
                            logger.warning(
                                "normalise() returned None for update %s — update=%s",
                                update.get("update_id"), update,
                            )
                            continue
                        if not self._on_request:
                            logger.error("_on_request callback is not set — messages will be dropped")
                            continue
                        logger.info(
                            "Dispatching request to on_request: user=%s text=%r",
                            request.user_id, request.text,
                        )
                        task = asyncio.create_task(self._on_request(request))
                        task.add_done_callback(
                            lambda t: logger.error(
                                "on_request task failed: %s", t.exception(), exc_info=t.exception()
                            )
                            if not t.cancelled() and t.exception()
                            else None
                        )
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.error("Telegram poll error: %s", e, exc_info=True)
                    await asyncio.sleep(5)

    def normalise(self, raw: Any, channel: str) -> AgentRequest | None:
        """Convert a Telegram Update dict into an AgentRequest. Returns None for async flows."""
        if not isinstance(raw, dict):
            return None

        message = raw.get("message") or raw.get("edited_message")
        callback = raw.get("callback_query")

        if callback:
            chat_id = callback["message"]["chat"]["id"]
        elif message:
            chat_id = message["chat"]["id"]
        else:
            return None

        cb_data: dict = {}
        if callback and callback.get("data"):
            try:
                cb_data = json.loads(callback["data"])
            except (json.JSONDecodeError, TypeError):
                pass

        is_start = bool(message and message.get("text") == "/start")

        if not is_start:
            if self._member_registry and not self._is_authorized(chat_id):
                logger.warning("Rejected unauthorized Telegram user: chat_id=%d", chat_id)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._reject_unauthorized(chat_id))
                except RuntimeError:
                    pass
                return None

        if is_start:
            if self._member_registry and self._is_authorized(chat_id):
                return AgentRequest(
                    user_id=self._resolve_user(chat_id),
                    device_id=f"telegram:{chat_id}",
                    input_channel=InputChannel.TELEGRAM,
                    text="hi",
                    signal={"chat_id": chat_id},
                    request_id=str(uuid.uuid4()),
                    session_id=f"tg:{chat_id}",
                )

            now = time.time()
            attempts = self._start_attempts.get(chat_id, [])
            attempts = [t for t in attempts if now - t < _START_RATE_WINDOW]
            if len(attempts) >= _START_RATE_LIMIT:
                logger.warning("Rate-limited /start from chat_id=%d", chat_id)
                return None
            attempts.append(now)
            self._start_attempts[chat_id] = attempts

            first_name = message.get("from", {}).get("first_name", "")
            username = message.get("from", {}).get("username", "")
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._send_holding_message(chat_id, first_name))
                loop.create_task(self._request_admin_approval(chat_id, first_name, username))
            except RuntimeError:
                pass
            return None

        if callback:
            action = cb_data.get("action")

            if action == "admin_approve":
                target_id = int(cb_data["id"])
                uid = cb_data.get("uid", "household")
                if self._member_registry:
                    self._member_registry.register_channel(uid, "telegram", str(target_id))
                    member = self._member_registry.get_member(uid)
                    display_name = member.display_name if member else uid
                else:
                    display_name = uid
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._confirm_admin_action(
                        chat_id, f"✅ Approved as {display_name}.",
                    ))
                    loop.create_task(self._notify_approved(target_id, display_name))
                except RuntimeError:
                    pass
                return None

            if action == "admin_deny":
                target_id = int(cb_data["id"])
                if self._member_registry:
                    self._member_registry.block_channel("telegram", str(target_id))
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._confirm_admin_action(
                        chat_id, f"❌ User {target_id} denied.",
                    ))
                    loop.create_task(self._notify_denied(target_id))
                except RuntimeError:
                    pass
                return None

            user_id = self._resolve_user(chat_id)
            return AgentRequest(
                user_id=user_id,
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                signal={
                    "confirmation": action,
                    "request_id": cb_data.get("request_id"),
                    "action_index": cb_data.get("action_index", cb_data.get("ai", 0)),
                },
                request_id=str(uuid.uuid4()),
                session_id=f"tg:{chat_id}",
            )

        if not message:
            return None

        user_id = self._resolve_user(chat_id)
        device_id = f"telegram:{chat_id}"
        session_id = f"tg:{chat_id}"
        request_id = str(uuid.uuid4())

        history = []
        if self._session_manager:
            sess = self._session_manager.get_or_create(session_id, user_id, device_id)
            history = self._session_manager.load_history(sess.session_id, user_id)

        if message.get("document"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self._download_document(message, user_id, device_id, session_id)
                )
            except RuntimeError:
                pass
            return None

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

    async def _reject_unauthorized(self, chat_id: int) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": (
                            "Sorry, this is a private assistant. "
                            "If you're a household member, "
                            "please ask the admin to add you."
                        ),
                    },
                    timeout=5,
                )
        except Exception:
            pass

    async def _send_holding_message(self, chat_id: int, first_name: str) -> None:
        name = first_name or "there"
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": (
                            f"Hi {name}! Your access request has been sent to the admin. "
                            "You'll be notified once it's approved."
                        ),
                    },
                    timeout=5,
                )
        except Exception:
            pass

    async def _request_admin_approval(
        self, chat_id: int, first_name: str, username: str
    ) -> None:
        """Send the admin an approval request with one button per household member plus Deny."""
        admin_id_str = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID")
        if not admin_id_str:
            return
        try:
            members = self._member_registry.get_members() if self._member_registry else []
            approve_rows = [
                [{
                    "text": f"✅ {m.display_name}",
                    "callback_data": json.dumps(
                        {"action": "admin_approve", "id": chat_id, "uid": m.id},
                        separators=(',', ':'),
                    ),
                }]
                for m in members
            ]
            deny_row = [{
                "text": "❌ Deny",
                "callback_data": json.dumps(
                    {"action": "admin_deny", "id": chat_id},
                    separators=(',', ':'),
                ),
            }]
            keyboard = {"inline_keyboard": approve_rows + [deny_row]}
            text = (
                f"🔔 New access request\n\n"
                f"Name: {first_name or 'Unknown'}\n"
                f"Username: @{username or 'none'}\n"
                f"Telegram ID: {chat_id}\n\n"
                f"Approve as which member?"
            )
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": int(admin_id_str),
                        "text": text,
                        "reply_markup": keyboard,
                    },
                    timeout=5,
                )
        except Exception:
            pass

    async def _confirm_admin_action(self, admin_chat_id: int, text: str) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={"chat_id": admin_chat_id, "text": text},
                    timeout=5,
                )
        except Exception:
            pass

    async def _notify_approved(self, chat_id: int, display_name: str) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": (
                            f"✅ You've been approved as {display_name}! "
                            "You can now use Mithra."
                        ),
                    },
                    timeout=5,
                )
        except Exception:
            pass

    async def _notify_denied(self, chat_id: int) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": "❌ Your access request has been declined.",
                    },
                    timeout=5,
                )
        except Exception:
            pass

    async def _download_document(
        self,
        message: dict,
        user_id: str,
        device_id: str,
        session_id: str,
    ) -> None:
        """Async document download — fires AgentRequest on completion."""
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
