"""TimeDomain — Time & Calendar domain plugin.

Implements the Domain ABC. Provides 8 calendar tools via build_time_tools().
Serves dashboard data from today's events.
Emits: appointment_created, appointment_updated, appointment_cancelled,
       recurring_event_created.
Subscribes to: nothing (Time domain does not react to other domain events).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import TYPE_CHECKING, Any

from mithra.domains.base import Domain
from mithra.domains.dashboard import DashboardData, DashboardItem

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

    def get_dashboard_data(
        self,
        context: "DomainContext",
        scope_filter: str = "household",
        viewer_user_id: str = "",
    ) -> DashboardData:
        """Return today's calendar events from local SQLite cache (ADR 16.2)."""
        db_path = context.local_db_path
        household_id = context.household_id or self._household_id
        today = date.today().isoformat()

        if not db_path:
            return DashboardData(
                domain="time",
                summary="Calendar unavailable",
                error="No local database configured",
                quick_actions=[{"label": "Add event", "action": "calendar_create_event"}],
            )

        try:
            scope_clause = ""
            params: list = [household_id, f"{today}%"]
            if scope_filter and scope_filter != "all":
                scope_clause = "AND scope=?"
                params.append(scope_filter)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""SELECT event_id, summary, start_time, end_time, scope
                        FROM calendar_events
                        WHERE household_id=?
                          AND start_time LIKE ?
                          {scope_clause}
                        ORDER BY start_time ASC
                        LIMIT 10""",
                    params,
                ).fetchall()
        except Exception as exc:
            _logger.error("TimeDomain dashboard error: %s", exc)
            return DashboardData(
                domain="time",
                summary="Calendar unavailable",
                error=str(exc),
                quick_actions=[{"label": "Today's briefing", "action": "calendar_daily_briefing"}],
            )

        items: list[DashboardItem] = []
        for r in rows:
            start_label = r["start_time"][:16] if r["start_time"] else ""
            items.append(DashboardItem(
                id=r["event_id"],
                title=r["summary"] or "Untitled",
                domain="time",
                subtitle=start_label,
                icon="mdi:calendar",
                priority="normal",
                scope=r["scope"] or "household",
                action="calendar_read_events",
            ))

        count = len(items)
        label = "event" if count == 1 else "events"
        summary = f"{count} {label} today" if count else "Nothing scheduled today"

        return DashboardData(
            domain="time",
            summary=summary,
            items=items,
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
