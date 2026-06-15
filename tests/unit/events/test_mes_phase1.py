"""
MES Phase 1 unit tests — MES-08, MES-09, MES-10.

Coverage:
  MES-08  TelegramNotificationHandler
            - success path (httpx mocked)
            - no chat_id → skipped (no HTTP call)
            - missing text → skipped
            - httpx error → raises (bus catches it, increments error_count)
  MES-09  _publish_notification_event
            - bus + loop available → run_coroutine_threadsafe called
            - bus None → silent no-op
            - loop None → silent no-op
  MES-10  replay skips notification.* events
            - notification events NOT re-delivered on bus.start()
            - domain events ARE re-delivered
            - error_count >= 3 events skipped
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mithra.events.asyncio_sqlite_bus import AsyncioSQLiteEventBus
from mithra.events.base import DomainEvent, NotificationEvent
from mithra.events.handlers.notification_handler import TelegramNotificationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bus(tmp_path) -> AsyncioSQLiteEventBus:
    return AsyncioSQLiteEventBus(db_path=str(tmp_path / "mes1.db"))


def _notif_event(
    household_id: str = "hh_test",
    user_id: str = "usr_1",
    chat_id: str = "99001",
    text: str = "hello",
) -> NotificationEvent:
    return NotificationEvent.create(
        topic="notification.telegram.send",
        household_id=household_id,
        user_id=user_id,
        payload={"chat_id": chat_id, "text": text},
    )


# ---------------------------------------------------------------------------
# MES-08 — TelegramNotificationHandler
# ---------------------------------------------------------------------------

class TestTelegramNotificationHandler:

    def test_topic_patterns(self):
        h = TelegramNotificationHandler(bot_token="tok", db_path="")
        assert h.topic_patterns == ["notification.telegram.send"]

    def test_handler_id(self):
        h = TelegramNotificationHandler(bot_token="tok", db_path="")
        assert h.handler_id == "notification.telegram"

    def test_success_sends_message(self):
        h = TelegramNotificationHandler(bot_token="BOT123", db_path="")
        event = _notif_event(chat_id="12345", text="Good morning!")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            asyncio.run(h.handle(event, None))

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert "12345" in str(call_kwargs)
        assert "Good morning!" in str(call_kwargs)

    def test_no_chat_id_skips_send(self):
        h = TelegramNotificationHandler(bot_token="BOT123", db_path="")
        event = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_1",
            payload={"text": "hello"},  # no chat_id
        )

        with patch("httpx.AsyncClient") as mock_cls:
            asyncio.run(h.handle(event, None))
            mock_cls.assert_not_called()

    def test_no_text_skips_send(self):
        h = TelegramNotificationHandler(bot_token="BOT123", db_path="")
        event = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_1",
            payload={"chat_id": "12345"},  # no text
        )

        with patch("httpx.AsyncClient") as mock_cls:
            asyncio.run(h.handle(event, None))
            mock_cls.assert_not_called()

    def test_chat_id_lookup_from_db(self, tmp_path):
        db_path = str(tmp_path / "lookup.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE member_channels "
                "(user_id TEXT, channel TEXT, channel_id TEXT)"
            )
            conn.execute(
                "INSERT INTO member_channels VALUES (?, ?, ?)",
                ("usr_abc", "telegram", "777888"),
            )
            conn.commit()

        h = TelegramNotificationHandler(bot_token="BOT", db_path=db_path)
        event = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_abc",
            payload={"text": "hi"},  # no chat_id — should be looked up
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("httpx.AsyncClient", return_value=mock_client):
            asyncio.run(h.handle(event, None))

        call_kwargs = mock_client.post.call_args
        assert "777888" in str(call_kwargs)

    def test_httpx_error_raises(self):
        h = TelegramNotificationHandler(bot_token="BOT123", db_path="")
        event = _notif_event(chat_id="12345", text="hello")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("network error"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(Exception, match="network error"):
                asyncio.run(h.handle(event, None))


# ---------------------------------------------------------------------------
# MES-09 — NotificationEvent creation and _publish_notification_event
# ---------------------------------------------------------------------------

class TestNotificationEvent:

    def test_create_sets_fields(self):
        evt = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_1",
            payload={"chat_id": "123", "text": "hi"},
        )
        assert evt.topic == "notification.telegram.send"
        assert evt.payload["chat_id"] == "123"
        assert evt.payload["text"] == "hi"
        assert evt.event_id.startswith("evt_")

    def test_create_default_payload(self):
        evt = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_1",
        )
        assert evt.payload == {}

    def test_publish_notification_no_bus_is_silent(self):
        from mithra.scheduler.task_scheduler import _publish_notification_event
        with patch("mithra.events.bus.get_event_bus", return_value=None):
            _publish_notification_event("usr_1", "123", "hello", "hh_1")  # no error

    def test_publish_notification_no_loop_is_silent(self):
        from mithra.scheduler.task_scheduler import _publish_notification_event
        mock_bus = MagicMock()
        with patch("mithra.events.bus.get_event_bus", return_value=mock_bus), \
             patch("mithra.events.bus.get_main_loop", return_value=None):
            _publish_notification_event("usr_1", "123", "hello", "hh_1")
        mock_bus.publish.assert_not_called()

    def test_publish_notification_fires_threadsafe(self):
        from mithra.scheduler.task_scheduler import _publish_notification_event
        mock_bus = MagicMock()
        mock_loop = MagicMock()
        with patch("mithra.events.bus.get_event_bus", return_value=mock_bus), \
             patch("mithra.events.bus.get_main_loop", return_value=mock_loop), \
             patch("asyncio.run_coroutine_threadsafe") as mock_rct:
            _publish_notification_event("usr_1", "123", "hello", "hh_1")
        mock_rct.assert_called_once()
        _, loop_arg = mock_rct.call_args[0]
        assert loop_arg is mock_loop


# ---------------------------------------------------------------------------
# MES-10 — replay skips notification.* events
# ---------------------------------------------------------------------------

class TestReplaySkipsNotifications:

    def _seed_events(self, db_path: str) -> tuple[str, str]:
        """Insert one unprocessed domain event and one unprocessed notification event."""
        domain_evt = DomainEvent.create(
            event_type="task_created",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_1",
        )
        notif_evt = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id="hh_1",
            user_id="usr_1",
            payload={"chat_id": "123", "text": "hi"},
        )
        import dataclasses, json
        with sqlite3.connect(db_path) as conn:
            for evt in [domain_evt, notif_evt]:
                conn.execute(
                    "INSERT OR IGNORE INTO events "
                    "(event_id, topic, household_id, user_id, timestamp, "
                    "causation_id, causation_depth, payload_json, processed, error_count) "
                    "VALUES (?,?,?,?,?,?,?,?,0,0)",
                    (
                        evt.event_id, evt.topic, evt.household_id, evt.user_id,
                        evt.timestamp, evt.causation_id, evt.causation_depth,
                        json.dumps(dataclasses.asdict(evt)),
                    ),
                )
            conn.commit()
        return domain_evt.event_id, notif_evt.event_id

    def test_replay_delivers_domain_not_notification(self, tmp_path):
        bus = _bus(tmp_path)
        domain_id, notif_id = self._seed_events(str(tmp_path / "mes1.db"))

        received_ids: list[str] = []

        from mithra.events.handler import EventHandler
        from mithra.events.base import BaseEvent

        class _RecordingHandler(EventHandler):
            @property
            def topic_patterns(self):
                return ["*.*.*"]
            async def handle(self, event: BaseEvent, context) -> None:
                received_ids.append(event.event_id)

        bus.subscribe("*.*.*", _RecordingHandler())
        asyncio.run(bus.replay("*.*.*", "1970-01-01T00:00:00+00:00"))

        assert domain_id in received_ids
        assert notif_id not in received_ids

    def test_replay_skips_high_error_count(self, tmp_path):
        bus = _bus(tmp_path)
        db_path = str(tmp_path / "mes1.db")

        domain_evt = DomainEvent.create(
            event_type="task_created",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_2",
        )
        import dataclasses, json
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_id, topic, household_id, user_id, timestamp, "
                "causation_id, causation_depth, payload_json, processed, error_count) "
                "VALUES (?,?,?,?,?,?,?,?,0,3)",  # error_count=3, should be skipped
                (
                    domain_evt.event_id, domain_evt.topic, domain_evt.household_id,
                    domain_evt.user_id, domain_evt.timestamp, domain_evt.causation_id,
                    domain_evt.causation_depth,
                    json.dumps(dataclasses.asdict(domain_evt)),
                ),
            )
            conn.commit()

        received_ids: list[str] = []

        from mithra.events.handler import EventHandler
        from mithra.events.base import BaseEvent

        class _RecordingHandler(EventHandler):
            @property
            def topic_patterns(self):
                return ["*.*.*"]
            async def handle(self, event: BaseEvent, context) -> None:
                received_ids.append(event.event_id)

        bus.subscribe("*.*.*", _RecordingHandler())
        asyncio.run(bus.replay("*.*.*", "1970-01-01T00:00:00+00:00"))

        assert domain_evt.event_id not in received_ids
