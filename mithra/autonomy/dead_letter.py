"""
DeadLetterQueue — stores failed deliveries after RetryableOutputAdapter exhausts
all retry attempts (3 attempts, exponential backoff 1s → 2s → 4s, max 30s).

Backed by StorageAdapter domain="observability" layer="history".
Dead letters are surfaced on the health dashboard. An operator (or future
recovery flow) can re-drive them manually.

RetryableOutputAdapter writes to this queue; it does not call the adapter
directly. DeadLetterQueue does not retry — it is a terminal failure store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mithra.storage.adapter import StorageAdapter, StorageRecord


@dataclass
class DeadLetter:
    """A delivery that failed all retry attempts."""

    letter_id: str
    action_id: str
    user_id: str
    action_type: str
    payload: dict
    error: str
    attempt_count: int
    first_attempted_at: str
    last_attempted_at: str
    resolved: bool = False


def _letter_to_dict(letter: DeadLetter) -> dict:
    return {
        "letter_id": letter.letter_id,
        "action_id": letter.action_id,
        "user_id": letter.user_id,
        "action_type": letter.action_type,
        "payload": letter.payload,
        "error": letter.error,
        "attempt_count": letter.attempt_count,
        "first_attempted_at": letter.first_attempted_at,
        "last_attempted_at": letter.last_attempted_at,
        "resolved": letter.resolved,
    }


def _dict_to_letter(d: dict) -> DeadLetter:
    return DeadLetter(
        letter_id=d["letter_id"],
        action_id=d["action_id"],
        user_id=d["user_id"],
        action_type=d["action_type"],
        payload=d.get("payload", {}),
        error=d["error"],
        attempt_count=d.get("attempt_count", 3),
        first_attempted_at=d["first_attempted_at"],
        last_attempted_at=d["last_attempted_at"],
        resolved=d.get("resolved", False),
    )


class DeadLetterQueue:
    """Stores and surfaces failed deliveries for operator review."""

    def __init__(self, storage_adapter: StorageAdapter) -> None:
        """
        Args:
            storage_adapter: StorageAdapter used for domain="observability" layer="history".
        """
        self._storage = storage_adapter

    def enqueue(self, letter: DeadLetter) -> DeadLetter:
        """Persist a failed delivery to the dead-letter store."""
        record = StorageRecord(
            domain="observability",
            key=letter.letter_id,
            layer="history",
            data=_letter_to_dict(letter),
            user_id=letter.user_id,
            version=1,
            updated_at=letter.last_attempted_at,
        )
        self._storage.write(record)
        return letter

    def list_unresolved(self, limit: int = 100) -> list[DeadLetter]:
        """Return unresolved dead letters, newest first."""
        records = self._storage.list("observability", "history", "household")
        letters = [
            _dict_to_letter(r.data)
            for r in records
            if not r.data.get("resolved", False)
        ]
        letters.sort(key=lambda l: l.last_attempted_at, reverse=True)
        return letters[:limit]

    def resolve(self, letter_id: str) -> bool:
        """Mark a dead letter as resolved. Return True if found."""
        record = self._storage.read("observability", letter_id, "history", "household")
        if not record:
            return False
        record.data["resolved"] = True
        self._storage.write(record)
        return True

    def health(self) -> dict:
        """Return dead-letter queue stats for the health dashboard."""
        try:
            unresolved = self.list_unresolved()
            return {
                "status": "ok",
                "unresolved_count": len(unresolved),
                "oldest_unresolved": unresolved[-1].first_attempted_at if unresolved else None,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
