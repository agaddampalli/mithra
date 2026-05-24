"""
/api/v1/webhook/* routes — inbound webhook endpoints.

POST /api/v1/webhook/telegram  — Telegram Bot API updates
POST /api/v1/webhook/nango     — Nango OAuth completion events

All routes are unauthenticated — each has its own validation mechanism
(Telegram uses X-Telegram-Bot-Api-Secret-Token; Nango events are
identified by their payload shape and the connection_id they carry).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

router = APIRouter()

_logger = logging.getLogger(__name__)


@router.post("/webhook/nango")
async def nango_webhook(request: Request):
    """Receive a Nango auth-completion webhook and persist the connection_id.

    Nango fires this after a user completes OAuth. The payload carries the
    Nango UUID connectionId and the end_user_id tag set during create_connect_session().
    We store the UUID so get_token() can look it up on every Calendar API call.
    """
    from mithra.api.gateway import _state

    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}

    event_type = payload.get("type", "")
    connection_id = payload.get("connectionId") or payload.get("connection_id")
    tags = payload.get("tags") or {}
    end_user_id = tags.get("end_user_id")

    _logger.info(
        "Nango webhook: type=%s connection_id=%s end_user_id=%s",
        event_type, connection_id, end_user_id,
    )

    if event_type not in ("auth", "connection.created", "authorization"):
        return {"ok": True}

    if not connection_id or not end_user_id:
        _logger.warning("Nango webhook missing fields — payload: %s", payload)
        return {"ok": True}

    local_adapter = _state.get("local_adapter")
    household_id = _state.get("household_id", "default")
    nango = _state.get("nango")

    if local_adapter and nango:
        nango.save_connection_id(
            user_id=end_user_id,
            household_id=household_id,
            nango_connection_id=connection_id,
            storage_adapter=local_adapter,
        )

        identity = _state.get("identity_resolver")
        telegram_output = _state.get("telegram_output")
        if telegram_output and identity:
            chat_id = identity.get_chat_id_for_user(end_user_id)
            if chat_id:
                from mithra.adapters.messaging import OutboundMessage
                telegram_output.register_chat(end_user_id, chat_id)
                try:
                    await telegram_output.send(
                        end_user_id,
                        OutboundMessage(text="✅ Google Calendar connected! You can now add events."),
                    )
                except Exception as exc:
                    _logger.warning("Nango webhook: Telegram notify failed: %s", exc)

    return {"ok": True}
