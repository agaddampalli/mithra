"""
Google Calendar output adapter.

Implements OutputAdapter. Writes events to Google Calendar via the v3 API.

Tokens are sourced from OAuthManager (per-user OAuth via Authlib).
Constructor accepts an optional http_client for test injection.

Undo strategy: delete event via event_id stored in DeliveryReceipt.undo_payload
(Reversibility.FULL).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx

_logger = logging.getLogger(__name__)

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _NullContext:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *args: object) -> None:
        pass


class CalendarAdapter(OutputAdapter):
    """Writes events to Google Calendar via the Google Calendar API v3."""

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        oauth_manager: Any | None = None,
        household_id: str = "",
        storage_adapter: Any | None = None,
    ) -> None:
        self._http_client = http_client
        self._oauth_manager = oauth_manager
        self._household_id = household_id
        self._storage = storage_adapter

    def _client(self):
        if self._http_client is not None:
            return _NullContext(self._http_client)
        return httpx.AsyncClient()

    async def _headers_for(self, user_id: str = "") -> dict[str, str]:
        """Return auth headers for the given user.

        When OAuthManager is configured but returns no token, raises immediately —
        an unauthenticated Google API call will always 401.
        When no OAuthManager is configured (unit tests / dev), passes through.
        """
        if user_id and self._oauth_manager:
            token = await self._oauth_manager.get_token("google", user_id)
            if token:
                return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            raise RuntimeError(
                f"No valid Google token for user '{user_id}'. "
                "Send 'connect google' in Telegram to connect."
            )
        return {"Content-Type": "application/json"}

    async def create_event(
        self, calendar_id: str, event: dict[str, Any], user_id: str = ""
    ) -> str:
        """Create a calendar event and return its Google event ID."""
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events"
        headers = await self._headers_for(user_id)
        async with self._client() as client:
            resp = await client.post(url, headers=headers, json=event, timeout=15)
            resp.raise_for_status()
            return resp.json()["id"]

    async def update_event(
        self, calendar_id: str, event_id: str, updates: dict[str, Any], user_id: str = ""
    ) -> None:
        """Update an existing calendar event."""
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
        headers = await self._headers_for(user_id)
        async with self._client() as client:
            resp = await client.patch(url, headers=headers, json=updates, timeout=15)
            resp.raise_for_status()

    async def delete_event(
        self, calendar_id: str, event_id: str, user_id: str = ""
    ) -> None:
        """Delete a calendar event by ID."""
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
        headers = await self._headers_for(user_id)
        async with self._client() as client:
            resp = await client.delete(url, headers=headers, timeout=15)
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    def _build_event(self, payload: dict[str, Any], fallback_summary: str = "New event") -> dict[str, Any]:
        """Build a Google Calendar event dict from a flat payload."""
        start = payload.get("start_datetime", _now())
        end = payload.get("end_datetime", _now())
        event: dict[str, Any] = {
            "summary": payload.get("summary", fallback_summary),
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        if payload.get("description"):
            event["description"] = payload["description"]
        if payload.get("location"):
            event["location"] = payload["location"]
        return event

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Create a calendar event from the message.

        message.text may be JSON with keys: summary, start_datetime,
        end_datetime, description, location. Falls back to treating text as
        the event summary with current time as start/end.
        """
        calendar_id = "primary"
        try:
            payload = json.loads(message.text)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        event = self._build_event(payload, fallback_summary=message.text or "New event")

        try:
            event_id = await self.create_event(calendar_id, event, user_id=user_id)
            return DeliveryReceipt(
                channel="calendar",
                message_id=event_id,
                delivered_at=_now(),
                success=True,
                undoable=True,
                undo_strategy="delete_event",
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="calendar",
                message_id=None,
                delivered_at=_now(),
                success=False,
                undoable=False,
                error=str(exc),
            )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Calendar does not support interactive actions -- delegate to plain send."""
        return await self.send(user_id, message)

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Collect stream into a summary text and create an event."""
        text = ""
        async for chunk in stream:
            text += chunk
        return await self.send(user_id, OutboundMessage(text=text, domain="calendar"))

    async def execute(self, action: Any) -> Any:
        """Execute an AgentAction — create a calendar event from action.payload."""
        from mithra.autonomy.executor import ExecutionResult

        payload = action.payload or {}
        user_id = payload.get("user_id", "")
        calendar_id = "primary"

        action_id = str(uuid.uuid4())
        event = self._build_event(payload)
        try:
            event_id = await self.create_event(calendar_id, event, user_id=user_id)
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=True,
                delivery_receipt={"event_id": event_id, "calendar_id": calendar_id},
                undo_payload={"event_id": event_id, "calendar_id": calendar_id},
            )
        except Exception as exc:
            _logger.error("CalendarAdapter.execute failed: %s", exc, exc_info=True)
            return ExecutionResult(
                action_id=action_id,
                action_type=action.action_type,
                success=False,
                error=str(exc),
            )

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Delete the calendar event using the event_id in the receipt."""
        if not receipt.message_id or not receipt.undoable:
            return False
        if receipt.undo_strategy != "delete_event":
            return False
        try:
            # calendar_id stored in undo_payload by executor; fallback to primary
            calendar_id = "primary"
            await self.delete_event(calendar_id, receipt.message_id)
            return True
        except Exception:
            return False

    def health(self) -> dict:
        """Return adapter health."""
        return {
            "status": "ok",
            "oauth_configured": self._oauth_manager is not None,
        }
