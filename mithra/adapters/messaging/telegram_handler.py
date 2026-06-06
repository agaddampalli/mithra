"""
TelegramSignalHandler — handles Telegram-specific signals and response dispatch.

Encapsulates every piece of Telegram wire-format knowledge (chat_id resolution,
callback_data payload shape, confirmation button structure) so gateway.py stays
a channel-agnostic lifecycle orchestrator with zero Telegram imports.

Single responsibility: Telegram protocol ↔ Mithra domain model.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from mithra.adapters.messaging import ActionButton, OutboundMessage

if TYPE_CHECKING:
    from mithra.agent.interface import AgentRequest
    from mithra.autonomy.engine import PendingAction

_logger = logging.getLogger(__name__)


class TelegramSignalHandler:
    """Translates Telegram signals into domain actions and dispatches responses.

    Created once at startup and injected into gateway._state so _process_request
    can delegate all channel-specific work here.
    """

    def __init__(
        self,
        telegram_output: Any,
        engine: Any | None = None,
        executor: Any | None = None,
        household_config: dict | None = None,
        oauth_manager: Any | None = None,
        base_url: str = "https://assistant.mithra-app.com",
        member_registry: Any | None = None,
        storage_adapter: Any | None = None,
    ) -> None:
        self._out = telegram_output
        self._engine = engine
        self._executor = executor
        self._household_config = household_config or {}
        self._oauth_manager = oauth_manager
        self._base_url = base_url.rstrip("/")
        self._member_registry = member_registry
        self._storage = storage_adapter

    # ------------------------------------------------------------------
    # Chat-id resolution
    # ------------------------------------------------------------------

    @staticmethod
    def chat_id_from_request(agent_request: "AgentRequest") -> int | None:
        """Resolve Telegram chat_id from device_id, with signal fallback.

        TelegramInputAdapter always sets device_id="telegram:{chat_id}" and
        signal["chat_id"] for text messages. Either source is sufficient.
        """
        device_id = agent_request.device_id or ""
        if ":" in device_id:
            try:
                return int(device_id.split(":", 1)[1])
            except (ValueError, IndexError):
                pass
        raw = (agent_request.signal or {}).get("chat_id")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        return None

    # ------------------------------------------------------------------
    # Response dispatch
    # ------------------------------------------------------------------

    async def send(
        self,
        agent_request: "AgentRequest",
        text: str,
        actions: list[ActionButton] | None = None,
    ) -> None:
        """Send a message back to the originating Telegram chat."""
        chat_id = self.chat_id_from_request(agent_request)
        _logger.info(
            "send: user=%s chat_id=%s text_len=%d actions=%d",
            agent_request.user_id, chat_id, len(text or ""), len(actions or []),
        )
        if not chat_id:
            _logger.warning("send: could not resolve chat_id — skipping")
            return
        self._out.register_chat(agent_request.user_id, chat_id)
        msg = OutboundMessage(text=text)
        try:
            if actions:
                await self._out.send_with_actions(agent_request.user_id, msg, actions)
            else:
                await self._out.send(agent_request.user_id, msg)
        except Exception as exc:
            _logger.error("send failed: %s", exc, exc_info=True)

    async def notify_user(self, user_id: str, text: str) -> None:
        """Send a notification to a user looked up by their Mithra user_id."""
        chat_id: int | None = None
        if self._member_registry:
            chat_id_str = self._member_registry.get_channel_id(user_id, "telegram")
            if chat_id_str:
                try:
                    chat_id = int(chat_id_str)
                except (ValueError, TypeError):
                    pass
        if not chat_id:
            _logger.warning("notify_user: no chat_id for user_id=%r", user_id)
            return
        self._out.register_chat(user_id, chat_id)
        try:
            await self._out.send(user_id, OutboundMessage(text=text))
        except Exception as exc:
            _logger.error("notify_user failed for %s: %s", user_id, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Confirmation buttons
    # ------------------------------------------------------------------

    def make_reconnect_button(self, user_id: str) -> ActionButton:
        """Return a URL button that starts the Google OAuth reconnect flow."""
        url = f"{self._base_url}/auth/google?user_id={user_id}"
        return ActionButton(
            label="🔗 Reconnect Google",
            action="url",
            payload={"url": url},
        )

    @staticmethod
    def make_confirmation_buttons(pending: "PendingAction") -> list[ActionButton]:
        """Return ✅ Yes / ❌ No buttons pre-wired for the given pending action."""
        return [
            ActionButton(
                label="✅ Yes",
                action="confirm",
                payload={
                    "confirmation": "confirm",
                    "request_id": pending.request_id,
                    "action_index": pending.action_index,
                },
            ),
            ActionButton(
                label="❌ No",
                action="reject",
                payload={
                    "confirmation": "reject",
                    "request_id": pending.request_id,
                    "action_index": pending.action_index,
                },
            ),
        ]

    # ------------------------------------------------------------------
    # Signal dispatch
    # ------------------------------------------------------------------

    async def handle_signal(self, agent_request: "AgentRequest") -> bool:
        """Return True if the request was fully handled as a Telegram signal."""
        if await self._handle_registration(agent_request):
            return True
        if await self._handle_integration(agent_request):
            return True
        if await self._handle_confirmation(agent_request):
            return True
        return False

    # ------------------------------------------------------------------
    # Individual handlers (private)
    # ------------------------------------------------------------------

    async def _handle_registration(self, agent_request: "AgentRequest") -> bool:
        signal = agent_request.signal or {}

        if signal.get("registration"):
            members = self._member_registry.get_members() if self._member_registry else []
            actions = [
                ActionButton(
                    label=m.display_name,
                    action="register",
                    payload={"user_id": m.id, "action": "register"},
                )
                for m in members
            ]
            first_name = signal.get("first_name", "")
            await self.send(agent_request, f"Hi {first_name}! Who are you?", actions=actions)
            return True

        if signal.get("registered"):
            user_id = signal.get("user_id", "")
            chat_id = self.chat_id_from_request(agent_request)
            if chat_id:
                self._out.register_chat(user_id, chat_id)
            await self.send(agent_request, "You're all set! Say hello to Mithra.")
            await self._notify_admin_of_registration(user_id)
            return True

        return False

    async def _handle_integration(self, agent_request: "AgentRequest") -> bool:
        signal = agent_request.signal or {}
        text_lower = (agent_request.text or "").lower()

        wants_reconnect_google = any(
            p in text_lower
            for p in ["reconnect google", "reauth google", "reconnect calendar"]
        )
        wants_google = wants_reconnect_google or signal.get("connect_integration") == "google" or any(
            p in text_lower
            for p in ["connect google", "link google", "connect calendar", "setup google"]
        )
        wants_microsoft = signal.get("connect_integration") == "microsoft" or any(
            word in text_lower
            for word in ["connect microsoft", "microsoft", "onedrive", "connect onedrive"]
        )

        if not wants_google and not wants_microsoft:
            return False

        uid = agent_request.user_id
        manager = self._oauth_manager
        base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")

        if wants_google:
            if not manager or not manager.is_provider_configured("google"):
                await self.send(agent_request, "Google is not configured.")
                return True
            url = f"{base_url}/auth/google?user_id={uid}"
            if wants_reconnect_google:
                # User explicitly requested reconnect — always provide URL
                await self.send(agent_request, f"Tap to reconnect Google Calendar:\n{url}")
            elif manager.is_connected("google", uid):
                token_data = manager.get_token_data("google", uid)
                if token_data and token_data.get("refresh_token"):
                    await self.send(agent_request, "✅ Google is already connected.")
                else:
                    # Token record exists but refresh_token is missing — reconnect needed
                    await self.send(
                        agent_request,
                        f"⚠️ Google connection needs reconnecting.\nTap to reconnect:\n{url}",
                    )
            else:
                await self.send(agent_request, f"Tap to connect Google:\n{url}")
            return True

        if wants_microsoft:
            if not manager or not manager.is_provider_configured("microsoft"):
                await self.send(
                    agent_request,
                    "Microsoft is not configured on this Mithra instance.",
                )
                return True
            if manager.is_connected("microsoft", uid):
                await self.send(agent_request, "✅ Microsoft is already connected.")
            else:
                url = f"{base_url}/auth/microsoft?user_id={uid}"
                await self.send(agent_request, f"Tap to connect Microsoft:\n{url}")
            return True

        return False

    async def _handle_confirmation(self, agent_request: "AgentRequest") -> bool:
        signal = agent_request.signal or {}
        text_lower = (agent_request.text or "").lower().strip()

        is_confirm = signal.get("confirmation") in ("confirm", "✅ Yes")
        is_reject = signal.get("confirmation") in ("reject", "❌ No")

        if not is_confirm and not is_reject:
            confirm_words = {
                "yes", "y", "confirm", "ok", "okay", "sure",
                "yes please", "go ahead", "do it",
                "add it", "confirmed", "yep", "yup", "yeah",
            }
            reject_words = {
                "no", "n", "cancel", "nope", "reject",
                "stop", "never mind", "don't", "abort",
            }
            is_confirm = text_lower in confirm_words
            is_reject = text_lower in reject_words

        if not is_confirm and not is_reject:
            return False

        request_id = signal.get("request_id")
        action_index = signal.get("action_index", 0)

        # Compact callback_data sends only the first 8 chars of the UUID.
        # Resolve back to the full request_id by matching against pending actions.
        if request_id and len(request_id) == 8 and self._engine:
            pending = self._engine.get_pending(agent_request.user_id)
            full_id = next(
                (p.request_id for p in pending if p.request_id.startswith(request_id)),
                None,
            )
            if full_id:
                request_id = full_id

        if not request_id and self._engine:
            pending = self._engine.get_pending(agent_request.user_id)
            if not pending:
                return False
            request_id = pending[0].request_id
            action_index = pending[0].action_index

        if not request_id:
            return False

        if is_confirm and self._engine and self._executor:
            receipt = await self._engine.confirm(
                request_id, action_index, agent_request.user_id, self._executor
            )
            text = receipt.response_text if receipt and receipt.response_text else "✅ Done."
            await self.send(agent_request, text)
            return True

        if is_reject and self._engine:
            await self._engine.reject(request_id, action_index, agent_request.user_id)
            await self.send(agent_request, "Cancelled.")
            return True

        return False

    async def _notify_admin_of_registration(self, user_id: str) -> None:
        admin_id_str = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")
        if not admin_id_str:
            return
        if self._member_registry:
            member = self._member_registry.get_member(user_id)
            member_name = member.display_name if member else user_id
        else:
            member_name = user_id
        try:
            self._out.register_chat("admin", int(admin_id_str))
            await self._out.send(
                "admin",
                OutboundMessage(
                    text=f"✅ New registration\nName: {member_name}\nUser ID: {user_id}"
                ),
            )
        except Exception as exc:
            _logger.error("Admin registration notification failed: %s", exc)
