"""TasksDomain — Tasks domain implementation (T1-T3).

Provides 7 core tools and live dashboard data backed by the local SQLite DB.
Full implementation follows ADR section 11 (T1–T10).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from mithra.domains.base import Domain
from mithra.domains.dashboard import DashboardData

if TYPE_CHECKING:
    from mithra.domains.bundle import ToolBundle
    from mithra.domains.context import DomainContext

_logger = logging.getLogger(__name__)


class TasksDomain(Domain):
    """Tasks domain — accountability system for things without a fixed time.

    Tools: task_create, task_list, task_complete, task_update,
           task_snooze, task_delete, task_suggest.
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
        return "0.3.0"

    @property
    def display_name(self) -> str:
        return "Tasks"

    def get_tools(self, context: "DomainContext") -> list["ToolBundle"]:
        from mithra.domains.tasks.tools import build_tasks_tools
        return build_tasks_tools(context)

    async def get_dashboard_data(
        self,
        user_id: str,
        household_id: str,
    ) -> DashboardData:
        now = datetime.now(timezone.utc).isoformat()
        today = date.today().isoformat()

        if not self._local_db_path:
            return DashboardData(
                domain="tasks",
                updated_at=now,
                summary="Tasks (storage unavailable)",
                items=[],
                alerts=[],
                quick_actions=[{"label": "Add task", "action": "task_create"}],
            )

        try:
            items, alerts = self._load_dashboard(household_id, user_id, today)
        except Exception as exc:
            _logger.warning("tasks dashboard query failed: %s", exc)
            items, alerts = [], []

        inbox_count = sum(1 for i in items if i.get("status") == "inbox")
        urgent_count = sum(1 for i in items if i.get("priority") == "urgent")
        overdue_count = sum(1 for i in items if i.get("overdue"))

        parts = []
        if overdue_count:
            parts.append(f"{overdue_count} overdue")
        if urgent_count:
            parts.append(f"{urgent_count} urgent")
        if inbox_count:
            parts.append(f"{inbox_count} in inbox")

        summary = "Tasks: " + ", ".join(parts) if parts else "No open tasks"

        return DashboardData(
            domain="tasks",
            updated_at=now,
            summary=summary,
            items=items,
            alerts=alerts,
            quick_actions=[
                {"label": "Add task", "action": "task_create"},
                {"label": "My inbox", "action": "task_list", "params": {"filter": "inbox"}},
                {"label": "What's urgent", "action": "task_list", "params": {"filter": "urgent"}},
            ],
        )

    def _load_dashboard(
        self, household_id: str, user_id: str, today: str
    ) -> tuple[list[dict], list[dict]]:
        items: list[dict] = []
        alerts: list[dict] = []

        with sqlite3.connect(self._local_db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Top 10 active tasks ordered by priority + due date
            rows = conn.execute(
                """SELECT id, title, priority, status, due_date, scope
                   FROM tasks
                   WHERE household_id=? AND status NOT IN ('done','cancelled')
                   ORDER BY
                     CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END ASC,
                     CASE WHEN due_date IS NOT NULL AND due_date<=? THEN 0 ELSE 1 END ASC,
                     due_date ASC
                   LIMIT 10""",
                (household_id, today),
            ).fetchall()

            for r in rows:
                overdue = bool(r["due_date"] and r["due_date"] < today)
                items.append({
                    "id": r["id"],
                    "title": r["title"],
                    "priority": r["priority"],
                    "status": r["status"],
                    "due_date": r["due_date"],
                    "scope": r["scope"],
                    "overdue": overdue,
                })

            # Overdue alert
            overdue_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND due_date<? AND status NOT IN ('done','cancelled')",
                (household_id, today),
            ).fetchone()[0]
            if overdue_count:
                alerts.append({
                    "type": "overdue",
                    "message": f"{overdue_count} task(s) are overdue",
                    "severity": "high",
                })

            # Inbox accumulation alert (> 10 tasks sitting in inbox)
            inbox_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND status='inbox'",
                (household_id,),
            ).fetchone()[0]
            if inbox_count > 10:
                alerts.append({
                    "type": "inbox_overload",
                    "message": f"{inbox_count} tasks in inbox — consider reviewing",
                    "severity": "low",
                })

        return items, alerts
