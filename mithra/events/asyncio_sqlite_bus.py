"""
AsyncioSQLiteEventBus — in-process event bus with SQLite durability.

ADR section 14.2 (two-way door): this is Phase 0.
RedisEventBus (Phase 2) and KafkaEventBus (Phase 3) are future swaps.
All publishers and handlers remain unchanged on migration.

Delivery contract (ADR 14.7 — error isolation):
  1. Persist event to SQLite before delivery. Raises if SQLite fails.
  2. For each matching handler: isolated try/except.
     - Handler failure logs ERROR but never stops sibling handlers.
  3. processed=1 only when ALL handlers succeed.
  4. Failed events accumulate error_count. Dead-lettered at >= 3.

Idempotency (ADR 14.6):
  - INSERT OR IGNORE on publish. Same event_id twice: second is a no-op.
  - Handlers are responsible for exactly-once semantics in their own writes.

Causation guard (ADR 14.8):
  - publish() rejects events with causation_depth > MAX_CAUSATION_DEPTH.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from mithra.events.base import (
    MAX_CAUSATION_DEPTH,
    BaseEvent,
    DomainEvent,
    InboundEvent,
)
from mithra.events.bus import EventBus

if TYPE_CHECKING:
    from mithra.events.handler import EventHandler

_logger = logging.getLogger(__name__)

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    topic           TEXT NOT NULL,
    household_id    TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    causation_id    TEXT,
    causation_depth INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL,
    processed       INTEGER NOT NULL DEFAULT 0,
    processed_at    TEXT,
    error_count     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic, processed);
CREATE INDEX IF NOT EXISTS idx_events_household ON events(household_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_causation ON events(causation_id);
"""


def _topic_matches(pattern: str, topic: str) -> bool:
    """Return True when topic matches pattern.

    '*' matches exactly one dot-separated segment.
    Examples:
      'domain.time.*'  matches 'domain.time.appointment_created'
      'domain.*.*'     matches 'domain.tasks.task_completed'
      'inbound.gmail.*' matches 'inbound.gmail.email_received'
      'domain.time.x'  does NOT match 'domain.tasks.x'
    """
    pp = pattern.split(".")
    tp = topic.split(".")
    if len(pp) != len(tp):
        return False
    return all(p == "*" or p == t for p, t in zip(pp, tp))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AsyncioSQLiteEventBus(EventBus):
    """Phase 0 EventBus: asyncio delivery + SQLite persistence.

    All SQLite writes run via asyncio.to_thread to avoid blocking the event loop.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # List of (pattern, handler) in registration order
        self._subscribers: list[tuple[str, "EventHandler"]] = []
        self._ensure_table()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def subscribe(self, topic_pattern: str, handler: "EventHandler") -> None:
        self._subscribers.append((topic_pattern, handler))
        _logger.debug(
            "EventBus.subscribe: pattern=%s handler=%s",
            topic_pattern,
            type(handler).__name__,
        )

    def unsubscribe(self, topic_pattern: str, handler: "EventHandler") -> None:
        before = len(self._subscribers)
        self._subscribers = [
            (p, h)
            for p, h in self._subscribers
            if not (p == topic_pattern and h is handler)
        ]
        _logger.debug(
            "EventBus.unsubscribe: pattern=%s removed=%d",
            topic_pattern,
            before - len(self._subscribers),
        )

    async def publish(self, event: BaseEvent) -> None:
        """Persist then deliver the event to all matching handlers."""
        if event.causation_depth > MAX_CAUSATION_DEPTH:
            _logger.error(
                "EventBus: causation_depth %d exceeds max %d for event %s — dead-lettered",
                event.causation_depth,
                MAX_CAUSATION_DEPTH,
                event.event_id,
            )
            await asyncio.to_thread(
                self._write_dead_letter, event, "causation_depth_exceeded"
            )
            return

        payload_json = json.dumps(dataclasses.asdict(event))

        # Persist first — raises if SQLite fails
        already_processed = await asyncio.to_thread(
            self._persist_and_check, event, payload_json
        )
        if already_processed:
            _logger.debug(
                "EventBus: event %s already processed — skipping re-delivery",
                event.event_id,
            )
            return

        handlers = self._find_handlers(event.topic)
        _logger.debug(
            "EventBus: publishing %s to %d handler(s)", event.topic, len(handlers)
        )

        all_ok = True
        total_errors = 0
        last_error: str | None = None

        for handler in handlers:
            try:
                await handler.handle(event, None)
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "EventBus: handler %s failed for event %s",
                    type(handler).__name__,
                    event.event_id,
                    exc_info=True,
                )
                all_ok = False
                total_errors += 1
                last_error = str(exc)

        if all_ok:
            await asyncio.to_thread(self._mark_processed, event.event_id)
        else:
            await asyncio.to_thread(
                self._update_error, event.event_id, total_errors, last_error or ""
            )
            # Check accumulated error_count for dead-letter threshold
            current_errors = await asyncio.to_thread(
                self._get_error_count, event.event_id
            )
            if current_errors >= 3:
                _logger.error(
                    "EventBus: event %s error_count=%d >= 3 — dead-lettered",
                    event.event_id,
                    current_errors,
                )
                await asyncio.to_thread(
                    self._write_dead_letter, event, f"handler_errors: {last_error}"
                )

    async def replay(self, topic_pattern: str, since: str) -> None:
        """Re-deliver unprocessed events matching topic_pattern since timestamp."""
        rows = await asyncio.to_thread(self._load_unprocessed, topic_pattern, since)
        if not rows:
            return
        _logger.info(
            "EventBus.replay: %d unprocessed events matching '%s'",
            len(rows),
            topic_pattern,
        )
        for row in rows:
            event = _reconstruct_event(row["payload_json"])
            if event is None:
                continue
            await self.publish(event)

    async def start(self) -> None:
        """Replay all unprocessed events from the beginning of time."""
        _logger.info("EventBus: starting — replaying unprocessed events")
        await self.replay("*.*.*", "1970-01-01T00:00:00+00:00")

    async def stop(self) -> None:
        _logger.info("EventBus: stopped")

    def health(self) -> dict:
        try:
            with sqlite3.connect(self._db_path) as conn:
                unprocessed = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE processed=0"
                ).fetchone()[0]
                total = conn.execute(
                    "SELECT COUNT(*) FROM events"
                ).fetchone()[0]
            return {
                "status": "healthy",
                "subscribers": len(self._subscribers),
                "unprocessed_events": unprocessed,
                "total_events": total,
            }
        except Exception as exc:
            _logger.error("EventBus.health() failed", exc_info=True)
            return {"status": "unhealthy", "error": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers — run inside asyncio.to_thread where needed
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_EVENTS_DDL)
            conn.commit()

    def _persist_and_check(self, event: BaseEvent, payload_json: str) -> bool:
        """INSERT OR IGNORE the event. Returns True if it was already processed."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events "
                "(event_id, topic, household_id, user_id, timestamp, "
                "causation_id, causation_depth, payload_json, processed, error_count) "
                "VALUES (?,?,?,?,?,?,?,?,0,0)",
                (
                    event.event_id,
                    event.topic,
                    event.household_id,
                    event.user_id,
                    event.timestamp,
                    event.causation_id,
                    event.causation_depth,
                    payload_json,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT processed FROM events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
        return bool(row and row[0] == 1)

    def _mark_processed(self, event_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE events SET processed=1, processed_at=? WHERE event_id=?",
                (_now_iso(), event_id),
            )
            conn.commit()

    def _update_error(
        self, event_id: str, error_count_delta: int, last_error: str
    ) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE events "
                "SET error_count=error_count+?, last_error=? "
                "WHERE event_id=?",
                (error_count_delta, last_error, event_id),
            )
            conn.commit()

    def _get_error_count(self, event_id: str) -> int:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT error_count FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _write_dead_letter(self, event: BaseEvent, reason: str) -> None:
        """Log dead-lettered event. StorageAdapter integration added in Phase 1."""
        _logger.error(
            "EventBus DEAD LETTER: event_id=%s topic=%s reason=%s",
            event.event_id,
            event.topic,
            reason,
        )

    def _load_unprocessed(
        self, topic_pattern: str, since: str
    ) -> list[sqlite3.Row]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM events "
                "WHERE processed=0 AND timestamp >= ? "
                "ORDER BY timestamp ASC",
                (since,),
            ).fetchall()
        # Filter by topic pattern in Python (SQL LIKE is too limited for * wildcards)
        return [r for r in rows if _topic_matches(topic_pattern, r["topic"])]

    def _find_handlers(self, topic: str) -> list["EventHandler"]:
        return [h for pattern, h in self._subscribers if _topic_matches(pattern, topic)]


# ---------------------------------------------------------------------------
# Event reconstruction for replay
# ---------------------------------------------------------------------------

def _reconstruct_event(payload_json: str) -> BaseEvent | None:
    """Reconstruct a typed BaseEvent from its persisted JSON payload."""
    try:
        data = json.loads(payload_json)
        topic: str = data.get("topic", "")
        if topic.startswith("inbound."):
            return InboundEvent(**data)
        if topic.startswith("domain."):
            return DomainEvent(**data)
        return BaseEvent(**data)
    except Exception as exc:
        _logger.error("EventBus: failed to reconstruct event from JSON: %s", exc)
        return None
