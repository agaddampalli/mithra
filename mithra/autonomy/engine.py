"""
AutononomyEngine — enforces the configured autonomy level for every agent action.

Process order: NEVER check first → OBSERVE → SURFACE → execute.

Levels:
  OBSERVE    (0) — no output, log signal only
  SURFACE    (1) — tell user, they decide (Level 1 confirmation required)
  ACT_TELL   (2) — act then notify; never notify without acting
  ACT_SILENT (3) — act, no notification unless failure
  NEVER      (-1) — permanent hard wall; not configurable at runtime

NEVER violations: logged as security events.
  >3 NEVER violations per hour → security alert triggered.

ACT_TELL: batch multiple actions in one request into a single notification.

Pending store: StorageAdapter domain="autonomy" layer="state"
Action log:    StorageAdapter domain="autonomy" layer="history"
Both survive restarts. No in-memory or Redis dependency.

PendingAction: expires 24 hours after creation. User is notified on expiry.

Reversibility on ActionLogEntry:
  FULL    — restore previous state (calendar delete, storage revert)
  PARTIAL — send a correction follow-up (WhatsApp)
  NONE    — cannot undo (TTS, push notification)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class AutonomyLevel(Enum):
    """Configured autonomy level for a user or domain."""

    OBSERVE = 0
    SURFACE = 1
    ACT_TELL = 2
    ACT_SILENT = 3
    NEVER = -1


class Reversibility(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


@dataclass
class ActionLogEntry:
    """Immutable record of an action that was proposed or executed."""

    action_id: str
    request_id: str
    user_id: str
    domain: str
    action_type: str
    payload: dict
    status: str
    reversibility: Reversibility
    timestamp: str
    intent: str                   # domain.action e.g. "people.send_birthday_message"
    autonomy_level: int           # the level that was applied (0-3)
    output_channel: str           # where the action was sent
    executed_at: str              # ISO timestamp of execution
    success: bool                 # did the action succeed?
    undo_payload: dict = field(default_factory=dict)
    error: str | None = None      # error message if success=False
    undoable: bool = False        # is undo possible?
    undo_expires_at: str | None = None   # when the undo window closes, None if not undoable
    undo_strategy: str | None = None     # "delete_event"|"restore_version"|"send_correction"|None


@dataclass
class PendingAction:
    """An action awaiting user confirmation at autonomy level SURFACE."""

    action_id: str
    request_id: str
    action_index: int
    user_id: str
    domain: str
    action_type: str
    payload: dict
    created_at: str
    expires_at: str
    intent: str        # domain.action
    description: str   # human-readable e.g. "Send birthday message to John on WhatsApp"


@dataclass
class RejectedAction:
    """A NEVER-level action that was blocked. Logged for security audit."""

    intent: str
    domain: str
    user_id: str
    reason: str         # "NEVER rule: money.execute_payment"
    attempted_at: str


@dataclass
class ExecutionReceipt:
    """Result of AutononomyEngine.process() — what happened to each action."""

    request_id: str
    executed: list[ActionLogEntry] = field(default_factory=list)
    pending: list[PendingAction] = field(default_factory=list)
    rejected: list[RejectedAction] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    notification_sent: bool = False
    response_text: str = ""


class AutononomyEngine(ABC):
    """Enforces autonomy levels and manages action confirmation flow."""

    @abstractmethod
    async def process(
        self, result: "AgentResult", user_id: str, executor: "ActionExecutorRegistry"
    ) -> ExecutionReceipt:
        """Evaluate all actions in an AgentResult and execute or queue them."""
        ...

    @abstractmethod
    def confirm(
        self, request_id: str, action_index: int, user_id: str, executor: "ActionExecutorRegistry"
    ) -> ExecutionReceipt:
        """User confirmed a pending action; execute it."""
        ...

    @abstractmethod
    def reject(
        self, request_id: str, action_index: int, user_id: str
    ) -> ExecutionReceipt:
        """User rejected a pending action; cancel and log it."""
        ...

    @abstractmethod
    def undo(
        self, action_id: str, user_id: str, executor: "ActionExecutorRegistry"
    ) -> ExecutionReceipt:
        """Attempt to undo a previously executed action."""
        ...

    @abstractmethod
    def get_pending(self, user_id: str) -> list[PendingAction]:
        """Return all pending actions awaiting confirmation for a user."""
        ...

    @abstractmethod
    def get_log(
        self, user_id: str, domain: str | None = None, limit: int = 50
    ) -> list[ActionLogEntry]:
        """Return recent action log entries for a user."""
        ...

    @abstractmethod
    def expire_pending(self) -> None:
        """Mark expired pending actions as expired and notify users."""
        ...

    @abstractmethod
    def health(self) -> dict:
        """Return health status for the health dashboard."""
        ...
