"""TasksDomain — stub implementation.

Full implementation follows ADR section 11 (T1–T10).
Registered at startup so /api/v1/dashboard/tasks and
intent routing work before tools are implemented.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from mithra.domains.base import Domain
from mithra.domains.dashboard import DashboardData

if TYPE_CHECKING:
    from mithra.domains.bundle import ToolBundle
    from mithra.domains.context import DomainContext

_logger = logging.getLogger(__name__)


class TasksDomain(Domain):
    """Tasks domain — accountability system for things without a fixed time.

    Stub: returns empty tools and placeholder dashboard data.
    Full implementation: 13 tools, 5 tables, capture flow, routines, projects.
    """

    def __init__(
        self,
        household_id: str = "",
        household_timezone: str = "UTC",
        default_user_id: str = "",
        local_db_path: str = "",
    ) -> None:
        self._household_id = household_id
        self._household_timezone = household_timezone
        self._default_user_id = default_user_id
        self._local_db_path = local_db_path

    @property
    def domain_id(self) -> str:
        return "tasks"

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def display_name(self) -> str:
        return "Tasks"

    def get_tools(self, context: "DomainContext") -> list["ToolBundle"]:
        return []

    async def get_dashboard_data(
        self,
        user_id: str,
        household_id: str,
    ) -> DashboardData:
        return DashboardData(
            domain="tasks",
            updated_at=datetime.now(timezone.utc).isoformat(),
            summary="Tasks coming soon",
            items=[],
            alerts=[],
            quick_actions=[{"label": "Add task", "action": "task_create"}],
        )
