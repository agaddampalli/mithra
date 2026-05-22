"""
ActionLog — append-only record of every action the agent has taken or proposed.

Backed by StorageAdapter domain="autonomy" layer="history". Rows are never
updated or deleted; corrections are new entries. Survives container restarts.

Used for:
  - Auditability
  - Undo context (ActionLogEntry.undo_payload)
  - AutononomyEngine.get_log() queries
  - Health dashboard dead-letter surfacing
"""

from __future__ import annotations

from mithra.autonomy.engine import ActionLogEntry, Reversibility
from mithra.storage.adapter import StorageAdapter, StorageRecord


def _entry_to_dict(entry: ActionLogEntry) -> dict:
    return {
        "action_id": entry.action_id,
        "request_id": entry.request_id,
        "user_id": entry.user_id,
        "domain": entry.domain,
        "action_type": entry.action_type,
        "payload": entry.payload,
        "status": entry.status,
        "reversibility": entry.reversibility.value,
        "timestamp": entry.timestamp,
        "intent": entry.intent,
        "autonomy_level": entry.autonomy_level,
        "output_channel": entry.output_channel,
        "executed_at": entry.executed_at,
        "success": entry.success,
        "undo_payload": entry.undo_payload,
        "error": entry.error,
        "undoable": entry.undoable,
        "undo_expires_at": entry.undo_expires_at,
        "undo_strategy": entry.undo_strategy,
    }


def _dict_to_entry(d: dict) -> ActionLogEntry:
    return ActionLogEntry(
        action_id=d["action_id"],
        request_id=d["request_id"],
        user_id=d["user_id"],
        domain=d["domain"],
        action_type=d["action_type"],
        payload=d.get("payload", {}),
        status=d["status"],
        reversibility=Reversibility(d.get("reversibility", "full")),
        timestamp=d["timestamp"],
        intent=d.get("intent", ""),
        autonomy_level=d.get("autonomy_level", 1),
        output_channel=d.get("output_channel", "none"),
        executed_at=d.get("executed_at", ""),
        success=d.get("success", False),
        undo_payload=d.get("undo_payload", {}),
        error=d.get("error"),
        undoable=d.get("undoable", False),
        undo_expires_at=d.get("undo_expires_at"),
        undo_strategy=d.get("undo_strategy"),
    )


class ActionLog:
    """Append-only store for ActionLogEntry records."""

    def __init__(self, storage_adapter: StorageAdapter, household_id: str = "") -> None:
        """
        Args:
            storage_adapter: A StorageAdapter used for domain="autonomy" layer="history".
            household_id: Household namespace from household.yaml.
        """
        self._storage = storage_adapter
        self._household_id = household_id

    def append(self, entry: ActionLogEntry) -> ActionLogEntry:
        """Append an entry and return it (with any storage-assigned fields set)."""
        record = StorageRecord(
            domain="autonomy",
            key=entry.action_id,
            layer="history",
            data=_entry_to_dict(entry),
            user_id=entry.user_id,
            version=1,
            updated_at=entry.timestamp,
            household_id=self._household_id,
        )
        self._storage.write(record)
        return entry

    def get(self, action_id: str, user_id: str) -> ActionLogEntry | None:
        """Retrieve a single entry by action_id."""
        record = self._storage.read("autonomy", action_id, "history", user_id)
        return _dict_to_entry(record.data) if record else None

    def list_recent(
        self,
        user_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[ActionLogEntry]:
        """Return recent entries for a user, optionally filtered by domain."""
        records = self._storage.list("autonomy", "history", user_id)
        entries = [_dict_to_entry(r.data) for r in records]
        if domain:
            entries = [e for e in entries if e.domain == domain]
        # Sort newest first by timestamp
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]
