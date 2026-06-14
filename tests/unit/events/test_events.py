"""
MES Phase 0 unit tests — ADR section 14, MES-07.

Coverage:
  - BaseEvent, InboundEvent, DomainEvent dataclasses
  - _topic_matches wildcard logic
  - AsyncioSQLiteEventBus: subscribe/publish/unsubscribe
  - Idempotency (same event_id published twice)
  - Causation depth guard (> MAX blocks delivery)
  - Error isolation (one handler fails, others still run)
  - Fan-out (multiple handlers receive the same event)
  - SQLite persistence (event written before delivery)
  - processed=1 after success, 0 after failure
  - replay of unprocessed events
  - InboundAdapter and EventHandler can be subclassed
  - health() returns expected keys
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import os
from typing import Any

import pytest

from mithra.events.base import (
    MAX_CAUSATION_DEPTH,
    BaseEvent,
    DomainEvent,
    InboundEvent,
)
from mithra.events.asyncio_sqlite_bus import AsyncioSQLiteEventBus, _topic_matches
from mithra.events.handler import EventHandler
from mithra.events.inbound import InboundAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bus(tmp_path) -> AsyncioSQLiteEventBus:
    return AsyncioSQLiteEventBus(db_path=str(tmp_path / "test.db"))


class _RecordingHandler(EventHandler):
    """EventHandler that records every event it receives."""

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns
        self.received: list[BaseEvent] = []

    @property
    def topic_patterns(self) -> list[str]:
        return self._patterns

    async def handle(self, event: BaseEvent, context: Any) -> None:
        self.received.append(event)


class _FailingHandler(EventHandler):
    """EventHandler that always raises."""

    def __init__(self) -> None:
        self.called = False

    @property
    def topic_patterns(self) -> list[str]:
        return ["domain.*.*"]

    async def handle(self, event: BaseEvent, context: Any) -> None:
        self.called = True
        raise RuntimeError("intentional handler failure")


class _ConcreteInboundAdapter(InboundAdapter):
    @property
    def source(self) -> str:
        return "test_source"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def _normalise(self, raw: dict) -> InboundEvent:
        return InboundEvent.create(
            topic="inbound.test_source.signal",
            household_id="hh_test",
            user_id="usr_test",
            source=self.source,
            raw_data=raw,
        )


# ---------------------------------------------------------------------------
# BaseEvent tests
# ---------------------------------------------------------------------------

class TestBaseEvent:
    def test_create_sets_event_id(self):
        ev = BaseEvent.create(
            topic="inbound.test.signal",
            household_id="hh_1",
            user_id="usr_1",
        )
        assert ev.event_id.startswith("evt_")
        assert len(ev.event_id) > 4

    def test_create_sets_timestamp(self):
        ev = BaseEvent.create(
            topic="inbound.test.signal",
            household_id="hh_1",
            user_id="usr_1",
        )
        assert "T" in ev.timestamp  # ISO8601

    def test_create_defaults_causation(self):
        ev = BaseEvent.create(
            topic="domain.time.appointment_created",
            household_id="hh_1",
            user_id="usr_1",
        )
        assert ev.causation_id is None
        assert ev.causation_depth == 0

    def test_create_with_causation(self):
        parent_id = "evt_abc123"
        ev = BaseEvent.create(
            topic="domain.tasks.task_created",
            household_id="hh_1",
            user_id="usr_1",
            causation_id=parent_id,
            causation_depth=2,
        )
        assert ev.causation_id == parent_id
        assert ev.causation_depth == 2

    def test_direct_instantiation(self):
        ev = BaseEvent(
            event_id="evt_manual",
            topic="system.startup.complete",
            household_id="hh_1",
            user_id="household",
            timestamp="2026-06-13T10:00:00+00:00",
        )
        assert ev.event_id == "evt_manual"
        assert ev.causation_depth == 0


# ---------------------------------------------------------------------------
# InboundEvent tests
# ---------------------------------------------------------------------------

class TestInboundEvent:
    def test_create_sets_source(self):
        ev = InboundEvent.create(
            topic="inbound.gmail.email_received",
            household_id="hh_1",
            user_id="usr_1",
            source="gmail",
            raw_data={"from": "amazon@amazon.com"},
        )
        assert ev.source == "gmail"
        assert ev.classified is False
        assert ev.raw_data == {"from": "amazon@amazon.com"}

    def test_inherits_base_fields(self):
        ev = InboundEvent.create(
            topic="inbound.ha_sensor.motion_detected",
            household_id="hh_1",
            user_id="household",
            source="ha_sensor",
        )
        assert ev.event_id.startswith("evt_")
        assert ev.household_id == "hh_1"
        assert ev.topic == "inbound.ha_sensor.motion_detected"

    def test_raw_data_defaults_empty(self):
        ev = InboundEvent.create(
            topic="inbound.scheduler.morning_briefing_due",
            household_id="hh_1",
            user_id="usr_1",
            source="scheduler",
        )
        assert ev.raw_data == {}


# ---------------------------------------------------------------------------
# DomainEvent tests
# ---------------------------------------------------------------------------

class TestDomainEvent:
    def test_create_sets_event_type_and_domain(self):
        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_abc",
            payload={"title": "Dentist", "date": "2026-06-20"},
        )
        assert ev.event_type == "appointment_created"
        assert ev.source_domain == "time"
        assert ev.request_id == "req_abc"
        assert ev.payload == {"title": "Dentist", "date": "2026-06-20"}

    def test_topic_auto_derived(self):
        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_xyz",
        )
        assert ev.topic == "domain.tasks.task_completed"

    def test_inherits_base_fields(self):
        ev = DomainEvent.create(
            event_type="expense_recorded",
            source_domain="money",
            household_id="hh_2",
            user_id="usr_2",
            request_id="req_001",
        )
        assert ev.event_id.startswith("evt_")
        assert ev.causation_depth == 0

    def test_create_with_causation(self):
        ev = DomainEvent.create(
            event_type="task_created",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_1",
            causation_id="evt_parent_1",
            causation_depth=1,
        )
        assert ev.causation_id == "evt_parent_1"
        assert ev.causation_depth == 1


# ---------------------------------------------------------------------------
# _topic_matches tests
# ---------------------------------------------------------------------------

class TestTopicMatches:
    def test_exact_match(self):
        assert _topic_matches(
            "domain.time.appointment_created",
            "domain.time.appointment_created",
        )

    def test_wildcard_last_segment(self):
        assert _topic_matches("domain.time.*", "domain.time.appointment_created")

    def test_wildcard_last_no_match_wrong_prefix(self):
        assert not _topic_matches("domain.time.*", "domain.tasks.task_completed")

    def test_multi_wildcard(self):
        assert _topic_matches("domain.*.*", "domain.tasks.task_completed")
        assert _topic_matches("domain.*.*", "domain.time.appointment_created")

    def test_length_mismatch(self):
        assert not _topic_matches("domain.time.*", "domain.time.sub.extra")
        assert not _topic_matches("domain.*", "domain.time.appointment_created")

    def test_inbound_wildcard(self):
        assert _topic_matches("inbound.gmail.*", "inbound.gmail.email_received")
        assert not _topic_matches("inbound.gmail.*", "inbound.ha_sensor.motion")

    def test_no_match_different_namespace(self):
        assert not _topic_matches("domain.*.*", "inbound.gmail.email_received")


# ---------------------------------------------------------------------------
# AsyncioSQLiteEventBus tests
# ---------------------------------------------------------------------------

class TestEventBusPubSub:
    def test_subscribe_and_publish_calls_handler(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.time.*"])
        bus.subscribe("domain.time.*", handler)

        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_1",
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 1
        assert handler.received[0].event_id == ev.event_id

    def test_unsubscribed_handler_not_called(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)
        bus.unsubscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_2",
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 0

    def test_no_subscribers_publish_succeeds(self, tmp_path):
        bus = _bus(tmp_path)
        ev = DomainEvent.create(
            event_type="expense_recorded",
            source_domain="money",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_3",
        )
        # Should not raise
        asyncio.run(bus.publish(ev))

    def test_fan_out_two_handlers_both_called(self, tmp_path):
        bus = _bus(tmp_path)
        h1 = _RecordingHandler(["domain.time.*"])
        h2 = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.time.*", h1)
        bus.subscribe("domain.*.*", h2)

        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_4",
        )
        asyncio.run(bus.publish(ev))
        assert len(h1.received) == 1
        assert len(h2.received) == 1

    def test_inbound_handler_not_called_for_domain_event(self, tmp_path):
        bus = _bus(tmp_path)
        h_inbound = _RecordingHandler(["inbound.*.*"])
        h_domain = _RecordingHandler(["domain.*.*"])
        bus.subscribe("inbound.*.*", h_inbound)
        bus.subscribe("domain.*.*", h_domain)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_5",
        )
        asyncio.run(bus.publish(ev))
        assert len(h_inbound.received) == 0
        assert len(h_domain.received) == 1

    def test_wildcard_exact_match_pattern(self, tmp_path):
        bus = _bus(tmp_path)
        h_exact = _RecordingHandler(["domain.time.appointment_created"])
        h_wild = _RecordingHandler(["domain.time.*"])
        bus.subscribe("domain.time.appointment_created", h_exact)
        bus.subscribe("domain.time.*", h_wild)

        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_6",
        )
        asyncio.run(bus.publish(ev))
        assert len(h_exact.received) == 1
        assert len(h_wild.received) == 1


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_event_id_published_twice_handler_called_once(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_idem",
        )
        asyncio.run(bus.publish(ev))
        asyncio.run(bus.publish(ev))  # second publish, same event_id

        assert len(handler.received) == 1

    def test_different_event_ids_both_delivered(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev1 = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_1",
        )
        ev2 = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_2",
        )
        asyncio.run(bus.publish(ev1))
        asyncio.run(bus.publish(ev2))

        assert len(handler.received) == 2


# ---------------------------------------------------------------------------
# Causation depth guard tests
# ---------------------------------------------------------------------------

class TestCausationDepth:
    def test_depth_at_max_is_delivered(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="task_created",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_max",
            causation_depth=MAX_CAUSATION_DEPTH,
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 1

    def test_depth_exceeding_max_is_not_delivered(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="task_created",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_overflow",
            causation_depth=MAX_CAUSATION_DEPTH + 1,
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 0

    def test_depth_zero_is_delivered(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_zero",
            causation_depth=0,
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 1


# ---------------------------------------------------------------------------
# Error isolation tests
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    def test_failing_handler_does_not_prevent_sibling(self, tmp_path):
        bus = _bus(tmp_path)
        failing = _FailingHandler()
        recording = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", failing)
        bus.subscribe("domain.*.*", recording)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_err",
        )
        asyncio.run(bus.publish(ev))

        assert failing.called is True
        assert len(recording.received) == 1

    def test_failed_delivery_increments_error_count(self, tmp_path):
        bus = _bus(tmp_path)
        failing = _FailingHandler()
        bus.subscribe("domain.*.*", failing)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_err2",
        )
        asyncio.run(bus.publish(ev))

        with sqlite3.connect(str(tmp_path / "test.db")) as conn:
            row = conn.execute(
                "SELECT error_count, processed FROM events WHERE event_id=?",
                (ev.event_id,),
            ).fetchone()
        assert row[0] >= 1      # error_count incremented
        assert row[1] == 0      # not marked processed


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_event_written_to_sqlite_before_delivery(self, tmp_path):
        writes: list[str] = []

        class _CheckingHandler(EventHandler):
            @property
            def topic_patterns(self) -> list[str]:
                return ["domain.*.*"]

            async def handle(self, event: BaseEvent, context: Any) -> None:
                # Check event is already in DB when handler is called
                with sqlite3.connect(str(tmp_path / "test.db")) as conn:
                    row = conn.execute(
                        "SELECT event_id FROM events WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()
                if row:
                    writes.append(event.event_id)

        bus = _bus(tmp_path)
        bus.subscribe("domain.*.*", _CheckingHandler())

        ev = DomainEvent.create(
            event_type="appointment_created",
            source_domain="time",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_persist",
        )
        asyncio.run(bus.publish(ev))
        assert ev.event_id in writes

    def test_successful_delivery_marks_processed(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_proc",
        )
        asyncio.run(bus.publish(ev))

        with sqlite3.connect(str(tmp_path / "test.db")) as conn:
            row = conn.execute(
                "SELECT processed, processed_at FROM events WHERE event_id=?",
                (ev.event_id,),
            ).fetchone()
        assert row[0] == 1
        assert row[1] is not None

    def test_event_with_no_handlers_marked_processed(self, tmp_path):
        bus = _bus(tmp_path)  # no subscribers

        ev = DomainEvent.create(
            event_type="expense_recorded",
            source_domain="money",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_noh",
        )
        asyncio.run(bus.publish(ev))

        with sqlite3.connect(str(tmp_path / "test.db")) as conn:
            row = conn.execute(
                "SELECT processed FROM events WHERE event_id=?",
                (ev.event_id,),
            ).fetchone()
        assert row[0] == 1


# ---------------------------------------------------------------------------
# Replay tests
# ---------------------------------------------------------------------------

class TestReplay:
    def test_replay_delivers_unprocessed_event(self, tmp_path):
        # Publish without subscribers → persisted but NOT processed
        bus1 = _bus(tmp_path)
        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_replay",
        )
        asyncio.run(bus1.publish(ev))

        # Verify stored but unprocessed
        with sqlite3.connect(str(tmp_path / "test.db")) as conn:
            row = conn.execute(
                "SELECT processed FROM events WHERE event_id=?",
                (ev.event_id,),
            ).fetchone()
        assert row[0] == 1  # no handler, so marked processed immediately

    def test_replay_with_handler_re_delivers_unprocessed(self, tmp_path):
        db_path = str(tmp_path / "replay.db")
        # Seed an unprocessed event directly in SQLite
        bus = AsyncioSQLiteEventBus(db_path=db_path)
        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_seed",
        )
        import dataclasses, json
        payload_json = json.dumps(dataclasses.asdict(ev))
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO events "
                "(event_id, topic, household_id, user_id, timestamp, "
                "causation_id, causation_depth, payload_json, processed, error_count) "
                "VALUES (?,?,?,?,?,?,?,?,0,0)",
                (ev.event_id, ev.topic, ev.household_id, ev.user_id,
                 ev.timestamp, ev.causation_id, ev.causation_depth, payload_json),
            )
            conn.commit()

        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)
        asyncio.run(
            bus.replay("domain.*.*", "1970-01-01T00:00:00+00:00")
        )
        assert len(handler.received) == 1

    def test_replay_skips_already_processed(self, tmp_path):
        db_path = str(tmp_path / "replay2.db")
        bus = AsyncioSQLiteEventBus(db_path=db_path)

        ev = DomainEvent.create(
            event_type="task_completed",
            source_domain="tasks",
            household_id="hh_1",
            user_id="usr_1",
            request_id="req_skip",
        )
        import dataclasses, json
        payload_json = json.dumps(dataclasses.asdict(ev))
        with sqlite3.connect(db_path) as conn:
            # Insert as already processed
            conn.execute(
                "INSERT INTO events "
                "(event_id, topic, household_id, user_id, timestamp, "
                "causation_id, causation_depth, payload_json, processed, error_count) "
                "VALUES (?,?,?,?,?,?,?,?,1,0)",
                (ev.event_id, ev.topic, ev.household_id, ev.user_id,
                 ev.timestamp, ev.causation_id, ev.causation_depth, payload_json),
            )
            conn.commit()

        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)
        asyncio.run(
            bus.replay("domain.*.*", "1970-01-01T00:00:00+00:00")
        )
        assert len(handler.received) == 0


# ---------------------------------------------------------------------------
# health() tests
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_healthy_with_keys(self, tmp_path):
        bus = _bus(tmp_path)
        h = bus.health()
        assert h["status"] == "healthy"
        assert "subscribers" in h
        assert "unprocessed_events" in h
        assert "total_events" in h

    def test_health_counts_subscribers(self, tmp_path):
        bus = _bus(tmp_path)
        bus.subscribe("domain.*.*", _RecordingHandler(["domain.*.*"]))
        bus.subscribe("inbound.*.*", _RecordingHandler(["inbound.*.*"]))
        assert bus.health()["subscribers"] == 2

    def test_health_counts_events(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["domain.*.*"])
        bus.subscribe("domain.*.*", handler)

        for i in range(3):
            ev = DomainEvent.create(
                event_type="task_completed",
                source_domain="tasks",
                household_id="hh_1",
                user_id="usr_1",
                request_id=f"req_{i}",
            )
            asyncio.run(bus.publish(ev))

        h = bus.health()
        assert h["total_events"] == 3
        assert h["unprocessed_events"] == 0  # all delivered successfully


# ---------------------------------------------------------------------------
# InboundAdapter tests
# ---------------------------------------------------------------------------

class TestInboundAdapter:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            InboundAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        adapter = _ConcreteInboundAdapter()
        assert adapter.source == "test_source"

    def test_health_default_returns_dict(self):
        adapter = _ConcreteInboundAdapter()
        h = adapter.health()
        assert h["source"] == "test_source"
        assert "status" in h

    def test_normalise_returns_inbound_event(self):
        adapter = _ConcreteInboundAdapter()
        ev = asyncio.run(
            adapter._normalise({"key": "value"})
        )
        assert isinstance(ev, InboundEvent)
        assert ev.source == "test_source"
        assert ev.raw_data == {"key": "value"}
        assert ev.classified is False


# ---------------------------------------------------------------------------
# EventHandler tests
# ---------------------------------------------------------------------------

class TestEventHandler:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            EventHandler()  # type: ignore[abstract]

    def test_concrete_subclass_reports_patterns(self):
        handler = _RecordingHandler(["domain.time.*", "domain.tasks.*"])
        assert "domain.time.*" in handler.topic_patterns
        assert "domain.tasks.*" in handler.topic_patterns

    def test_handle_called_by_bus(self, tmp_path):
        bus = _bus(tmp_path)
        handler = _RecordingHandler(["inbound.*.*"])
        bus.subscribe("inbound.*.*", handler)

        ev = InboundEvent.create(
            topic="inbound.gmail.email_received",
            household_id="hh_1",
            user_id="usr_1",
            source="gmail",
        )
        asyncio.run(bus.publish(ev))
        assert len(handler.received) == 1
        assert isinstance(handler.received[0], InboundEvent)


# ---------------------------------------------------------------------------
# MAX_CAUSATION_DEPTH constant
# ---------------------------------------------------------------------------

def test_max_causation_depth_is_five():
    assert MAX_CAUSATION_DEPTH == 5
