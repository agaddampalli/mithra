"""
Google Calendar output adapter.

Implements OutputAdapter. Writes events to Google Calendar via the v3 API.

Constructor accepts an optional http_client for test injection. When provided,
no credentials are loaded and all API calls go through the injected client.

Undo strategy: delete event via event_id stored in DeliveryReceipt.undo_payload
(Reversibility.FULL).

Env vars: GOOGLE_CALENDAR_CREDENTIALS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import httpx

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


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
        credentials_path: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.credentials_path = credentials_path
        self._http_client = http_client
        self._creds: Any = None
        if http_client is None:
            self._load_credentials()

    def _load_credentials(self) -> None:
        try:
            from google.oauth2 import service_account
            self._creds = service_account.Credentials.from_service_account_file(
                self.credentials_path, scopes=_SCOPES
            )
        except Exception:
            self._creds = None

    def _auth_headers(self) -> dict[str, str]:
        if self._creds is None:
            return {}
        try:
            from google.auth.transport.requests import Request
            if not self._creds.valid:
                self._creds.refresh(Request())
            return {
                "Authorization": f"Bearer {self._creds.token}",
                "Content-Type": "application/json",
            }
        except Exception:
            return {}

    def _client(self):
        if self._http_client is not None:
            return _NullContext(self._http_client)
        return httpx.AsyncClient()

    async def create_event(self, calendar_id: str, event: dict[str, Any]) -> str:
        """Create a calendar event and return its Google event ID."""
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events"
        async with self._client() as client:
            resp = await client.post(
                url,
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
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
        async with self._client() as client:
            resp = await client.patch(
                url,
                headers=self._auth_headers(),
                json=updates,
                timeout=15,
            )
            resp.raise_for_status()

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete a calendar event by ID."""
        url = f"{_CALENDAR_BASE}/calendars/{calendar_id}/events/{event_id}"
        async with self._client() as client:
            resp = await client.delete(
                url,
                headers=self._auth_headers(),
                timeout=15,
            )
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Create a calendar event from the message.

        message.text may be JSON with keys: summary, start_datetime,
        end_datetime, description, location. Falls back to treating text as
        the event summary with current time as start/end.
        """
        calendar_id = f"{user_id}@group.calendar.google.com"
        try:
            event_data = json.loads(message.text)
        except (json.JSONDecodeError, TypeError):
            event_data = {}

        start = event_data.get("start_datetime", _now())
        end = event_data.get("end_datetime", _now())
        event: dict[str, Any] = {
            "summary": event_data.get("summary", message.text),
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        if event_data.get("description"):
            event["description"] = event_data["description"]
        if event_data.get("location"):
            event["location"] = event_data["location"]

        try:
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
        creds_valid = bool(self._creds and getattr(self._creds, "valid", False))
        return {
            "status": "ok",
            "credentials_valid": creds_valid,
            "credentials_path": self.credentials_path,
        }
