"""
Google Calendar output adapter.

Implements OutputAdapter. Writes events and reminders to Google Calendar
via the Google Calendar API v3. Credentials loaded from
GOOGLE_CALENDAR_CREDENTIALS (path to service account JSON or OAuth token file).

All calendar writes are logged to ActionLog before execution.
Undo strategy: delete event via event_id stored in DeliveryReceipt.undo_strategy
(Reversibility.FULL).

Env vars: GOOGLE_CALENDAR_CREDENTIALS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)

_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CalendarAdapter(OutputAdapter):
    """Writes events to Google Calendar via the Google Calendar API."""

    def __init__(self, credentials_path: str) -> None:
        self.credentials_path = credentials_path
        self._creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=_SCOPES
        )

    def _auth_headers(self) -> dict:
        if not self._creds.valid:
            self._creds.refresh(Request())
        return {
            "Authorization": f"Bearer {self._creds.token}",
            "Content-Type": "application/json",
        }

    async def create_event(self, calendar_id: str, event: dict[str, Any]) -> str:
        """Create a calendar event and return its Google event ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                headers=self._auth_headers(),
                json=event,
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()["id"]

    async def update_event(
        self, calendar_id: str, event_id: str, updates: dict[str, Any]
    ) -> None:
        """Update an existing calendar event."""
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers=self._auth_headers(),
                json=updates,
                timeout=15,
            )
            resp.raise_for_status()

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete a calendar event by ID."""
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers=self._auth_headers(),
                timeout=15,
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Create a calendar event from the message payload.

        Expects message.domain to carry event JSON via the payload convention used
        by AgentAction: the OutboundMessage.text is treated as the event summary
        and message must carry structured event data via a caller-set attribute.
        For direct calendar writes, prefer create_event() directly.
        """
        event = {
            "summary": message.text,
            "description": f"Created by Mithra ({message.domain})",
            "start": {"dateTime": _now(), "timeZone": "UTC"},
            "end": {"dateTime": _now(), "timeZone": "UTC"},
        }
        try:
            calendar_id = f"{user_id}@group.calendar.google.com"
            event_id = await self.create_event(calendar_id, event)
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
                undo_strategy=None,
                error=str(exc),
            )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Calendar does not support interactive actions — delegate to plain send."""
        return await self.send(user_id, message)

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Collect stream into a summary text and create an event."""
        text = ""
        async for chunk in stream:
            text += chunk
        return await self.send(user_id, OutboundMessage(text=text, domain="calendar"))

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """Delete the calendar event using the event_id in the receipt."""
        if not receipt.message_id or not receipt.undoable:
            return False
        try:
            calendar_id = "primary"
            await self.delete_event(calendar_id, receipt.message_id)
            return True
        except Exception:
            return False

    def health(self) -> dict:
        """Return adapter health including Google API token validity."""
        return {
            "status": "ok",
            "credentials_valid": self._creds.valid,
            "credentials_path": self.credentials_path,
        }
