from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class DomainEvent:
    """Cross-domain side effect emitted by a tool after successful execution.

    Events fire ONLY after autonomy confirmation. Never before.
    Firing before confirmation creates phantom side effects.
    """

    event_type: str        # e.g. "appointment_created"
    source_domain: str     # e.g. "time"
    payload: dict
    user_id: str
    household_id: str
    request_id: str        # links to originating request / A2A task_id
    timestamp: str = ""    # ISO8601; auto-set if empty

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ToolResult:
    """Standard return type for all domain tools.

    summary is a ONE-WAY DOOR: always present, always meaningful.
    LLM uses summary to compose the response — without it, LLM hallucinates.
    Never raise exceptions from tools (except infrastructure failures).
    """

    success: bool
    summary: str                    # ONE-WAY DOOR: always present
    data: dict | None = None        # structured result data
    error: str | None = None        # human-readable error message
    error_code: str | None = None   # machine-readable e.g. "GOOGLE_NOT_CONNECTED"
    undo_token: str | None = None   # if is_reversible, token needed to undo
    events: list[DomainEvent] = field(default_factory=list)

    @classmethod
    def ok(
        cls,
        summary: str,
        data: dict | None = None,
        undo_token: str | None = None,
        events: list[DomainEvent] | None = None,
    ) -> "ToolResult":
        return cls(
            success=True,
            summary=summary,
            data=data,
            undo_token=undo_token,
            events=events or [],
        )

    @classmethod
    def fail(
        cls,
        summary: str,
        error: str | None = None,
        error_code: str | None = None,
    ) -> "ToolResult":
        return cls(
            success=False,
            summary=summary,
            error=error or summary,
            error_code=error_code,
        )
