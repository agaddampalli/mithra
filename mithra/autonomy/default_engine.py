"""
DefaultAutonomyEngine — concrete implementation of AutononomyEngine.

Process order (enforced unconditionally for every action):
  1. NEVER  — block, log, track violation rate, security alert if > 3/hour
  2. OBSERVE — log signal only, no output
  3. SURFACE — create PendingAction, await user confirmation
  4. Execute — ACT_TELL or ACT_SILENT
  5. Batch notify — single notification for all ACT_TELL successes in request
  6. Build response_text

Config loading:
  autonomy.yaml loaded at __init__.
  Precedence: never_actions list > domain_overrides > SURFACE (unknown intent).
  Unknown intent never defaults to NEVER or ACT_SILENT — always SURFACE.

NEVER violation tracking: in-memory time-windowed counter per (user_id, intent).
  Does not need to survive restarts — rate alerting is transient.
  > 3 violations within 60 minutes → security alert via executor.notify().
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
import uuid

_logger = logging.getLogger(__name__)
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from mithra.autonomy.action_log import ActionLog
from mithra.autonomy.engine import (
    ActionLogEntry,
    AutononomyEngine,
    AutonomyLevel,
    ExecutionReceipt,
    PendingAction,
    RejectedAction,
    Reversibility,
)
from mithra.storage.adapter import StorageAdapter, StorageRecord

if TYPE_CHECKING:
    from mithra.agent.interface import AgentAction, AgentResult
    from mithra.autonomy.executor import ActionExecutorRegistry

_NEVER_VIOLATION_WINDOW_SECONDS = 3600
_NEVER_VIOLATION_THRESHOLD = 3  # alert fires when count exceeds this (i.e. > 3)
_UNDO_WINDOW_SECONDS = 3600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call_async(coro) -> None:
    """Run a coroutine from sync context, handling both sync and async callers."""
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        asyncio.run(coro)


def _pending_to_dict(p: PendingAction) -> dict:
    return {
        "action_id": p.action_id,
        "request_id": p.request_id,
        "action_index": p.action_index,
        "user_id": p.user_id,
        "domain": p.domain,
        "action_type": p.action_type,
        "payload": p.payload,
        "created_at": p.created_at,
        "expires_at": p.expires_at,
        "intent": p.intent,
        "description": p.description,
    }


def _dict_to_pending(d: dict) -> PendingAction:
    return PendingAction(
        action_id=d["action_id"],
        request_id=d["request_id"],
        action_index=d["action_index"],
        user_id=d["user_id"],
        domain=d["domain"],
        action_type=d["action_type"],
        payload=d.get("payload", {}),
        created_at=d["created_at"],
        expires_at=d["expires_at"],
        intent=d.get("intent", ""),
        description=d.get("description", ""),
    )


class DefaultAutonomyEngine(AutononomyEngine):
    """Concrete autonomy engine that enforces configured action levels."""

    def __init__(
        self,
        storage_adapter: StorageAdapter,
        action_log: ActionLog,
        config_path: str,
        executor: "ActionExecutorRegistry | None" = None,
    ) -> None:
        self._storage = storage_adapter
        self._action_log = action_log
        self._executor = executor
        self._violation_timestamps: dict[str, list[float]] = defaultdict(list)
        self._load_config(config_path)

    def _load_config(self, config_path: str) -> None:
        """Load and cache autonomy.yaml.  Missing file → safe defaults."""
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}

        self._default_level: int = cfg.get("default_level", AutonomyLevel.SURFACE.value)
        self._never_actions: frozenset[str] = frozenset(cfg.get("never_actions", []))
        self._domain_overrides: dict[str, int] = cfg.get("domain_overrides") or {}

    def _resolve_level(self, action: "AgentAction") -> AutonomyLevel:
        """Return the autonomy level that applies to this action.

        Precedence:
          1. never_actions list  → NEVER
          2. domain_overrides    → configured level
          3. Unknown             → SURFACE (never ACT_SILENT or NEVER for unknowns)
        """
        if action.action_type in self._never_actions:
            return AutonomyLevel.NEVER

        if action.domain in self._domain_overrides:
            level_int = self._domain_overrides[action.domain]
            return AutonomyLevel(level_int)

        # Intent not found in config — safest non-blocking default
        return AutonomyLevel.SURFACE

    def _describe_action(self, action: "AgentAction") -> str:
        """Return a human-readable description of an action for user prompts.

        Prefers action.description set by the agent (OCP — engine is closed to
        action-type changes). Falls back to a generic label so no action is silent.
        """
        if action.description:
            return action.description
        return f"complete action: {action.action_type}"

    def _pending_storage_key(self, action_id: str) -> str:
        return f"pending:{action_id}"

    async def process(
        self,
        result: "AgentResult",
        user_id: str,
        executor: "ActionExecutorRegistry",
    ) -> ExecutionReceipt:
        """Enforce autonomy levels for every action in the AgentResult.

        Steps are executed in strict order — NEVER is checked before anything
        else and no action that reaches the NEVER check can ever be executed.
        """
        receipt = ExecutionReceipt(request_id=result.request_id)
        act_tell_successes: list[tuple["AgentAction", ActionLogEntry]] = []

        for idx, action in enumerate(result.actions):
            level = self._resolve_level(action)
            intent = f"{action.domain}.{action.action_type}"

            # ----------------------------------------------------------------
            # Step 1 — NEVER: block unconditionally, track violations
            # ----------------------------------------------------------------
            if level == AutonomyLevel.NEVER:
                rejected = RejectedAction(
                    intent=intent,
                    domain=action.domain,
                    user_id=user_id,
                    reason=f"NEVER rule: {intent}",
                    attempted_at=_now(),
                )
                receipt.rejected.append(rejected)

                log_entry = ActionLogEntry(
                    action_id=str(uuid.uuid4()),
                    request_id=result.request_id,
                    user_id=user_id,
                    domain=action.domain,
                    action_type=action.action_type,
                    payload=action.payload,
                    status="rejected_never",
                    reversibility=Reversibility.NONE,
                    timestamp=_now(),
                    intent=intent,
                    autonomy_level=AutonomyLevel.NEVER.value,
                    output_channel="none",
                    executed_at="",
                    success=False,
                    error="Blocked by NEVER rule",
                )
                self._action_log.append(log_entry)

                # Track violation and alert if threshold exceeded
                vkey = f"{user_id}:{intent}"
                now_ts = time.time()
                cutoff = now_ts - _NEVER_VIOLATION_WINDOW_SECONDS
                self._violation_timestamps[vkey] = [
                    t for t in self._violation_timestamps[vkey] if t > cutoff
                ]
                self._violation_timestamps[vkey].append(now_ts)
                recent_count = len(self._violation_timestamps[vkey])

                if recent_count > _NEVER_VIOLATION_THRESHOLD:
                    await executor.notify(
                        "household",
                        "telegram",
                        f"Security alert: {recent_count} NEVER violations in the last hour "
                        f"for {intent} by {user_id}",
                    )
                continue

            # ----------------------------------------------------------------
            # Step 2 — OBSERVE: log signal, no output
            # ----------------------------------------------------------------
            if level == AutonomyLevel.OBSERVE:
                receipt.skipped.append({
                    "action_type": action.action_type,
                    "domain": action.domain,
                    "intent": intent,
                    "payload": action.payload,
                    "observed_at": _now(),
                })
                log_entry = ActionLogEntry(
                    action_id=str(uuid.uuid4()),
                    request_id=result.request_id,
                    user_id=user_id,
                    domain=action.domain,
                    action_type=action.action_type,
                    payload=action.payload,
                    status="observed",
                    reversibility=Reversibility.NONE,
                    timestamp=_now(),
                    intent=intent,
                    autonomy_level=AutonomyLevel.OBSERVE.value,
                    output_channel="none",
                    executed_at="",
                    success=False,
                )
                self._action_log.append(log_entry)
                continue

            # ----------------------------------------------------------------
            # Step 3 — SURFACE: create pending action, await confirmation
            # ----------------------------------------------------------------
            if level == AutonomyLevel.SURFACE:
                now = _now()
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).isoformat()
                description = self._describe_action(action)
                pending = PendingAction(
                    action_id=str(uuid.uuid4()),
                    request_id=result.request_id,
                    action_index=idx,
                    user_id=user_id,
                    domain=action.domain,
                    action_type=action.action_type,
                    payload=action.payload,
                    created_at=now,
                    expires_at=expires_at,
                    intent=intent,
                    description=description,
                )
                self._storage.write(StorageRecord(
                    domain="autonomy",
                    key=self._pending_storage_key(pending.action_id),
                    layer="state",
                    data=_pending_to_dict(pending),
                    user_id=user_id,
                    version=1,
                    updated_at=now,
                ))
                receipt.pending.append(pending)
                continue

            # ----------------------------------------------------------------
            # Step 4 — Execute: ACT_TELL (2) or ACT_SILENT (3)
            # ----------------------------------------------------------------
            exec_result = await executor.execute(action)
            now = _now()

            try:
                rev = Reversibility(action.reversibility)
            except ValueError:
                rev = Reversibility.FULL

            undoable = rev != Reversibility.NONE and exec_result.success
            undo_expires_at = (
                (datetime.now(timezone.utc) + timedelta(seconds=_UNDO_WINDOW_SECONDS)).isoformat()
                if undoable else None
            )
            undo_strategy = (
                action.reversibility if undoable else None
            )

            log_entry = ActionLogEntry(
                action_id=exec_result.action_id,
                request_id=result.request_id,
                user_id=user_id,
                domain=action.domain,
                action_type=action.action_type,
                payload=action.payload,
                status="executed" if exec_result.success else "failed",
                reversibility=rev,
                timestamp=now,
                intent=intent,
                autonomy_level=level.value,
                output_channel=action.output_channel,
                executed_at=now,
                success=exec_result.success,
                undo_payload=exec_result.undo_payload,
                error=exec_result.error,
                undoable=undoable,
                undo_expires_at=undo_expires_at,
                undo_strategy=undo_strategy,
            )
            self._action_log.append(log_entry)
            receipt.executed.append(log_entry)

            if level == AutonomyLevel.ACT_TELL and exec_result.success:
                act_tell_successes.append((action, log_entry))

        # --------------------------------------------------------------------
        # Step 5 — Batch ACT_TELL notifications (never notify for ACT_SILENT,
        #           never notify if execution failed)
        # --------------------------------------------------------------------
        if act_tell_successes:
            notify_channel = _notification_channel(act_tell_successes[0][0])
            if len(act_tell_successes) == 1:
                action, entry = act_tell_successes[0]
                message = f"Done: {entry.intent}"
            else:
                lines = [f"• {entry.intent}" for _, entry in act_tell_successes]
                message = f"Completed {len(act_tell_successes)} actions:\n" + "\n".join(lines)
            await executor.notify(user_id, notify_channel, message)
            receipt.notification_sent = True

        # --------------------------------------------------------------------
        # Step 6 — Build response_text
        # --------------------------------------------------------------------
        executed_descs = [e.action_type for e in receipt.executed]
        pending_descs = [p.description for p in receipt.pending]
        rejected_intents = [r.intent for r in receipt.rejected]

        if receipt.executed:
            if any("calendar" in e for e in executed_descs):
                receipt.response_text = "✅ Added to your calendar!"
            else:
                receipt.response_text = "✅ Done."
        elif receipt.pending:
            description = pending_descs[0] if pending_descs else "this action"
            receipt.response_text = f"Should I {description}?"
        elif receipt.rejected:
            receipt.response_text = "Sorry, that action is not permitted."
        # If no actions — leave receipt.response_text empty; agent response is used
        return receipt

    async def confirm(
        self,
        request_id: str,
        action_index: int,
        user_id: str,
        executor: "ActionExecutorRegistry",
    ) -> ExecutionReceipt:
        """Execute a pending action the user has confirmed."""
        pending = self._find_pending(request_id, action_index, user_id)
        receipt = ExecutionReceipt(request_id=request_id)

        if pending is None:
            receipt.response_text = "Pending action not found or already expired."
            return receipt

        # Remove from pending store
        self._storage.delete("autonomy", self._pending_storage_key(pending.action_id), user_id)

        from mithra.agent.interface import AgentAction
        action = AgentAction(
            action_type=pending.action_type,
            domain=pending.domain,
            payload=pending.payload,
            autonomy_level=AutonomyLevel.SURFACE.value,
            reversibility="full",
        )
        exec_result = await executor.execute(action)
        now = _now()

        log_entry = ActionLogEntry(
            action_id=exec_result.action_id,
            request_id=request_id,
            user_id=user_id,
            domain=pending.domain,
            action_type=pending.action_type,
            payload=pending.payload,
            status="executed" if exec_result.success else "failed",
            reversibility=Reversibility.FULL,
            timestamp=now,
            intent=pending.intent,
            autonomy_level=AutonomyLevel.SURFACE.value,
            output_channel="none",
            executed_at=now,
            success=exec_result.success,
            undo_payload=exec_result.undo_payload,
            error=exec_result.error,
            undoable=exec_result.success,
            undo_expires_at=(
                (datetime.now(timezone.utc) + timedelta(seconds=_UNDO_WINDOW_SECONDS)).isoformat()
                if exec_result.success else None
            ),
        )
        self._action_log.append(log_entry)
        receipt.executed.append(log_entry)
        if exec_result.success:
            if "calendar" in pending.action_type:
                receipt.response_text = "✅ Added to your calendar!"
            else:
                receipt.response_text = "✅ Done."
        else:
            _logger.error("confirm() execution failed for %s: %s", pending.intent, exec_result.error)
            receipt.response_text = f"❌ Couldn't complete that. Error: {exec_result.error}"
        return receipt

    async def reject(
        self,
        request_id: str,
        action_index: int,
        user_id: str,
    ) -> ExecutionReceipt:
        """Cancel a pending action the user has rejected."""
        pending = self._find_pending(request_id, action_index, user_id)
        receipt = ExecutionReceipt(request_id=request_id)

        if pending is None:
            receipt.response_text = "Pending action not found or already expired."
            return receipt

        self._storage.delete("autonomy", self._pending_storage_key(pending.action_id), user_id)

        log_entry = ActionLogEntry(
            action_id=str(uuid.uuid4()),
            request_id=request_id,
            user_id=user_id,
            domain=pending.domain,
            action_type=pending.action_type,
            payload=pending.payload,
            status="rejected_user",
            reversibility=Reversibility.NONE,
            timestamp=_now(),
            intent=pending.intent,
            autonomy_level=AutonomyLevel.SURFACE.value,
            output_channel="none",
            executed_at="",
            success=False,
            error="Rejected by user",
        )
        self._action_log.append(log_entry)
        receipt.skipped.append({"action_id": pending.action_id, "reason": "rejected_user"})
        receipt.response_text = f"Cancelled: {pending.intent}"
        return receipt

    async def undo(
        self,
        action_id: str,
        user_id: str,
        executor: "ActionExecutorRegistry",
    ) -> ExecutionReceipt:
        """Attempt to undo a previously executed action."""
        receipt = ExecutionReceipt(request_id="undo")
        entry = self._action_log.get(action_id, user_id)

        if entry is None:
            receipt.response_text = "Action not found."
            return receipt
        if not entry.undoable:
            receipt.response_text = f"Cannot undo: {entry.intent} ({entry.reversibility.value})"
            return receipt
        if entry.undo_expires_at:
            expiry = datetime.fromisoformat(entry.undo_expires_at)
            if datetime.now(timezone.utc) > expiry:
                receipt.response_text = f"Undo window has closed for {entry.intent}"
                return receipt

        undo_result = await executor.undo(entry)
        if undo_result.success:
            receipt.response_text = f"Undone: {entry.intent}"
        else:
            receipt.response_text = f"Undo failed for {entry.intent}: {undo_result.error}"
        return receipt

    def get_pending(self, user_id: str) -> list[PendingAction]:
        """Return all non-expired pending actions for a user."""
        records = self._storage.list("autonomy", "state", user_id)
        now = datetime.now(timezone.utc)
        pending = []
        for record in records:
            if not record.key.startswith("pending:"):
                continue
            p = _dict_to_pending(record.data)
            if datetime.fromisoformat(p.expires_at) > now:
                pending.append(p)
        return pending

    def get_log(
        self,
        user_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[ActionLogEntry]:
        """Return recent action log entries for a user."""
        return self._action_log.list_recent(user_id, domain=domain, limit=limit)

    def expire_pending(self) -> None:
        """Delete expired pending actions from storage and notify users."""
        now = datetime.now(timezone.utc)
        for user_id in ("person_a", "person_b", "household"):
            records = self._storage.list("autonomy", "state", user_id)
            for record in records:
                if not record.key.startswith("pending:"):
                    continue
                p = _dict_to_pending(record.data)
                if datetime.fromisoformat(p.expires_at) <= now:
                    self._storage.delete("autonomy", record.key, user_id)
                    if self._executor is not None:
                        msg = f"Your pending action '{p.action_type}' has expired without confirmation."
                        _call_async(self._executor.notify(user_id, "telegram", msg))

    def health(self) -> dict:
        """Return autonomy engine health for the health dashboard."""
        total_pending = sum(
            len(self.get_pending(uid)) for uid in ("person_a", "person_b", "household")
        )
        total_violations = sum(len(v) for v in self._violation_timestamps.values())
        return {
            "status": "ok",
            "pending_actions": total_pending,
            "never_violation_tracking_keys": len(self._violation_timestamps),
            "active_violations_in_window": total_violations,
        }

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _find_pending(
        self, request_id: str, action_index: int, user_id: str
    ) -> PendingAction | None:
        for record in self._storage.list("autonomy", "state", user_id):
            if not record.key.startswith("pending:"):
                continue
            p = _dict_to_pending(record.data)
            if p.request_id == request_id and p.action_index == action_index:
                return p
        return None


def _notification_channel(action: "AgentAction") -> str:
    """Prefer the action's own output channel, fall back to telegram."""
    if action.output_channel and action.output_channel != "none":
        return action.output_channel
    return "telegram"


