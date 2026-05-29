"""GoogleCalendarService — thin gcsa wrapper for Time domain.

Accepts OAuthManager credentials, caches per-user GoogleCalendar instances,
and provides a clean sync interface over gcsa.
All public methods are synchronous — callers wrap with asyncio.to_thread.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

_logger = logging.getLogger(__name__)


def _to_aware(dt: datetime, tz_name: str) -> datetime:
    """Ensure a datetime is timezone-aware using the household timezone."""
    if dt.tzinfo is not None:
        return dt
    import pytz
    tz = pytz.timezone(tz_name)
    return tz.localize(dt)


class GoogleCalendarService:
    """Thin wrapper around gcsa for Time domain tools.

    Creates one GoogleCalendar instance per user, cached in memory.
    Credentials are built from OAuthManager's token store so gcsa can
    auto-refresh when the access token expires.

    oauth_manager must expose: _store.load(provider_id, user_id) -> dict | None
    """

    def __init__(
        self,
        oauth_manager: Any,
        household_id: str,
        client_id: str = "",
        client_secret: str = "",
        household_timezone: str = "UTC",
    ) -> None:
        self._oauth = oauth_manager
        self._household_id = household_id
        self._client_id = client_id or os.environ.get("GOOGLE_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("GOOGLE_CLIENT_SECRET", "")
        self._household_tz = household_timezone
        self._gc_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Internal — credential building
    # ------------------------------------------------------------------

    def _get_gc(self, user_id: str) -> Any:
        """Return cached or newly constructed GoogleCalendar for user."""
        if user_id in self._gc_cache:
            return self._gc_cache[user_id]

        if self._oauth is None:
            raise RuntimeError(
                "Google not connected — send 'connect google' to Mithra."
            )

        token_data = self._oauth._store.load("google", user_id)
        if not token_data:
            raise RuntimeError(
                f"Google not connected for {user_id}. "
                "Send 'connect google' to Mithra."
            )

        from google.oauth2.credentials import Credentials
        from gcsa.google_calendar import GoogleCalendar

        scopes = token_data.get("scopes") or [
            "https://www.googleapis.com/auth/calendar"
        ]
        if isinstance(scopes, str):
            scopes = scopes.split()

        creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=scopes,
            token_uri="https://oauth2.googleapis.com/token",
        )
        gc = GoogleCalendar(credentials=creds)
        self._gc_cache[user_id] = gc
        return gc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_events(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        calendar_id: str = "primary",
    ) -> list[Any]:
        """Return events in [start, end)."""
        gc = self._get_gc(user_id)
        start = _to_aware(start, self._household_tz)
        end = _to_aware(end, self._household_tz)
        return list(gc.get_events(start, end, calendar_id=calendar_id))

    def get_event(
        self,
        user_id: str,
        event_id: str,
        calendar_id: str = "primary",
    ) -> Any | None:
        """Return event by ID, or None if not found."""
        gc = self._get_gc(user_id)
        try:
            return gc.get_event(event_id, calendar_id=calendar_id)
        except Exception:
            return None

    def find_events_by_title(
        self,
        user_id: str,
        query: str,
        start: datetime | None = None,
        end: datetime | None = None,
        calendar_id: str = "primary",
    ) -> list[Any]:
        """Find events matching a title substring (case-insensitive)."""
        if start is None:
            start = datetime.now(timezone.utc) - timedelta(days=7)
        if end is None:
            end = datetime.now(timezone.utc) + timedelta(days=90)
        events = self.list_events(user_id, start, end, calendar_id=calendar_id)
        q = query.lower()
        return [e for e in events if q in (e.summary or "").lower()]

    def create_event(
        self,
        user_id: str,
        event: Any,
        calendar_id: str = "primary",
    ) -> Any:
        """Create an event and return the created Event object."""
        gc = self._get_gc(user_id)
        return gc.add_event(event, calendar_id=calendar_id)

    def update_event(
        self,
        user_id: str,
        event: Any,
        calendar_id: str = "primary",
    ) -> Any:
        """Update an existing event."""
        gc = self._get_gc(user_id)
        return gc.update_event(event, calendar_id=calendar_id)

    def delete_event(
        self,
        user_id: str,
        event_id: str,
        calendar_id: str = "primary",
    ) -> None:
        """Delete an event by ID."""
        gc = self._get_gc(user_id)
        gc.delete_event(event_id, calendar_id=calendar_id)

    def get_free_busy(
        self,
        user_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> dict[str, list[dict]]:
        """Return busy time blocks per user. Format: {user_id: [{start, end}]}."""
        result: dict[str, list[dict]] = {}
        for uid in user_ids:
            try:
                events = self.list_events(uid, start, end)
                blocks = []
                for ev in events:
                    ev_start = getattr(ev, "start", None)
                    ev_end = getattr(ev, "end", None)
                    if ev_start and ev_end:
                        if isinstance(ev_start, date) and not isinstance(ev_start, datetime):
                            ev_start = datetime(ev_start.year, ev_start.month, ev_start.day,
                                                tzinfo=timezone.utc)
                        if isinstance(ev_end, date) and not isinstance(ev_end, datetime):
                            ev_end = datetime(ev_end.year, ev_end.month, ev_end.day,
                                              tzinfo=timezone.utc)
                        blocks.append({"start": ev_start, "end": ev_end})
                result[uid] = blocks
            except Exception as exc:
                _logger.warning("get_free_busy failed for %s: %s", uid, exc)
                result[uid] = []
        return result

    def list_calendars(self, user_id: str) -> list[Any]:
        """Return all calendars for user."""
        gc = self._get_gc(user_id)
        return list(gc.get_calendar_list())

    def invalidate_cache(self, user_id: str) -> None:
        """Remove cached gc instance — forces credential reload on next call."""
        self._gc_cache.pop(user_id, None)
