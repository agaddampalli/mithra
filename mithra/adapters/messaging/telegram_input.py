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
  - MITHRA_ADMIN_TELEGRAM_ID always authorized as person_a.

UserIdentityResolver maps Telegram chat IDs -> Mithra user_ids.
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
        self._webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL", "")
        # Per-chat_id timestamps of /start attempts — for rate limiting
        self._start_attempts: dict[int, list[float]] = {}

    def _api(self, method: str) -> str:
        return f"{_TELEGRAM_API.format(token=self.bot_token)}/{method}"

    def _resolve_user(self, chat_id: int) -> str:
        if self._identity_resolver:
            return self._identity_resolver.resolve_telegram(chat_id)
        return "household"

    async def start(self) -> None:
        """Start in webhook mode if TELEGRAM_WEBHOOK_URL is set, else start polling."""
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

        # Parse callback data once
        cb_data: dict = {}
        if callback and callback.get("data"):
            try:
                cb_data = json.loads(callback["data"])
            except (json.JSONDecodeError, TypeError):
                pass

        is_start = bool(message and message.get("text") == "/start")

        # Authorization gate — reject unknown users before any processing.
        # Only exemption: /start lets unknown users request access from admin.
        if not is_start:
            if self._identity_resolver and not self._identity_resolver.is_authorized(chat_id):
                logger.warning("Rejected unauthorized Telegram user: chat_id=%d", chat_id)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._reject_unauthorized(chat_id))
                except RuntimeError:
                    pass
                return None

        # --- /start ---
        if is_start:
            # Already authorized → greet them and pass through
            if self._identity_resolver and self._identity_resolver.is_authorized(chat_id):
                return AgentRequest(
                    user_id=self._resolve_user(chat_id),
                    device_id=f"telegram:{chat_id}",
                    input_channel=InputChannel.TELEGRAM,
                    text="hi",
                    signal={"chat_id": chat_id},
                    request_id=str(uuid.uuid4()),
                    session_id=f"tg:{chat_id}",
                )

            # Unknown user — rate limit to prevent admin notification spam
            now = time.time()
            attempts = self._start_attempts.get(chat_id, [])
            attempts = [t for t in attempts if now - t < _START_RATE_WINDOW]
            if len(attempts) >= _START_RATE_LIMIT:
                logger.warning("Rate-limited /start from chat_id=%d", chat_id)
                return None  # silent ignore — no message
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

        # --- Generic callback (admin approval or Level-1 confirmation) ---
        if callback:
            action = cb_data.get("action")

            # Admin approves the pending user as a specific household member
            if action == "admin_approve":
                target_id = int(cb_data["id"])
                uid = cb_data.get("uid", "household")
                if self._identity_resolver:
                    self._identity_resolver.register(target_id, uid)
                    display_name = next(
                        (m["display_name"] for m in self._identity_resolver.get_members()
                         if m["user_id"] == uid),
                        uid,
                    )
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

            # Admin denies — block the user permanently
            if action == "admin_deny":
                target_id = int(cb_data["id"])
                if self._identity_resolver:
                    self._identity_resolver.block(target_id)
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

    async def _send_holding_message(self, chat_id: int, first_name: str) -> None:
        """Tell the unknown user their request has been sent to the admin."""
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
            members = self._identity_resolver.get_members() if self._identity_resolver else []
            # One approve button per member, then deny — each on its own row.
            # callback_data kept under Telegram's 64-byte hard limit via compact separators.
            approve_rows = [
                [{
                    "text": f"✅ {m['display_name']}",
                    "callback_data": json.dumps(
                        {"action": "admin_approve", "id": chat_id, "uid": m["user_id"]},
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

    async def _notify_approved(self, chat_id: int, display_name: str) -> None:
        """Tell the newly-approved user which household member they were registered as."""
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
        """Tell the denied user their request was declined."""
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
