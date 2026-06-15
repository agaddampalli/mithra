"""
TelegramNotificationHandler — MES-08.

Subscribes to notification.telegram.send events and delivers them
via the Telegram Bot API using async HTTP (httpx).

Uses asyncio-native httpx — NOT urllib.request (blocking I/O in an async
handler blocks the event loop; see LESSONS_LEARNED L-01).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from mithra.events.handler import EventHandler

if TYPE_CHECKING:
    from mithra.events.base import BaseEvent
    from mithra.domains.context import DomainContext

_logger = logging.getLogger(__name__)


class TelegramNotificationHandler(EventHandler):
    """Delivers notification.telegram.send events via the Telegram Bot API."""

    handler_id = "notification.telegram"

    def __init__(self, bot_token: str, db_path: str) -> None:
        self._bot_token = bot_token
        self._db_path = db_path

    @property
    def topic_patterns(self) -> list[str]:
        return ["notification.telegram.send"]

    async def handle(
        self,
        event: "BaseEvent",
        context: "DomainContext | None",
    ) -> None:
        import httpx

        payload: dict = getattr(event, "payload", {})
        text: str = payload.get("text", "")
        chat_id: str = payload.get("chat_id", "")

        if not chat_id:
            user_id = payload.get("user_id", "") or event.user_id
            if user_id and self._db_path:
                try:
                    with sqlite3.connect(self._db_path) as conn:
                        row = conn.execute(
                            "SELECT channel_id FROM member_channels "
                            "WHERE user_id=? AND channel='telegram'",
                            (user_id,),
                        ).fetchone()
                        if row:
                            chat_id = row[0]
                except Exception as exc:
                    _logger.warning(
                        "TelegramNotificationHandler: chat_id lookup failed: %s", exc
                    )

        if not chat_id or not text:
            _logger.debug(
                "TelegramNotificationHandler: skipping event %s — no chat_id or text",
                event.event_id,
            )
            return

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                )
                resp.raise_for_status()
            _logger.info(
                "TelegramNotificationHandler: sent to chat_id=%s event=%s",
                chat_id,
                event.event_id,
            )
        except Exception as exc:
            _logger.error(
                "TelegramNotificationHandler: send failed chat_id=%s event=%s: %s",
                chat_id,
                event.event_id,
                exc,
            )
            raise
