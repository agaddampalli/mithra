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

Authorization:
  - Unknown Telegram IDs are rejected before any processing.
  - /start is always allowed so unknown users can self-register.
  - /start is rate-limited to 3 attempts per hour per chat_id.
  - Register callbacks (action="register") are allowed for unknown users
    so they can complete registration after pressing the member button.
  - MITHRA_ADMIN_TELEGRAM_ID always authorized as person_a.

UserIdentityResolver maps Telegram chat IDs -> Mithra user_ids.
Unknown sender -> user_id = "household" (safe default).

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, MITHRA_ADMIN_TELEGRAM_ID
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)
from typing import Any, Callable, Coroutine

import httpx

from mithra.adapters.messaging import InputAdapter
from mithra.agent.interface import AgentRequest, InputChannel
from mithra.routing.session import ConversationSessionManager

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_POLL_TIMEOUT = 30
_START_RATE_LIMIT = 3       # max /start attempts per window
_START_RATE_WINDOW = 3600   # seconds (1 hour)


class UserIdentityResolver:
    """Maps Telegram IDs, device IDs, and WhatsApp numbers to Mithra user_ids.

    Built from household_config (loaded from household.yaml at startup).
    Dynamic registrations are persisted via storage_adapter so they survive
    restarts. Unknown IDs fall back to "household" -- the safe default.

    Authorization: only IDs in _telegram_map are considered known members.
    is_authorized() is the gate used by TelegramInputAdapter.
    """

    def __init__(self, household_config: dict, storage_adapter=None) -> None:
        logger.debug("UserIdentityResolver household_config: %s", household_config)
        self._household_config = household_config
        self._storage = storage_adapter
        self._telegram_map: dict[int, str] = {}
        self._device_map: dict[str, str] = {}
        self._blocked: set[int] = set()
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
        # Admin is always authorized — bootstraps access before registration.
        # Ensures the admin can reach the bot even on a fresh install before
        # household.yaml is populated with their telegram_id.
        admin_id_str = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")
        if admin_id_str:
            try:
                self._telegram_map[int(admin_id_str)] = "person_a"
            except (ValueError, TypeError):
                logger.warning("Invalid MITHRA_ADMIN_TELEGRAM_ID: %r", admin_id_str)
        # Overlay with any previously persisted dynamic registrations
        if storage_adapter:
            self._load_from_storage()

    def _load_from_storage(self) -> None:
        """Load saved Telegram ID → user_id mappings and blocked IDs from storage."""
        try:
            records = self._storage.list("identity", "state", "household")
            for record in records:
                if record.data.get("type") == "telegram_mapping":
                    self._telegram_map[int(record.data["telegram_id"])] = (
                        record.data["user_id"]
                    )
                elif record.data.get("type") == "blocked":
                    self._blocked.add(int(record.data["telegram_id"]))
        except Exception:
            pass

    def register(self, telegram_id: int, user_id: str) -> None:
        """Persist a new Telegram ID → user_id mapping."""
        self._telegram_map[telegram_id] = user_id
        if self._storage:
            import datetime
            from mithra.storage.adapter import StorageRecord
            self._storage.write(StorageRecord(
                domain="identity",
                key=f"telegram:{telegram_id}",
                layer="state",
                data={
                    "type": "telegram_mapping",
                    "telegram_id": telegram_id,
                    "user_id": user_id,
                },
                user_id="household",
                version=1,
                updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))

    def block(self, telegram_id: int) -> None:
        """Permanently block a telegram_id. Blocked users are rejected even if in telegram_map."""
        self._blocked.add(telegram_id)
        if self._storage:
            import datetime
            from mithra.storage.adapter import StorageRecord
            self._storage.write(StorageRecord(
                domain="identity",
                key=f"blocked:{telegram_id}",
                layer="state",
                data={"type": "blocked", "telegram_id": telegram_id},
                user_id="household",
                version=1,
                updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ))

    def is_authorized(self, telegram_id: int) -> bool:
        """Return True only if this telegram_id is a known member and not blocked."""
        if telegram_id in self._blocked:
            return False
        return telegram_id in self._telegram_map

    def get_members(self) -> list[dict]:
        """Return household members for the registration prompt."""
        return self._household_config.get("members", [])

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
        # Per-chat_id timestamps of /start attempts — for rate limiting
        self._start_attempts: dict[int, list[float]] = {}

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

        message = raw.get("message") or raw.get("edited_message")
        callback = raw.get("callback_query")

        # Extract chat_id early — needed for auth check before any other processing
        if callback:
            chat_id = callback["message"]["chat"]["id"]
        elif message:
            chat_id = message["chat"]["id"]
        else:
            return None

        # Classify this update so we know which paths bypass auth
        is_start = bool(message and message.get("text") == "/start")
        is_register_callback = False
        cb_data: dict = {}
        if callback and callback.get("data"):
            try:
                cb_data = json.loads(callback["data"])
                is_register_callback = cb_data.get("action") == "register"
            except (json.JSONDecodeError, TypeError):
                pass

        # Authorization gate — reject unknown users before any processing.
        # Exemptions: /start (lets users begin registration) and register callbacks
        # (lets users complete registration by tapping the member-selection button).
        if not is_start and not is_register_callback:
            if self._identity_resolver and not self._identity_resolver.is_authorized(chat_id):
                logger.warning("Rejected unauthorized Telegram user: chat_id=%d", chat_id)
                first_name = ""
                username = ""
                msg_text = ""
                if message:
                    sender = message.get("from", {})
                    first_name = sender.get("first_name", "")
                    username = sender.get("username", "")
                    msg_text = message.get("text", "")
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._reject_unauthorized(chat_id))
                    loop.create_task(self._notify_admin_of_unknown_user(
                        chat_id, first_name, username, msg_text,
                    ))
                except RuntimeError:
                    pass
                return None

        # --- /start — self-registration flow ---
        if is_start:
            # Rate limit: max 3 /start attempts per hour to prevent abuse
            now = time.time()
            attempts = self._start_attempts.get(chat_id, [])
            attempts = [t for t in attempts if now - t < _START_RATE_WINDOW]
            if len(attempts) >= _START_RATE_LIMIT:
                logger.warning("Rate-limited /start from chat_id=%d", chat_id)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._reject_unauthorized(chat_id))
                except RuntimeError:
                    pass
                return None
            attempts.append(now)
            self._start_attempts[chat_id] = attempts

            first_name = message.get("from", {}).get("first_name", "")
            return AgentRequest(
                user_id="household",
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                text="/start",
                signal={
                    "registration": True,
                    "chat_id": chat_id,
                    "first_name": first_name,
                },
                request_id=str(uuid.uuid4()),
                session_id=f"tg:{chat_id}",
            )

        # --- Registration callback (member selection button tapped) ---
        if is_register_callback:
            user_id = cb_data.get("user_id", "")
            if self._identity_resolver and user_id:
                self._identity_resolver.register(chat_id, user_id)
            return AgentRequest(
                user_id=user_id,
                device_id=f"telegram:{chat_id}",
                input_channel=InputChannel.TELEGRAM,
                signal={
                    "registered": True,
                    "user_id": user_id,
                },
                request_id=str(uuid.uuid4()),
                session_id=f"tg:{chat_id}",
            )

        # --- Generic callback (Level-1 confirmation response) ---
        if callback:
            action = cb_data.get("action")

            # Admin approve — register the pending user as household member
            if action == "admin_allow":
                allowed_id = int(cb_data["id"])
                if self._identity_resolver:
                    self._identity_resolver.register(allowed_id, "household")
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._confirm_admin_action(
                        chat_id,
                        f"✅ User {allowed_id} has been allowed.",
                    ))
                    loop.create_task(self._notify_allowed_user(allowed_id))
                except RuntimeError:
                    pass
                return None

            # Admin deny — block the user permanently
            if action == "admin_block":
                blocked_id = int(cb_data["id"])
                if self._identity_resolver:
                    self._identity_resolver.block(blocked_id)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._confirm_admin_action(
                        chat_id,
                        f"❌ User {blocked_id} has been blocked.",
                    ))
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
                    "action_index": cb_data.get("action_index"),
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

    async def _reject_unauthorized(self, chat_id: int) -> None:
        """Send a single rejection message to an unknown user."""
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

    async def _notify_admin_of_unknown_user(
        self,
        chat_id: int,
        first_name: str,
        username: str,
        message_text: str,
    ) -> None:
        """Notify the admin when an unknown user attempts access, with Allow/Block buttons."""
        admin_id_str = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID")
        if not admin_id_str:
            return
        try:
            # Keep callback_data under Telegram's 64-byte limit.
            # first_name is visible in the message text — no need to repeat it.
            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "✅ Allow",
                        "callback_data": json.dumps({
                            "action": "admin_allow",
                            "id": chat_id,
                        }),
                    },
                    {
                        "text": "❌ Block",
                        "callback_data": json.dumps({
                            "action": "admin_block",
                            "id": chat_id,
                        }),
                    },
                ]]
            }
            text = (
                f"⚠️ Unknown user tried to access Mithra\n\n"
                f"Name: {first_name}\n"
                f"Username: @{username or 'none'}\n"
                f"Telegram ID: {chat_id}\n"
                f"Message: '{message_text[:50]}'\n\n"
                f"Do you want to allow this person?"
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
        """Send a confirmation message back to the admin after they approve or block."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={"chat_id": admin_chat_id, "text": text},
                    timeout=5,
                )
        except Exception:
            pass

    async def _notify_allowed_user(self, chat_id: int) -> None:
        """Tell a newly-allowed user that they now have access."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    self._api("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": "✅ You have been granted access to Mithra. Say hello!",
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
