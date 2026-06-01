"""TimeDomain — Time & Calendar domain plugin.

Implements the Domain ABC. Provides 8 calendar tools via build_time_tools().
Serves dashboard data from today's events.
Emits: appointment_created, appointment_updated, appointment_cancelled,
       recurring_event_created.
Subscribes to: nothing (Time domain does not react to other domain events).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mithra.domains.base import Domain
from mithra.domains.dashboard import DashboardData, DashboardItem
from mithra.domains.time.service import GoogleCalendarService

if TYPE_CHECKING:
    from mithra.domains.bundle import ToolBundle
    from mithra.domains.context import DomainContext

_logger = logging.getLogger(__name__)


class TimeDomain(Domain):
    """Time & Calendar domain — manages all household scheduling.

    Dashboard polls every 5 minutes via GET /api/v1/dashboard/time.
    Requires: oauth_manager with Google credentials per user.
    """

    def __init__(
        self,
        oauth_manager: Any = None,
        household_id: str = "",
        household_timezone: str = "UTC",
        default_user_id: str = "",
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        self._oauth_manager = oauth_manager
        self._household_id = household_id
        self._household_timezone = household_timezone
        self._default_user_id = default_user_id
        self._client_id = client_id
        self._client_secret = client_secret

    # ------------------------------------------------------------------
    # Domain identity
    # ------------------------------------------------------------------

    @property
    def domain_id(self) -> str:
        return "time"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def display_name(self) -> str:
        return "Time & Calendar"

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_tools(self, context: "DomainContext") -> list["ToolBundle"]:
        from mithra.domains.time.tools import build_time_tools
        return build_time_tools(context)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    async def get_dashboard_data(
        self,
        user_id: str,
        household_id: str,
    ) -> DashboardData:
        """Return today's events for HA Lovelace dashboard."""
        updated_at = datetime.now(timezone.utc).isoformat()

        if self._oauth_manager is None:
            return DashboardData(
                domain="time",
                updated_at=updated_at,
                summary="Google Calendar not connected",
                items=[],
                alerts=["Send 'connect google' to Mithra to connect your calendar."],
                quick_actions=[
                    {"label": "Add event", "action": "calendar_create_event"},
                ],
            )

        try:
            import asyncio
            from mithra.domains.time.tools import _range_from_period, _format_event

            # Fall back to household owner for dashboard polling (no user_id in GET)
            uid = user_id or self._default_user_id

            svc = GoogleCalendarService(
                oauth_manager=self._oauth_manager,
                household_id=household_id or self._household_id,
                client_id=self._client_id,
                client_secret=self._client_secret,
                household_timezone=self._household_timezone,
            )

            start, end = _range_from_period("today", self._household_timezone)
            events = await asyncio.to_thread(svc.list_events, uid, start, end)
        except Exception as exc:
            _logger.error("TimeDomain dashboard error: %s", exc, exc_info=True)
            return DashboardData(
                domain="time",
                updated_at=updated_at,
                summary="Calendar unavailable",
                items=[],
                alerts=[],
                quick_actions=[
                    {"label": "Today's briefing", "action": "calendar_daily_briefing"},
                ],
            )

        now = datetime.now(timezone.utc)
        items: list[DashboardItem] = []
        for ev in events:
            ev_start = getattr(ev, "start", None)
            priority = "normal"
            if ev_start and isinstance(ev_start, datetime):
                minutes_until = (ev_start - now).total_seconds() / 60
                if 0 <= minutes_until <= 120:
                    priority = "high"
            items.append(
                DashboardItem(
                    id=getattr(ev, "id", ""),
                    title=getattr(ev, "summary", "Untitled"),
                    subtitle=_format_event(ev, self._household_timezone),
                    icon="mdi:calendar",
                    priority=priority,
                    action="calendar_read_events",
                )
            )

        count = len(events)
        label = "event" if count == 1 else "events"
        summary = f"{count} {label} today" if count else "Nothing scheduled today"

        return DashboardData(
            domain="time",
            updated_at=updated_at,
            summary=summary,
            items=items,
            alerts=[],
            quick_actions=[
                {"label": "Add event", "action": "calendar_create_event"},
                {"label": "Today's briefing", "action": "calendar_daily_briefing"},
            ],
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def get_event_subscriptions(self) -> list[str]:
        return []
