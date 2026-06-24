"""TasksDomain — Tasks domain implementation (T1-T3).

Provides 7 core tools and live dashboard data backed by the local SQLite DB.
Full implementation follows ADR section 11 (T1–T10).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import TYPE_CHECKING

from mithra.domains.base import Domain
from mithra.domains.dashboard import DashboardAlert, DashboardData, DashboardItem

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

    def get_dashboard_data(
        self,
        context: "DomainContext",
        scope_filter: str = "household",
        viewer_user_id: str = "",
    ) -> DashboardData:
        db_path = context.local_db_path or self._local_db_path
        household_id = context.household_id or self._household_id
        user_id = context.user_id or self._default_user_id
        today = date.today().isoformat()

        if not db_path:
            return DashboardData(
                domain="tasks",
                summary="Tasks (storage unavailable)",
                error="No local database configured",
                quick_actions=[{"label": "Add task", "action": "task_create"}],
            )

        try:
            items, alerts, routine_summary, active_count, urgent_count, overdue_count = (
                self._load_dashboard(db_path, household_id, user_id, today, scope_filter)
            )
        except Exception as exc:
            _logger.warning("tasks dashboard query failed: %s", exc)
            return DashboardData(domain="tasks", summary="Tasks unavailable", error=str(exc))

        parts = []
        if urgent_count:
            parts.append(f"{urgent_count} urgent")
        if overdue_count:
            parts.append(f"{overdue_count} overdue")
        if active_count and not parts:
            parts.append(f"{active_count} active tasks")
        if routine_summary:
            parts.append(routine_summary)

        summary = " · ".join(parts) if parts else "All clear today! 🎉"

        return DashboardData(
            domain="tasks",
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
        self, db_path: str, household_id: str, user_id: str, today: str, scope_filter: str = "household"
    ) -> tuple[list[DashboardItem], list[DashboardAlert], str, int, int, int]:
        items: list[DashboardItem] = []
        alerts: list[DashboardAlert] = []
        routine_summary: str = ""

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Top 10 active tasks ordered by priority + due date
            rows = conn.execute(
                """SELECT id, title, priority, status, due_date
                   FROM tasks
                   WHERE household_id=? AND status NOT IN ('done','cancelled','snoozed')
                   ORDER BY
                     CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END ASC,
                     CASE WHEN due_date IS NOT NULL AND due_date<=? THEN 0 ELSE 1 END ASC,
                     due_date ASC
                   LIMIT 10""",
                (household_id, today),
            ).fetchall()

            for r in rows:
                subtitle = (
                    f"Due {r['due_date']}" if r["due_date"]
                    else (r["priority"] or "normal").title()
                )
                items.append(DashboardItem(
                    id=r["id"],
                    title=r["title"],
                    domain="tasks",
                    subtitle=subtitle,
                    icon="mdi:checkbox-marked-circle-outline",
                    priority="high" if r["priority"] == "urgent" else "normal",
                    scope="household",
                    action="task_complete",
                ))

            # Counts via direct SQL — not derived from the display slice above
            active_count = conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE household_id=? AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id,),
            ).fetchone()[0]

            urgent_count = conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE household_id=? AND priority='urgent'
                   AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id,),
            ).fetchone()[0]

            overdue_count = conn.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE household_id=? AND due_date<?
                   AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id, today),
            ).fetchone()[0]

            if overdue_count:
                alerts.append(DashboardAlert(
                    type="overdue",
                    message=f"{overdue_count} task(s) are overdue",
                    severity="high",
                ))

            # Inbox accumulation alert (> 10 tasks sitting in inbox)
            inbox_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND status='inbox'",
                (household_id,),
            ).fetchone()[0]
            if inbox_count > 10:
                alerts.append(DashboardAlert(
                    type="inbox_overload",
                    message=f"{inbox_count} tasks in inbox — consider reviewing",
                    severity="low",
                ))

            # Routine summary — household routines done today
            try:
                r_total = conn.execute(
                    "SELECT COUNT(*) FROM routines WHERE household_id=? AND active=1 AND scope='household'",
                    (household_id,),
                ).fetchone()[0]
                r_done = conn.execute(
                    "SELECT COUNT(*) FROM routine_completions rc "
                    "JOIN routines r ON r.id=rc.routine_id "
                    "WHERE rc.household_id=? AND rc.completion_date=? AND r.scope='household'",
                    (household_id, today),
                ).fetchone()[0]
                if r_total > 0:
                    routine_summary = f"{r_done}/{r_total} routines done"
                    if r_done < r_total:
                        alerts.append(DashboardAlert(
                            type="routines_pending",
                            message=f"{r_total - r_done} routine(s) not done today",
                            severity="low",
                        ))
            except Exception:
                pass  # routines table may not exist in older DBs

        return items, alerts, routine_summary, active_count, urgent_count, overdue_count
