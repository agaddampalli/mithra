"""Unit tests for DefaultAutonomyEngine.process() and related methods."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from mithra.agent.interface import AgentAction, AgentIntent, AgentResult, InputChannel
from mithra.autonomy.action_log import ActionLog
from mithra.autonomy.default_engine import DefaultAutonomyEngine
from mithra.autonomy.engine import AutonomyLevel
from mithra.autonomy.executor import ActionExecutorRegistry, ExecutionResult
from mithra.storage.local import LocalStorageAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    return LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "test.db"))


@pytest.fixture
def action_log(storage):
    return ActionLog(storage)


@pytest.fixture
def config_path(tmp_path) -> str:
    """Minimal autonomy.yaml with explicit per-domain levels for all test cases."""
    cfg = {
        "default_level": 2,  # ACT_TELL — distinguishable from SURFACE fallback
        "never_actions": ["blocked_action"],
        "domain_overrides": {
            "observe_domain": 0,    # OBSERVE
            "surface_domain": 1,    # SURFACE
            "act_tell_domain": 2,   # ACT_TELL
            "act_silent_domain": 3, # ACT_SILENT
        },
    }
    path = tmp_path / "autonomy.yaml"
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return str(path)


@pytest.fixture
def engine(storage, action_log, config_path):
    return DefaultAutonomyEngine(storage, action_log, config_path)


def _mock_executor(success: bool = True) -> MagicMock:
    """Return an executor mock where execute() returns a successful ExecutionResult."""
    executor = MagicMock(spec=ActionExecutorRegistry)
    executor.execute = AsyncMock(return_value=ExecutionResult(
        action_id=str(uuid.uuid4()),
        action_type="test_action",
        success=success,
        undo_payload={"key": "val"},
    ))
    executor.notify = AsyncMock()
    executor.undo = AsyncMock(return_value=ExecutionResult(
        action_id=str(uuid.uuid4()),
        action_type="test_action",
        success=True,
    ))
    return executor


def _result(actions: list[AgentAction], request_id: str = "req-1") -> AgentResult:
    return AgentResult(
        request_id=request_id,
        success=True,
        intent=AgentIntent.SYSTEM_UNKNOWN,
        domain="system",
        confidence=1.0,
        actions=actions,
        response_text="",
    )


def _action(
    action_type: str = "do_thing",
    domain: str = "act_tell_domain",
    output_channel: str = "telegram",
    reversibility: str = "full",
) -> AgentAction:
    return AgentAction(
        action_type=action_type,
        domain=domain,
        payload={"data": 1},
        output_channel=output_channel,
        reversibility=reversibility,
    )


USER = "person_a"


# ---------------------------------------------------------------------------
# NEVER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_action_is_rejected_not_executed(engine):
    executor = _mock_executor()
    action = _action(action_type="blocked_action", domain="act_tell_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.rejected) == 1
    assert len(receipt.executed) == 0
    executor.execute.assert_not_called()
    assert "NEVER rule" in receipt.rejected[0].reason


@pytest.mark.asyncio
async def test_never_action_not_in_pending_or_skipped(engine):
    executor = _mock_executor()
    action = _action(action_type="blocked_action", domain="act_tell_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.pending) == 0
    assert len(receipt.skipped) == 0


@pytest.mark.asyncio
async def test_never_violation_threshold_triggers_security_alert(engine):
    """More than 3 NEVER violations within 60 minutes fires a security alert."""
    executor = _mock_executor()
    action = _action(action_type="blocked_action", domain="act_tell_domain")

    for _ in range(4):
        await engine.process(_result([action]), USER, executor)

    # notify() must have been called at least once (fires on every call above threshold)
    executor.notify.assert_called()
    call_args = executor.notify.call_args
    assert call_args[0][0] == "household"
    assert call_args[0][1] == "telegram"
    assert "Security alert" in call_args[0][2]


@pytest.mark.asyncio
async def test_never_violation_below_threshold_no_alert(engine):
    """Exactly 3 violations does not trigger an alert (threshold is > 3)."""
    executor = _mock_executor()
    action = _action(action_type="blocked_action", domain="act_tell_domain")

    for _ in range(3):
        await engine.process(_result([action]), USER, executor)

    executor.notify.assert_not_called()


# ---------------------------------------------------------------------------
# OBSERVE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_action_in_skipped_not_executed(engine):
    executor = _mock_executor()
    action = _action(domain="observe_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.skipped) == 1
    assert len(receipt.executed) == 0
    executor.execute.assert_not_called()
    assert receipt.skipped[0]["domain"] == "observe_domain"


@pytest.mark.asyncio
async def test_observe_action_not_in_pending_or_rejected(engine):
    executor = _mock_executor()
    action = _action(domain="observe_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.pending) == 0
    assert len(receipt.rejected) == 0


# ---------------------------------------------------------------------------
# SURFACE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_action_in_pending_not_executed(engine):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.pending) == 1
    assert len(receipt.executed) == 0
    executor.execute.assert_not_called()
    assert receipt.pending[0].domain == "surface_domain"


@pytest.mark.asyncio
async def test_surface_action_persisted_in_storage(engine, storage):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    stored = storage.list("autonomy", "state", USER)
    pending_records = [r for r in stored if r.key.startswith("pending:")]
    assert len(pending_records) == 1
    assert pending_records[0].data["domain"] == "surface_domain"


@pytest.mark.asyncio
async def test_surface_pending_has_24h_expiry(engine):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    expiry = datetime.fromisoformat(receipt.pending[0].expires_at)
    now = datetime.now(timezone.utc)
    delta_hours = (expiry - now).total_seconds() / 3600
    assert 23.9 < delta_hours <= 24.1


# ---------------------------------------------------------------------------
# ACT_TELL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_tell_action_executed_and_notification_sent(engine):
    executor = _mock_executor()
    action = _action(domain="act_tell_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.executed) == 1
    assert receipt.executed[0].success is True
    executor.execute.assert_called_once()
    executor.notify.assert_called_once()
    assert receipt.notification_sent is True


@pytest.mark.asyncio
async def test_act_tell_failed_execution_no_notification(engine):
    executor = _mock_executor(success=False)
    action = _action(domain="act_tell_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.executed) == 1
    assert receipt.executed[0].success is False
    executor.notify.assert_not_called()
    assert receipt.notification_sent is False


# ---------------------------------------------------------------------------
# ACT_SILENT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_silent_action_executed_no_notification(engine):
    executor = _mock_executor()
    action = _action(domain="act_silent_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    assert len(receipt.executed) == 1
    assert receipt.executed[0].success is True
    executor.notify.assert_not_called()
    assert receipt.notification_sent is False


# ---------------------------------------------------------------------------
# Unknown intent → SURFACE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_intent_defaults_to_surface(engine):
    """An intent with no matching config entry must default to SURFACE, not ACT_SILENT."""
    executor = _mock_executor()
    action = _action(action_type="mystery_action", domain="unknown_domain")
    receipt = await engine.process(_result([action]), USER, executor)

    # SURFACE: queued as pending, never executed
    assert len(receipt.pending) == 1
    assert len(receipt.executed) == 0
    executor.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Mixed actions in one request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_actions_each_correctly_routed(engine):
    executor = _mock_executor()
    actions = [
        _action(action_type="blocked_action", domain="act_tell_domain"),  # NEVER
        _action(domain="observe_domain"),                                   # OBSERVE
        _action(domain="surface_domain"),                                   # SURFACE
        _action(domain="act_tell_domain"),                                  # ACT_TELL
        _action(domain="act_silent_domain"),                                # ACT_SILENT
    ]
    receipt = await engine.process(_result(actions), USER, executor)

    assert len(receipt.rejected) == 1
    assert len(receipt.skipped) == 1
    assert len(receipt.pending) == 1
    assert len(receipt.executed) == 2    # ACT_TELL + ACT_SILENT


@pytest.mark.asyncio
async def test_mixed_only_act_tell_sends_notification(engine):
    executor = _mock_executor()
    actions = [
        _action(domain="act_tell_domain"),   # ACT_TELL → notify
        _action(domain="act_silent_domain"), # ACT_SILENT → no notify
    ]
    receipt = await engine.process(_result(actions), USER, executor)

    executor.notify.assert_called_once()   # only one notification, for ACT_TELL
    assert receipt.notification_sent is True


# ---------------------------------------------------------------------------
# Multiple ACT_TELL → single batched notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_act_tell_single_batched_notification(engine):
    executor = _mock_executor()
    actions = [
        _action(action_type="action_a", domain="act_tell_domain"),
        _action(action_type="action_b", domain="act_tell_domain"),
        _action(action_type="action_c", domain="act_tell_domain"),
    ]
    receipt = await engine.process(_result(actions), USER, executor)

    # Only ONE notify call, not three
    executor.notify.assert_called_once()
    message = executor.notify.call_args[0][2]
    assert "Completed 3 actions" in message
    assert receipt.notification_sent is True


# ---------------------------------------------------------------------------
# response_text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_text_summarises_all_outcomes(engine):
    executor = _mock_executor()
    actions = [
        _action(action_type="blocked_action", domain="act_tell_domain"),
        _action(domain="surface_domain"),
        _action(domain="act_tell_domain"),
    ]
    receipt = await engine.process(_result(actions), USER, executor)

    # Executed actions take priority in the single-priority response format
    assert receipt.response_text != ""
    assert "Done" in receipt.response_text


# ---------------------------------------------------------------------------
# NEVER — written to storage history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_action_written_to_storage_history(engine, action_log):
    executor = _mock_executor()
    action = _action(action_type="blocked_action", domain="act_tell_domain")
    await engine.process(_result([action]), USER, executor)

    entries = action_log.list_recent(USER)
    never_entries = [e for e in entries if e.status == "rejected_never"]
    assert len(never_entries) == 1
    assert never_entries[0].autonomy_level == -1


# ---------------------------------------------------------------------------
# OBSERVE — written to storage history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_action_written_to_storage_history(engine, action_log):
    executor = _mock_executor()
    action = _action(domain="observe_domain")
    await engine.process(_result([action]), USER, executor)

    entries = action_log.list_recent(USER)
    observed = [e for e in entries if e.status == "observed"]
    assert len(observed) == 1
    assert observed[0].domain == "observe_domain"


# ---------------------------------------------------------------------------
# confirm()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_executes_pending_action(engine, storage):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action], request_id="req-confirm"), USER, executor)

    pending = receipt.pending[0]
    conf_receipt = await engine.confirm(pending.request_id, pending.action_index, USER, executor)

    assert len(conf_receipt.executed) == 1
    assert conf_receipt.executed[0].success is True
    # execute was called (once for confirm, zero for process since SURFACE)
    executor.execute.assert_called_once()


@pytest.mark.asyncio
async def test_confirm_deletes_pending_record_from_storage(engine, storage):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action], request_id="req-del"), USER, executor)

    pending = receipt.pending[0]
    await engine.confirm(pending.request_id, pending.action_index, USER, executor)

    # The pending record must be gone from storage
    stored = storage.list("autonomy", "state", USER)
    pending_records = [r for r in stored if r.key.startswith("pending:")]
    assert len(pending_records) == 0


@pytest.mark.asyncio
async def test_confirm_not_found_returns_empty_receipt(engine):
    executor = _mock_executor()
    receipt = await engine.confirm("no-such-request", 0, USER, executor)
    assert len(receipt.executed) == 0
    assert "not found" in receipt.response_text.lower()


# ---------------------------------------------------------------------------
# reject()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_deletes_pending_record_from_storage(engine, storage):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action], request_id="req-rej"), USER, executor)

    pending = receipt.pending[0]
    await engine.reject(pending.request_id, pending.action_index, USER)

    stored = storage.list("autonomy", "state", USER)
    pending_records = [r for r in stored if r.key.startswith("pending:")]
    assert len(pending_records) == 0


@pytest.mark.asyncio
async def test_reject_logs_rejection_to_history(engine, action_log):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    receipt = await engine.process(_result([action], request_id="req-log"), USER, executor)

    pending = receipt.pending[0]
    await engine.reject(pending.request_id, pending.action_index, USER)

    entries = action_log.list_recent(USER)
    rejected_entries = [e for e in entries if e.status == "rejected_user"]
    assert len(rejected_entries) == 1


@pytest.mark.asyncio
async def test_reject_not_found_returns_empty_receipt(engine):
    receipt = await engine.reject("no-such-request", 0, USER)
    assert len(receipt.executed) == 0
    assert "not found" in receipt.response_text.lower()


# ---------------------------------------------------------------------------
# get_pending()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pending_returns_non_expired_actions(engine):
    executor = _mock_executor()
    action = _action(domain="surface_domain")
    await engine.process(_result([action]), USER, executor)

    pending = engine.get_pending(USER)
    assert len(pending) == 1
    assert pending[0].domain == "surface_domain"


@pytest.mark.asyncio
async def test_get_pending_excludes_expired_actions(engine, storage):
    from datetime import datetime, timezone, timedelta
    from mithra.storage.adapter import StorageRecord

    # Write a pending record with a past expires_at directly to storage
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    action_id = "test-expired-id"
    storage.write(StorageRecord(
        domain="autonomy",
        key=f"pending:{action_id}",
        layer="state",
        data={
            "action_id": action_id,
            "request_id": "req-exp",
            "action_index": 0,
            "user_id": USER,
            "domain": "surface_domain",
            "action_type": "do_thing",
            "payload": {},
            "created_at": past,
            "expires_at": past,
            "intent": "surface_domain.do_thing",
            "description": "test",
        },
        user_id=USER,
        version=1,
        updated_at=past,
    ))

    pending = engine.get_pending(USER)
    assert all(p.action_id != action_id for p in pending)


# ---------------------------------------------------------------------------
# expire_pending()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expire_pending_deletes_expired_from_storage(engine, storage):
    from datetime import datetime, timezone, timedelta
    from mithra.storage.adapter import StorageRecord

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    action_id = "exp-action-id"
    storage.write(StorageRecord(
        domain="autonomy",
        key=f"pending:{action_id}",
        layer="state",
        data={
            "action_id": action_id,
            "request_id": "req-exp2",
            "action_index": 0,
            "user_id": USER,
            "domain": "surface_domain",
            "action_type": "do_thing",
            "payload": {},
            "created_at": past,
            "expires_at": past,
            "intent": "surface_domain.do_thing",
            "description": "test",
        },
        user_id=USER,
        version=1,
        updated_at=past,
    ))

    engine.expire_pending()

    stored = storage.list("autonomy", "state", USER)
    pending_records = [r for r in stored if r.key.startswith("pending:")]
    assert all(r.data["action_id"] != action_id for r in pending_records)


@pytest.mark.asyncio
async def test_expire_pending_notifies_user_if_executor_provided(storage, action_log, config_path):
    from datetime import datetime, timezone, timedelta
    from mithra.storage.adapter import StorageRecord

    executor = _mock_executor()
    eng = DefaultAutonomyEngine(storage, action_log, config_path, executor=executor)

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    action_id = "exp-notify-id"
    storage.write(StorageRecord(
        domain="autonomy",
        key=f"pending:{action_id}",
        layer="state",
        data={
            "action_id": action_id,
            "request_id": "req-exp3",
            "action_index": 0,
            "user_id": USER,
            "domain": "surface_domain",
            "action_type": "do_thing",
            "payload": {},
            "created_at": past,
            "expires_at": past,
            "intent": "surface_domain.do_thing",
            "description": "test",
        },
        user_id=USER,
        version=1,
        updated_at=past,
    ))

    eng.expire_pending()
    executor.notify.assert_called_once()
    call_args = executor.notify.call_args[0]
    assert call_args[0] == USER
    assert "expired" in call_args[2].lower()
