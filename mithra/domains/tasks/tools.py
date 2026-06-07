"""Tasks domain — classification helpers, ID generators, and core tools (T2+T3).

Pure helpers (no I/O):  classify_task_scope, classify_task_priority,
                         should_auto_promote, new_*_id, now_iso

Core tools (SQLite I/O): TaskCreateTool, TaskListTool, TaskCompleteTool,
                          TaskUpdateTool, TaskSnoozeTool, TaskDeleteTool,
                          TaskSuggestTool

All tools follow the LangChain BaseTool pattern used by TimeDomain.
Factory: build_tasks_tools(context) → list[ToolBundle]
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from mithra.domains.bundle import PermissionTier, ToolBundle, ToolMetadata
from mithra.domains.context import DomainContext
from mithra.domains.result import DomainEvent, ToolResult

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification constants
# ---------------------------------------------------------------------------

_HOUSEHOLD_KEYWORDS: frozenset[str] = frozenset({
    # People / relationships
    "household", "family", "both", "we", "us",
    "everyone", "kids", "children", "baby", "shared", "together",
    # Waste / recycling
    "trash", "recycling", "garbage", "bins",
    # Finances
    "rent", "mortgage", "utilities", "bills",
    # School / childcare
    "school", "pickup", "dropoff", "daycare",
    # Tradespeople
    "pest", "plumber", "contractor",
    # Outdoor
    "lawn", "yard", "garden",
    # Outdoor structures
    "roof", "gutter", "driveway", "garage", "fence", "gate",
    # HVAC / mechanical
    "ceiling", "fan", "hvac", "furnace", "water heater",
    # Kitchen / laundry appliances
    "dishwasher", "washer", "dryer", "fridge", "refrigerator",
})

_PERSONAL_RECURRING_KEYWORDS: frozenset[str] = frozenset({
    "gym", "workout", "run", "yoga", "meditation",
    "medication", "therapy", "doctor", "dentist",
    "commute", "work", "office", "standup",
})


# ---------------------------------------------------------------------------
# T2 — Pure classification helpers
# ---------------------------------------------------------------------------

def classify_task_scope(title: str, user_text: str = "") -> str:
    """Return 'household' if any household keyword appears, else 'personal'."""
    text = (title + " " + user_text).lower()
    for kw in _HOUSEHOLD_KEYWORDS:
        if kw in text:
            return "household"
    return "personal"


def classify_task_priority(
    title: str,
    user_text: str = "",
    due_date: Optional[str] = None,
) -> str:
    """Infer priority from title, user text, and deadline proximity."""
    text = (title + " " + user_text).lower()

    if any(w in text for w in (
        "urgent", "asap", "emergency", "immediately", "today", "tonight",
    )):
        return "urgent"

    if due_date:
        try:
            due = date.fromisoformat(due_date)
            days_until = (due - date.today()).days
            if days_until <= 7:
                return "urgent"
            if days_until <= 30:
                return "high"
        except ValueError:
            pass

    if any(w in text for w in (
        "someday", "eventually", "maybe", "nice to have",
        "would be nice", "one day",
    )):
        return "someday"

    return "normal"


def should_auto_promote(
    priority: str,
    due_date: Optional[str],
) -> Optional[str]:
    """Return new priority if auto-promotion rule applies, else None.

    Rules (from ADR 11.5):
      NORMAL with due_date ≤ 7 days → URGENT
      HIGH   with due_date ≤ 3 days → URGENT
    """
    if not due_date or priority == "urgent":
        return None
    try:
        due = date.fromisoformat(due_date)
        days_until = (due - date.today()).days
        if priority == "normal" and days_until <= 7:
            return "urgent"
        if priority == "high" and days_until <= 3:
            return "urgent"
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def new_task_id() -> str:
    return "tsk_" + uuid.uuid4().hex[:12]


def new_project_id() -> str:
    return "prj_" + uuid.uuid4().hex[:12]


def new_milestone_id() -> str:
    return "mst_" + uuid.uuid4().hex[:12]


def new_routine_id() -> str:
    return "rtn_" + uuid.uuid4().hex[:12]


def new_routine_completion_id() -> str:
    return "rc_" + uuid.uuid4().hex[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# T3 — Private helpers used by tool classes
# ---------------------------------------------------------------------------

def _is_iso_date(text: str) -> bool:
    """Return True if text is already YYYY-MM-DD."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", text.strip()))


_DAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

_SETTINGS = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}


def _parse_due_date(text: str) -> Optional[str]:
    """Parse natural-language or ISO date to YYYY-MM-DD. Returns None on failure.

    dateparser doesn't handle "next <day>" or "this <day>" phrases. When the
    first attempt fails and the text starts with "next"/"this" followed by a
    day name, strip the prefix and retry — PREFER_DATES_FROM=future keeps it
    in the future.
    """
    if not text:
        return None
    stripped = text.strip()
    if _is_iso_date(stripped):
        return stripped

    import dateparser

    def _try(s: str) -> Optional[str]:
        try:
            result = dateparser.parse(s, settings=_SETTINGS)
            if result:
                return result.date().isoformat()
        except Exception as exc:
            _logger.debug("_parse_due_date failed for %r: %s", s, exc)
        return None

    result = _try(stripped)
    if result:
        return result

    # Retry: strip "next"/"this" before a day name
    lower = stripped.lower()
    for prefix in ("next ", "this "):
        if lower.startswith(prefix):
            remainder = stripped[len(prefix):]
            if remainder.lower() in _DAY_NAMES:
                result = _try(remainder)
                if result:
                    return result

    return None


def _store_capture_state(
    db_path: str, user_id: str, task_id: str, step: str, context: dict
) -> None:
    """Persist multi-turn capture state in conversation_state table."""
    import json
    import sqlite3

    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO conversation_state
                   (user_id, last_task_id, capture_step, context_json, updated_at)
                   VALUES (?,?,?,?,?)""",
                (user_id, task_id, step, json.dumps(context), now),
            )
    except Exception as exc:
        _logger.warning("Could not store capture state: %s", exc)


def _get_capture_state(db_path: str, user_id: str) -> Optional[dict]:
    """Retrieve pending capture state for a user. Returns None if none."""
    import json
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_task_id, capture_step, context_json FROM conversation_state WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row:
            return {
                "task_id": row[0],
                "step": row[1],
                "context": json.loads(row[2] or "{}"),
            }
    except Exception as exc:
        _logger.warning("Could not get capture state: %s", exc)
    return None


def _clear_capture_state(db_path: str, user_id: str) -> None:
    """Clear pending capture state for a user."""
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM conversation_state WHERE user_id=?", (user_id,))
    except Exception as exc:
        _logger.warning("Could not clear capture state: %s", exc)


def _next_capture_question(step: str) -> str:
    """Return the clarifying question for the given multi-turn capture step."""
    questions = {
        "due_date": "When is this due? (or reply 'skip')",
        "priority": "How urgent is this? urgent / high / normal / someday (or 'skip')",
        "notes": "Any notes to add? (or 'skip')",
    }
    return questions.get(step, "")


def _find_project(conn, household_id: str, query: str) -> Optional[dict]:
    """Return best-matching active project for a title keyword query.

    When multiple projects match the LIKE pattern, the one with the most
    milestones wins — it is most likely the intended project rather than
    an empty project with a similar name.
    """
    row = conn.execute(
        """
        SELECT p.*, COUNT(m.id) AS milestone_count
        FROM projects p
        LEFT JOIN milestones m ON m.project_id = p.id
        WHERE p.household_id = ?
          AND p.title LIKE ?
          AND p.status != 'completed'
        GROUP BY p.id
        ORDER BY milestone_count DESC, p.created_at DESC
        LIMIT 1
        """,
        (household_id, f"%{query}%"),
    ).fetchone()
    return dict(row) if row else None


def _resolve_milestone(conn, project_id: str, milestone_query: str):
    """Find a milestone by title match, then ordinal, then next-pending fallback.

    Handles queries like 'phase 1', 'phase 2', 'first', 'second' where the
    milestone titles are descriptive ('Buy materials', 'Build frame') and the
    LIKE match would fail.
    """
    if milestone_query:
        row = conn.execute(
            "SELECT * FROM milestones WHERE project_id=? AND title LIKE ? LIMIT 1",
            (project_id, f"%{milestone_query}%"),
        ).fetchone()
        if row:
            return row

        _ORDINAL_MAP: dict[str, int] = {
            "1": 1, "first": 1, "one": 1,
            "2": 2, "second": 2, "two": 2,
            "3": 3, "third": 3, "three": 3,
            "4": 4, "fourth": 4, "four": 4,
            "5": 5, "fifth": 5, "five": 5,
        }
        query_lower = milestone_query.lower()
        num_match = re.search(r"\d+", query_lower)
        order: Optional[int] = int(num_match.group()) if num_match else None
        if order is None:
            for word, idx in _ORDINAL_MAP.items():
                if word in query_lower:
                    order = idx
                    break
        if order is not None:
            row = conn.execute(
                "SELECT * FROM milestones WHERE project_id=? AND order_index=? LIMIT 1",
                (project_id, order),
            ).fetchone()
            if row:
                return row

    return conn.execute(
        "SELECT * FROM milestones WHERE project_id=? AND status='pending' ORDER BY order_index ASC LIMIT 1",
        (project_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Tool 1 — task_create
# ---------------------------------------------------------------------------

class _TaskCreateArgs(BaseModel):
    title: str = Field(description="Task title exactly as the user described it.")
    due_date: str = Field(
        default="",
        description="Due date in natural language ('next Friday') or ISO (YYYY-MM-DD). Empty if not mentioned.",
    )
    notes: str = Field(default="", description="Optional notes or details for the task.")
    scope: str = Field(
        default="",
        description="'household' or 'personal'. Leave empty to auto-detect from title.",
    )
    priority: str = Field(
        default="",
        description="'urgent', 'high', 'normal', or 'someday'. Leave empty to auto-detect.",
    )
    project_id: str = Field(default="", description="Project ID (prj_...) to link. Leave empty if none.")


class TaskCreateTool(BaseTool):
    name: str = "task_create"
    description: str = (
        "Create a new task. Use when the user mentions something they need to do, "
        "wants to add a to-do, or says 'remind me to X'. "
        "Do NOT use for calendar events with a specific clock time — use calendar_create_event."
    )
    args_schema: Type[BaseModel] = _TaskCreateArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        title: str,
        due_date: str = "",
        notes: str = "",
        scope: str = "",
        priority: str = "",
        project_id: str = "",
    ) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        task_scope = scope or classify_task_scope(title)
        parsed_due: Optional[str] = _parse_due_date(due_date) if due_date else None
        task_priority = priority or classify_task_priority(title, due_date=parsed_due)
        task_id = new_task_id()
        now = now_iso()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.execute(
                    """INSERT INTO tasks
                       (id, household_id, user_id, title, notes, scope, priority, status,
                        due_date, project_id, source, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id, ctx.household_id, ctx.user_id,
                        title, notes or None, task_scope, task_priority,
                        "inbox", parsed_due, project_id or None,
                        "manual", now, now,
                    ),
                )
        except Exception as exc:
            _logger.error("task_create DB error", exc_info=True)
            return ToolResult.fail("Could not save task.", error=str(exc))

        _logger.info("task_create: %s scope=%s priority=%s", task_id, task_scope, task_priority)

        # Build summary per Section 12 response standards — no quotes, ✓ prefix
        detail_parts = []
        if parsed_due:
            detail_parts.append(f"due {parsed_due}")
        if task_priority not in ("normal", ""):
            detail_parts.append(task_priority)
        scope_badge = " 🏠" if task_scope == "household" else ""
        detail = " · ".join(detail_parts)
        summary = f"✓ {title}" + (f" — {detail}" if detail else "") + scope_badge

        # Determine initial capture step and write state for follow-up routing
        if not parsed_due:
            initial_step: Optional[str] = "due_date"
            summary += "\n\nAny deadline?"
        elif not priority:
            initial_step = "priority"
            summary += "\n\nPriority? (urgent / high / normal / someday)"
        else:
            initial_step = None  # all fields provided, no follow-up needed

        if initial_step:
            _store_capture_state(
                ctx.local_db_path,
                ctx.user_id,
                task_id,
                initial_step,
                {"title": title},
            )

        return ToolResult.ok(
            summary,
            data={
                "task_id": task_id,
                "scope": task_scope,
                "priority": task_priority,
                "due_date": parsed_due,
            },
            events=[
                DomainEvent(
                    event_type="task_created",
                    source_domain="tasks",
                    payload={
                        "task_id": task_id,
                        "title": title,
                        "priority": task_priority,
                        "scope": task_scope,
                    },
                    user_id=ctx.user_id,
                    household_id=ctx.household_id,
                    request_id="",
                )
            ],
        )


# ---------------------------------------------------------------------------
# Tool 2 — task_list
# ---------------------------------------------------------------------------

class _TaskListArgs(BaseModel):
    filter: str = Field(
        default="inbox",
        description=(
            "Which tasks to return. Options: "
            "'today' (due today), 'inbox' (status=inbox), "
            "'overdue' (past due, not done), 'urgent' (priority=urgent), "
            "'household' (scope=household), 'waiting' (status=waiting), "
            "'all' (all active tasks, capped at 20)."
        ),
    )


class TaskListTool(BaseTool):
    name: str = "task_list"
    description: str = (
        "List tasks by filter. Use when the user asks what tasks they have, "
        "what's due today, what's urgent, or wants to review their inbox."
    )
    args_schema: Type[BaseModel] = _TaskListArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, filter: str = "inbox") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        today = date.today().isoformat()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                if filter == "today":
                    # "today" = urgent + high priority + due today + overdue
                    # Snoozed tasks excluded — user asked to hide them
                    rows = conn.execute(
                        """SELECT * FROM tasks
                           WHERE household_id=? AND status NOT IN ('done','cancelled','snoozed')
                             AND (priority='urgent' OR priority='high' OR due_date<=?)
                           ORDER BY
                             CASE priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'normal' THEN 3 ELSE 4 END ASC,
                             due_date ASC NULLS LAST""",
                        (ctx.household_id, today),
                    ).fetchall()
                elif filter == "overdue":
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND due_date<? AND status NOT IN ('done','cancelled') ORDER BY due_date ASC",
                        (ctx.household_id, today),
                    ).fetchall()
                elif filter == "urgent":
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND priority='urgent' AND status NOT IN ('done','cancelled') ORDER BY due_date ASC",
                        (ctx.household_id,),
                    ).fetchall()
                elif filter == "household":
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND scope='household' AND status NOT IN ('done','cancelled') ORDER BY priority DESC",
                        (ctx.household_id,),
                    ).fetchall()
                elif filter == "waiting":
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND status='waiting' ORDER BY updated_at DESC",
                        (ctx.household_id,),
                    ).fetchall()
                elif filter == "all":
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND status NOT IN ('done','cancelled') ORDER BY priority DESC, due_date ASC LIMIT 20",
                        (ctx.household_id,),
                    ).fetchall()
                else:  # inbox
                    rows = conn.execute(
                        "SELECT * FROM tasks WHERE household_id=? AND status='inbox' ORDER BY priority DESC, created_at DESC",
                        (ctx.household_id,),
                    ).fetchall()
        except Exception as exc:
            _logger.error("task_list DB error", exc_info=True)
            return ToolResult.fail("Could not read tasks.", error=str(exc))

        tasks = [dict(r) for r in rows]

        if not tasks:
            _EMPTY_MESSAGES: dict[str, str] = {
                "today":     "You're all clear for today! 🎉",
                "inbox":     "Inbox is empty.",
                "overdue":   "Nothing overdue. ✓",
                "urgent":    "No urgent tasks.",
                "household": "No household tasks.",
                "done":      "No completed tasks yet.",
                "waiting":   "No waiting tasks.",
                "all":       "No tasks found.",
                "personal":  "No personal tasks.",
            }
            return ToolResult.ok(
                _EMPTY_MESSAGES.get(filter, "No tasks found."),
                data={"tasks": [], "count": 0},
            )

        lines = []
        for i, t in enumerate(tasks, 1):
            line = f"{i}. {t['title']}"
            if t.get("due_date"):
                line += f" — due {t['due_date']}"
            priority = t.get("priority", "normal")
            if priority == "urgent":
                line += " ⚠️"
            elif priority == "high":
                line += " 🔹"
            if t.get("scope") == "household":
                line += " 🏠"
            status = t.get("status", "")
            if status == "waiting":
                line += " ⏳"
            elif status == "snoozed":
                line += f" (snoozed until {t.get('snoozed_until', '')})"
            lines.append(line)

        _FILTER_LABELS = {
            "today": "Today",
            "inbox": "Inbox",
            "overdue": "Overdue",
            "urgent": "Urgent",
            "household": "Household",
            "waiting": "Waiting",
            "all": "All tasks",
            "done": "Completed",
            "personal": "Personal",
        }
        label = _FILTER_LABELS.get(filter, filter.title())
        count = len(tasks)
        count_str = "1 task" if count == 1 else f"{count} tasks"
        summary = f"{label} — {count_str}:\n" + "\n".join(lines)
        return ToolResult.ok(summary, data={"tasks": tasks, "count": count})


# ---------------------------------------------------------------------------
# Tool 3 — task_complete
# ---------------------------------------------------------------------------

class _TaskCompleteArgs(BaseModel):
    task_id: str = Field(default="", description="Task ID (tsk_...). Prefer this over title_match.")
    title_match: str = Field(
        default="",
        description="Partial title to match when task_id is unknown. Case-insensitive substring.",
    )


class TaskCompleteTool(BaseTool):
    name: str = "task_complete"
    description: str = (
        "Mark a task as done. Use when the user says they finished, completed, "
        "or checked off a task. Provide task_id from task_list, or title_match."
    )
    args_schema: Type[BaseModel] = _TaskCompleteArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, task_id: str = "", title_match: str = "") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        if not task_id and not title_match:
            return ToolResult.fail("Provide task_id or title_match to identify the task.")

        now = now_iso()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                if task_id:
                    row = conn.execute(
                        "SELECT id, title FROM tasks WHERE id=? AND household_id=?",
                        (task_id, ctx.household_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT id, title FROM tasks WHERE household_id=? AND LOWER(title) LIKE ? AND status NOT IN ('done','cancelled') LIMIT 1",
                        (ctx.household_id, f"%{title_match.lower()}%"),
                    ).fetchone()

                if not row:
                    return ToolResult.fail(f"Task not found: {task_id or title_match}")

                tid, title = row["id"], row["title"]
                conn.execute(
                    "UPDATE tasks SET status='done', completed_at=?, updated_at=? WHERE id=?",
                    (now, now, tid),
                )
        except Exception as exc:
            _logger.error("task_complete DB error", exc_info=True)
            return ToolResult.fail("Could not complete task.", error=str(exc))

        _logger.info("task_complete: %s title=%r", tid, title)
        return ToolResult.ok(
            f"✓ {title}",
            data={"task_id": tid, "title": title, "completed_at": now},
            events=[
                DomainEvent(
                    event_type="task_completed",
                    source_domain="tasks",
                    payload={"task_id": tid, "title": title},
                    user_id=ctx.user_id,
                    household_id=ctx.household_id,
                    request_id="",
                )
            ],
        )


# ---------------------------------------------------------------------------
# Tool 4 — task_update
# ---------------------------------------------------------------------------

class _TaskUpdateArgs(BaseModel):
    task_id: str = Field(
        default="",
        description="Task ID to update (tsk_...). Use task_list to find it. "
                    "Leave empty when responding to a capture follow-up question — "
                    "the last created task is used automatically.",
    )
    title: str = Field(default="", description="New title. Leave empty to keep current.")
    due_date: str = Field(
        default="",
        description="New due date in natural language or ISO. Leave empty to keep current.",
    )
    priority: str = Field(
        default="",
        description="New priority: urgent/high/normal/someday. Leave empty to keep current.",
    )
    status: str = Field(
        default="",
        description="New status: inbox/active/scheduled/waiting/snoozed/done/cancelled. Leave empty to keep current.",
    )
    notes: str = Field(default="", description="New notes. Leave empty to keep current.")


class TaskUpdateTool(BaseTool):
    name: str = "task_update"
    description: str = (
        "Update one or more fields on an existing task. Use when the user changes a deadline, "
        "priority, status, or notes on a task. Requires task_id from task_list."
    )
    args_schema: Type[BaseModel] = _TaskUpdateArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        task_id: str = "",
        title: str = "",
        due_date: str = "",
        priority: str = "",
        status: str = "",
        notes: str = "",
    ) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        # If no task_id, look up the active capture state for this user
        _capture: Optional[dict] = None
        if not task_id:
            _capture = _get_capture_state(ctx.local_db_path, ctx.user_id)
            if _capture:
                task_id = _capture["task_id"]
            else:
                return ToolResult.fail("No task ID provided. Use task_list to find the task ID.")

        updates: list[tuple[str, Any]] = []
        now = now_iso()

        if title:
            updates.append(("title", title))
        if due_date:
            parsed = _parse_due_date(due_date)
            if parsed:
                updates.append(("due_date", parsed))
        if priority:
            updates.append(("priority", priority))
        if status:
            updates.append(("status", status))
        if notes:
            updates.append(("notes", notes))

        if not updates:
            return ToolResult.fail("No fields to update. Specify at least one field.")

        set_clause = ", ".join(f"{col}=?" for col, _ in updates) + ", updated_at=?"
        values: list[Any] = [v for _, v in updates] + [now, task_id, ctx.household_id]

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT title FROM tasks WHERE id=? AND household_id=?",
                    (task_id, ctx.household_id),
                ).fetchone()
                if not row:
                    return ToolResult.fail(f"Task not found: {task_id}")

                old_title = row["title"]
                cur = conn.execute(
                    f"UPDATE tasks SET {set_clause} WHERE id=? AND household_id=?",
                    values,
                )
                if cur.rowcount == 0:
                    return ToolResult.fail(f"Task not found: {task_id}")
        except Exception as exc:
            _logger.error("task_update DB error", exc_info=True)
            return ToolResult.fail("Could not update task.", error=str(exc))

        _logger.info("task_update: %s fields=%s", task_id, [c for c, _ in updates])
        summary = f"✓ {old_title} — updated"

        # Advance capture step if this update came from a follow-up flow
        if _capture:
            _STEP_ORDER = {"due_date": "priority", "priority": "notes", "notes": "done"}
            next_step = _STEP_ORDER.get(_capture["step"], "done")
            if next_step == "done":
                _clear_capture_state(ctx.local_db_path, ctx.user_id)
            else:
                _store_capture_state(
                    ctx.local_db_path,
                    ctx.user_id,
                    task_id,
                    next_step,
                    _capture["context"],
                )
                question = _next_capture_question(next_step)
                if question:
                    summary += f"\n\n{question}"

        return ToolResult.ok(
            summary,
            data={"task_id": task_id, "updated_fields": dict(updates)},
        )


# ---------------------------------------------------------------------------
# Tool 5 — task_snooze
# ---------------------------------------------------------------------------

class _TaskSnoozeArgs(BaseModel):
    task_id: str = Field(description="Task ID to snooze (tsk_...).")
    until: str = Field(
        description="When to resurface: 'tomorrow', 'next week', 'in 3 days', or a date string."
    )


class TaskSnoozeTool(BaseTool):
    name: str = "task_snooze"
    description: str = (
        "Snooze a task until a future date. Use when the user says 'remind me later', "
        "'not now', 'snooze', or gives a future date to revisit the task."
    )
    args_schema: Type[BaseModel] = _TaskSnoozeArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, task_id: str, until: str) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        snooze_date = _parse_due_date(until)
        if not snooze_date:
            return ToolResult.fail(
                f"Could not parse date from '{until}'.",
                error_code="UNPARSEABLE_DATE",
            )

        now = now_iso()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT title FROM tasks WHERE id=? AND household_id=?",
                    (task_id, ctx.household_id),
                ).fetchone()
                if not row:
                    return ToolResult.fail(f"Task not found: {task_id}")

                title = row["title"]
                conn.execute(
                    "UPDATE tasks SET status='snoozed', snoozed_until=?, updated_at=? WHERE id=? AND household_id=?",
                    (snooze_date, now, task_id, ctx.household_id),
                )
        except Exception as exc:
            _logger.error("task_snooze DB error", exc_info=True)
            return ToolResult.fail("Could not snooze task.", error=str(exc))

        _logger.info("task_snooze: %s until=%s", task_id, snooze_date)
        return ToolResult.ok(
            f"✓ {title} — snoozed until {snooze_date}",
            data={"task_id": task_id, "snoozed_until": snooze_date},
        )


# ---------------------------------------------------------------------------
# Tool 6 — task_delete  (DESTRUCTIVE — always requires confirmation)
# ---------------------------------------------------------------------------

class _TaskDeleteArgs(BaseModel):
    task_id: str = Field(description="Task ID to permanently delete (tsk_...).")
    title_hint: str = Field(
        default="",
        description="Human-readable task title for the confirmation prompt (e.g. 'Fix ceiling fan'). "
                    "Fill from task_list results when available.",
    )


class TaskDeleteTool(BaseTool):
    name: str = "task_delete"
    description: str = (
        "Permanently delete a task. Use ONLY when the user explicitly wants to remove "
        "a task (not just complete it). Always requires user confirmation."
    )
    args_schema: Type[BaseModel] = _TaskDeleteArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, task_id: str, title_hint: str = "") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row

                row = None
                if task_id:
                    row = conn.execute(
                        "SELECT id, title FROM tasks WHERE id=? AND household_id=? AND status != 'cancelled'",
                        (task_id, ctx.household_id),
                    ).fetchone()

                if not row and title_hint:
                    row = conn.execute(
                        "SELECT id, title FROM tasks "
                        "WHERE household_id=? AND title LIKE ? AND status != 'cancelled' LIMIT 1",
                        (ctx.household_id, f"%{title_hint}%"),
                    ).fetchone()

                if not row:
                    query = task_id or title_hint or "?"
                    return ToolResult.fail(f"No task found matching '{query}'.")

                found_id = row["id"]
                found_title = row["title"]

                # Soft delete: status=cancelled preserves history per ADR section 11.4.
                # CANCELLED = user decided not to do it; distinct from DONE.
                conn.execute(
                    "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?",
                    (now_iso(), found_id),
                )
        except Exception as exc:
            _logger.error("task_delete DB error", exc_info=True)
            return ToolResult.fail("Could not delete task.", error=str(exc))

        _logger.info("task_delete: %s title=%r", found_id, found_title)
        return ToolResult.ok(
            f"✓ {found_title} — removed",
            data={"task_id": found_id, "title": found_title},
        )


# ---------------------------------------------------------------------------
# Tool 7 — task_suggest
# ---------------------------------------------------------------------------

class _TaskSuggestArgs(BaseModel):
    context_hint: str = Field(
        default="",
        description="Optional hint about what the user is currently working on or their energy level.",
    )


class TaskSuggestTool(BaseTool):
    name: str = "task_suggest"
    description: str = (
        "Suggest the top 3 most important tasks to work on right now. Use when the user asks "
        "'what should I do?', 'what's next?', or wants a nudge on what to prioritise."
    )
    args_schema: Type[BaseModel] = _TaskSuggestArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, context_hint: str = "") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        today = date.today().isoformat()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT * FROM tasks
                       WHERE household_id=? AND user_id=?
                         AND status NOT IN ('done','cancelled','snoozed')
                       ORDER BY
                         CASE priority
                           WHEN 'urgent'  THEN 0
                           WHEN 'high'    THEN 1
                           WHEN 'normal'  THEN 2
                           ELSE 3
                         END ASC,
                         CASE WHEN due_date IS NOT NULL AND due_date<=? THEN 0 ELSE 1 END ASC,
                         due_date ASC
                       LIMIT 3""",
                    (ctx.household_id, ctx.user_id, today),
                ).fetchall()
        except Exception as exc:
            _logger.error("task_suggest DB error", exc_info=True)
            return ToolResult.fail("Could not retrieve task suggestions.", error=str(exc))

        tasks = [dict(r) for r in rows]

        if not tasks:
            return ToolResult.ok(
                "No open tasks — you're all caught up!",
                data={"suggestions": []},
            )

        lines = []
        for i, t in enumerate(tasks, 1):
            line = f"{i}. {t['title']}"
            if t.get("due_date"):
                line += f" — due {t['due_date']}"
            priority = t.get("priority", "normal")
            if priority == "urgent":
                line += " ⚠️"
            elif priority == "high":
                line += " 🔹"
            if t.get("scope") == "household":
                line += " 🏠"
            lines.append(line)

        summary = "Top tasks to focus on:\n" + "\n".join(lines)
        return ToolResult.ok(summary, data={"suggestions": tasks})


# ---------------------------------------------------------------------------
# Tool 8 — project_create
# ---------------------------------------------------------------------------

class _ProjectCreateArgs(BaseModel):
    title: str = Field(description="Project title")
    description: str = Field(default="", description="Optional description")
    target_date: str = Field(default="", description="Target completion date")
    scope: str = Field(default="", description="personal|household")
    milestones: list[str] = Field(
        default=[],
        description="Optional ordered milestone titles. e.g. ['Buy materials', 'Build frame', 'Paint']",
    )


class ProjectCreateTool(BaseTool):
    name: str = "project_create"
    description: str = (
        "Create a new project with optional milestones. "
        "Use for multi-step work with phases. "
        "Examples: 'backyard deck by July 4', 'woodworking bench with milestones'."
    )
    args_schema: Type[BaseModel] = _ProjectCreateArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        title: str,
        description: str = "",
        target_date: str = "",
        scope: str = "",
        milestones: list[str] | None = None,
    ) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        parsed_date = _parse_due_date(target_date) if target_date else None
        final_scope = scope or classify_task_scope(title)
        project_id = new_project_id()
        now = now_iso()
        milestones = milestones or []

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.execute(
                    """INSERT INTO projects
                       (id, household_id, user_id, title, description, scope, status, target_date, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        project_id, ctx.household_id, ctx.user_id,
                        title, description or None, final_scope,
                        "active", parsed_date, now, now,
                    ),
                )
                for i, ms_title in enumerate(milestones, 1):
                    conn.execute(
                        """INSERT INTO milestones
                           (id, project_id, household_id, title, order_index, status, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (new_milestone_id(), project_id, ctx.household_id, ms_title, i, "pending", now, now),
                    )
        except Exception as exc:
            _logger.error("project_create DB error", exc_info=True)
            return ToolResult.fail("Could not create project.", error=str(exc))

        _logger.info("project_create: %s scope=%s milestones=%d", project_id, final_scope, len(milestones))

        scope_badge = " 🏠" if final_scope == "household" else ""
        detail = f"target {parsed_date}" if parsed_date else ""
        summary = f"✓ {title}" + (f" — {detail}" if detail else "") + scope_badge

        if milestones:
            ms_lines = "\n".join(f"  {i}. {m}" for i, m in enumerate(milestones, 1))
            summary += f"\n{len(milestones)} milestones:\n" + ms_lines
        else:
            summary += (
                "\n\nAdd milestones? List them or say 'skip'.\n"
                "e.g. 'Buy materials, Build frame, Paint'"
            )
            _store_capture_state(ctx.local_db_path, ctx.user_id, project_id, "milestone", {})

        return ToolResult.ok(
            summary,
            data={
                "project_id": project_id,
                "milestone_count": len(milestones),
                "scope": final_scope,
                "target_date": parsed_date,
            },
        )


# ---------------------------------------------------------------------------
# Tool 9 — project_update
# ---------------------------------------------------------------------------

class _ProjectUpdateArgs(BaseModel):
    project_query: str = Field(description="Project title or keyword to search for")
    title: str = Field(default="", description="New project title")
    description: str = Field(default="", description="New description")
    target_date: str = Field(default="", description="New target completion date")
    status: str = Field(default="", description="active|completed|on_hold")


class ProjectUpdateTool(BaseTool):
    name: str = "project_update"
    description: str = (
        "Update a project's title, description, target date, or status. "
        "Examples: 'mark backyard deck on hold', 'update deck deadline to August'."
    )
    args_schema: Type[BaseModel] = _ProjectUpdateArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        project_query: str,
        title: str = "",
        description: str = "",
        target_date: str = "",
        status: str = "",
    ) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                project = _find_project(conn, ctx.household_id, project_query)
                if not project:
                    return ToolResult.fail(f"No project found matching '{project_query}'")

                project_id = project["id"]
                now = now_iso()

                updates: dict[str, Any] = {"updated_at": now}
                if title:
                    updates["title"] = title
                if description:
                    updates["description"] = description
                if target_date:
                    parsed = _parse_due_date(target_date)
                    if parsed:
                        updates["target_date"] = parsed
                if status in ("active", "completed", "on_hold"):
                    updates["status"] = status

                if len(updates) == 1:  # only updated_at
                    return ToolResult.fail(
                        "No valid fields to update. Specify title, description, target_date, or status."
                    )

                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE projects SET {set_clause} WHERE id=?",
                    (*updates.values(), project_id),
                )
        except Exception as exc:
            _logger.error("project_update DB error", exc_info=True)
            return ToolResult.fail("Could not update project.", error=str(exc))

        old_title = project["title"]
        if status == "completed":
            summary = f"✓ {old_title} — project complete! 🎉"
        else:
            summary = f"✓ {old_title} — updated"

        _logger.info("project_update: %s", project_id)
        return ToolResult.ok(summary, data={"project_id": project_id})


# ---------------------------------------------------------------------------
# Tool 10 — milestone_complete
# ---------------------------------------------------------------------------

class _MilestoneCompleteArgs(BaseModel):
    project_query: str = Field(description="Project title or keyword")
    milestone_query: str = Field(
        default="",
        description="Milestone title or keyword. If empty, completes the next pending milestone.",
    )


class MilestoneCompleteTool(BaseTool):
    name: str = "milestone_complete"
    description: str = (
        "Mark a project milestone as done. "
        "If no milestone specified, completes the next pending one. "
        "Examples: 'complete phase 1 of backyard deck', 'next milestone on deck'."
    )
    args_schema: Type[BaseModel] = _MilestoneCompleteArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, project_query: str, milestone_query: str = "") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row

                project = _find_project(conn, ctx.household_id, project_query)
                if not project:
                    return ToolResult.fail(f"No project found matching '{project_query}'")

                project_id = project["id"]

                ms_row = _resolve_milestone(conn, project_id, milestone_query)

                if not ms_row:
                    return ToolResult.fail(f"No pending milestones for '{project['title']}'")

                ms = dict(ms_row)
                now = now_iso()

                conn.execute(
                    "UPDATE milestones SET status='done', updated_at=? WHERE id=?",
                    (now, ms["id"]),
                )

                total = conn.execute(
                    "SELECT COUNT(*) FROM milestones WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                done_count = conn.execute(
                    "SELECT COUNT(*) FROM milestones WHERE project_id=? AND status='done'", (project_id,)
                ).fetchone()[0]

                auto_complete = done_count == total and total > 0
                if auto_complete:
                    conn.execute(
                        "UPDATE projects SET status='completed', updated_at=? WHERE id=?",
                        (now, project_id),
                    )
        except Exception as exc:
            _logger.error("milestone_complete DB error", exc_info=True)
            return ToolResult.fail("Could not complete milestone.", error=str(exc))

        pct = int(done_count / total * 100) if total else 0
        summary = (
            f"✓ {ms['title']}\n"
            f"{project['title']}: {done_count}/{total} milestones ({pct}%)"
        )
        if auto_complete:
            summary += "\n🎉 Project complete!"

        _logger.info(
            "milestone_complete: %s project=%s %d/%d", ms["id"], project_id, done_count, total
        )
        return ToolResult.ok(
            summary,
            data={
                "milestone_id": ms["id"],
                "project_id": project_id,
                "done": done_count,
                "total": total,
                "pct": pct,
                "auto_completed": auto_complete,
            },
        )


# ---------------------------------------------------------------------------
# Tool 11 — project_status
# ---------------------------------------------------------------------------

class _ProjectStatusArgs(BaseModel):
    project_query: str = Field(description="Project title or keyword")


class ProjectStatusTool(BaseTool):
    name: str = "project_status"
    description: str = (
        "Show project progress with all milestones. "
        "Use for 'how is [project] going' queries."
    )
    args_schema: Type[BaseModel] = _ProjectStatusArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, project_query: str) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row

                project = _find_project(conn, ctx.household_id, project_query)
                if not project:
                    return ToolResult.fail(f"No project found matching '{project_query}'")

                project_id = project["id"]

                ms_rows = conn.execute(
                    "SELECT * FROM milestones WHERE project_id=? ORDER BY order_index ASC",
                    (project_id,),
                ).fetchall()
                milestones = [dict(r) for r in ms_rows]
        except Exception as exc:
            _logger.error("project_status DB error", exc_info=True)
            return ToolResult.fail("Could not retrieve project status.", error=str(exc))

        total = len(milestones)
        done_count = sum(1 for m in milestones if m["status"] == "done")
        pct = int(done_count / total * 100) if total else 0

        lines = [f"📋 {project['title']}"]
        meta = []
        if project.get("target_date"):
            meta.append(f"Target: {project['target_date']}")
        if project.get("scope") == "household":
            meta.append("🏠 Household")
        if meta:
            lines.append(" · ".join(meta))
        lines.append(f"Progress: {done_count}/{total} milestones ({pct}%)")
        lines.append("")

        _ICONS = {"done": "✓", "active": "▶", "pending": "○"}
        for m in milestones:
            icon = _ICONS.get(m["status"], "○")
            lines.append(f"{icon} {m['order_index']}. {m['title']}")

        if not milestones:
            lines.append("No milestones yet.")

        summary = "\n".join(lines)
        _logger.info("project_status: %s %d/%d", project_id, done_count, total)
        return ToolResult.ok(
            summary,
            data={
                "project_id": project_id,
                "title": project["title"],
                "status": project["status"],
                "done": done_count,
                "total": total,
                "pct": pct,
                "milestones": milestones,
            },
        )


# ---------------------------------------------------------------------------
# Tool 12 — routine_create
# ---------------------------------------------------------------------------

class _RoutineCreateArgs(BaseModel):
    title: str = Field(description="Routine title")
    scope: str = Field(
        default="",
        description="personal|household. Defaults to household for baby/family routines.",
    )
    reset_time: str = Field(
        default="00:00",
        description=(
            "HH:MM when routine resets for a new day. Default 00:00 (midnight). "
            "Use 21:00 for evening routines that reset at 9pm."
        ),
    )


class RoutineCreateTool(BaseTool):
    name: str = "routine_create"
    description: str = (
        "Create a daily recurring routine checklist item. "
        "Use for things that repeat every day and need to be checked off. "
        "Examples: 'mix baby formula', 'warm water for baby', 'soak lentils', "
        "'evening wind-down checklist'. "
        "Different from tasks (one-time) or calendar events (specific time)."
    )
    args_schema: Type[BaseModel] = _RoutineCreateArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, title: str, scope: str = "", reset_time: str = "00:00") -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        if not re.match(r"^\d{2}:\d{2}$", reset_time):
            reset_time = "00:00"

        final_scope = scope if scope in ("household", "personal") else classify_task_scope(title)
        routine_id = new_routine_id()
        now = now_iso()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.execute(
                    """INSERT INTO routines
                       (id, household_id, title, scope, reset_time, active, created_at)
                       VALUES (?,?,?,?,?,1,?)""",
                    (routine_id, ctx.household_id, title, final_scope, reset_time, now),
                )
        except Exception as exc:
            _logger.error("routine_create DB error", exc_info=True)
            return ToolResult.fail("Could not create routine.", error=str(exc))

        scope_badge = " 🏠" if final_scope == "household" else ""
        reset_label = f"resets at {reset_time}" if reset_time != "00:00" else "resets daily at midnight"
        summary = f"✓ {title} — daily routine added{scope_badge}\n{reset_label}"

        _logger.info("routine_create: %s title=%r scope=%s", routine_id, title, final_scope)
        return ToolResult.ok(
            summary,
            data={
                "routine_id": routine_id,
                "title": title,
                "scope": final_scope,
                "reset_time": reset_time,
            },
            events=[
                DomainEvent(
                    event_type="routine_created",
                    source_domain="tasks",
                    payload={"routine_id": routine_id, "title": title, "scope": final_scope},
                    user_id=ctx.user_id,
                    household_id=ctx.household_id,
                    request_id="",
                )
            ],
        )


# ---------------------------------------------------------------------------
# Tool 13 — routine_complete
# ---------------------------------------------------------------------------

class _RoutineCompleteArgs(BaseModel):
    routine_query: str = Field(description="Routine title or keyword")


class RoutineCompleteTool(BaseTool):
    name: str = "routine_complete"
    description: str = (
        "Mark a routine as done for today. Either household member can mark done. "
        "Examples: 'baby formula done', 'mark warm water done', 'done with soak lentils'."
    )
    args_schema: Type[BaseModel] = _RoutineCompleteArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, routine_query: str) -> ToolResult:
        import sqlite3
        import pytz

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        tz = pytz.timezone(ctx.household_timezone or "UTC")
        today = datetime.now(tz).strftime("%Y-%m-%d")
        now = now_iso()

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row

                routine = conn.execute(
                    "SELECT * FROM routines WHERE household_id=? AND title LIKE ? AND active=1 LIMIT 1",
                    (ctx.household_id, f"%{routine_query}%"),
                ).fetchone()

                if not routine:
                    return ToolResult.fail(
                        f"No routine found matching '{routine_query}'. "
                        "Say 'show routines' to see your list."
                    )

                routine = dict(routine)
                routine_id = routine["id"]

                try:
                    conn.execute(
                        """INSERT INTO routine_completions
                           (id, routine_id, household_id, completed_by, completion_date, completed_at)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            new_routine_completion_id(),
                            routine_id,
                            ctx.household_id,
                            ctx.user_id,
                            today,
                            now,
                        ),
                    )
                    summary = f"✓ {routine['title']} — done today"
                except sqlite3.IntegrityError:
                    prev = conn.execute(
                        "SELECT completed_by FROM routine_completions "
                        "WHERE routine_id=? AND completion_date=? LIMIT 1",
                        (routine_id, today),
                    ).fetchone()
                    if prev and prev[0] != ctx.user_id:
                        summary = f"✓ {routine['title']} — already done today (by another member)"
                    else:
                        summary = f"✓ {routine['title']} — already marked done today"

        except Exception as exc:
            _logger.error("routine_complete DB error", exc_info=True)
            return ToolResult.fail("Could not mark routine done.", error=str(exc))

        _logger.info(
            "routine_complete: %s today=%s user=%s", routine_id, today, ctx.user_id
        )
        return ToolResult.ok(
            summary,
            data={
                "routine_id": routine_id,
                "title": routine["title"],
                "completion_date": today,
            },
        )


# ---------------------------------------------------------------------------
# Tool 14 — routine_list
# ---------------------------------------------------------------------------

class _RoutineListArgs(BaseModel):
    scope_filter: str = Field(
        default="all",
        description="all|household|personal",
    )


class RoutineListTool(BaseTool):
    name: str = "routine_list"
    description: str = (
        "Show today's routines with done/not-done status. "
        "Use for 'what routines are done today', 'show my routines', "
        "'what's left to do today'."
    )
    args_schema: Type[BaseModel] = _RoutineListArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, scope_filter: str = "all") -> ToolResult:
        import sqlite3
        import pytz

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

        tz = pytz.timezone(ctx.household_timezone or "UTC")
        today = datetime.now(tz).strftime("%Y-%m-%d")

        scope_clause = ""
        if scope_filter == "household":
            scope_clause = "AND r.scope='household'"
        elif scope_filter == "personal":
            scope_clause = "AND r.scope='personal'"

        try:
            with sqlite3.connect(ctx.local_db_path) as conn:
                conn.row_factory = sqlite3.Row
                routines = conn.execute(
                    f"""SELECT r.*,
                               rc.completed_by,
                               rc.completed_at
                        FROM routines r
                        LEFT JOIN routine_completions rc
                            ON rc.routine_id = r.id
                            AND rc.completion_date = ?
                            AND rc.household_id = r.household_id
                        WHERE r.household_id = ?
                          AND r.active = 1
                          {scope_clause}
                        ORDER BY r.created_at ASC""",
                    (today, ctx.household_id),
                ).fetchall()
        except Exception as exc:
            _logger.error("routine_list DB error", exc_info=True)
            return ToolResult.fail("Could not list routines.", error=str(exc))

        if not routines:
            return ToolResult.ok(
                "No routines set up yet. Add one: 'add routine mix baby formula'",
                data={"routines": [], "count": 0, "done_count": 0, "date": today},
            )

        done: list[dict] = []
        pending: list[dict] = []
        for r in routines:
            rd = dict(r)
            if rd.get("completed_by") is not None:
                done.append(rd)
            else:
                pending.append(rd)

        lines: list[str] = []
        for r in pending:
            badge = " 🏠" if r["scope"] == "household" else ""
            lines.append(f"○ {r['title']}{badge}")
        for r in done:
            badge = " 🏠" if r["scope"] == "household" else ""
            lines.append(f"✓ {r['title']}{badge}")

        total = len(routines)
        done_count = len(done)
        summary = f"Today's routines ({done_count}/{total} done):\n" + "\n".join(lines)

        _logger.info(
            "routine_list: household=%s date=%s %d/%d done",
            ctx.household_id, today, done_count, total,
        )
        return ToolResult.ok(
            summary,
            data={
                "routines": [dict(r) for r in routines],
                "count": total,
                "done_count": done_count,
                "date": today,
            },
        )


# ---------------------------------------------------------------------------
# Factory — build all 14 ToolBundles for a given DomainContext
# ---------------------------------------------------------------------------

def build_tasks_tools(context: DomainContext) -> list[ToolBundle]:
    """Construct all 14 Tasks domain tools bound to the given context."""
    return [
        ToolBundle(
            tool=TaskCreateTool(context=context),
            metadata=ToolMetadata(
                name="task_create",
                domain_id="tasks",
                permission=PermissionTier.READ,   # low-friction capture: no confirmation
                is_reversible=True,
                autonomy_level=3,
                event_emitted="task_created",
            ),
        ),
        ToolBundle(
            tool=TaskListTool(context=context),
            metadata=ToolMetadata(
                name="task_list",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=TaskCompleteTool(context=context),
            metadata=ToolMetadata(
                name="task_complete",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=True,
                autonomy_level=2,
                event_emitted="task_completed",
            ),
        ),
        ToolBundle(
            tool=TaskUpdateTool(context=context),
            metadata=ToolMetadata(
                name="task_update",
                domain_id="tasks",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=2,
            ),
        ),
        ToolBundle(
            tool=TaskSnoozeTool(context=context),
            metadata=ToolMetadata(
                name="task_snooze",
                domain_id="tasks",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=2,
            ),
        ),
        ToolBundle(
            tool=TaskDeleteTool(context=context),
            metadata=ToolMetadata(
                name="task_delete",
                domain_id="tasks",
                permission=PermissionTier.DESTRUCTIVE,
                is_reversible=False,
                autonomy_level=1,
            ),
        ),
        ToolBundle(
            tool=TaskSuggestTool(context=context),
            metadata=ToolMetadata(
                name="task_suggest",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=ProjectCreateTool(context=context),
            metadata=ToolMetadata(
                name="project_create",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=True,
                autonomy_level=1,
                event_emitted="project_created",
            ),
        ),
        ToolBundle(
            tool=ProjectUpdateTool(context=context),
            metadata=ToolMetadata(
                name="project_update",
                domain_id="tasks",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=1,
            ),
        ),
        ToolBundle(
            tool=MilestoneCompleteTool(context=context),
            metadata=ToolMetadata(
                name="milestone_complete",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=2,
                event_emitted="milestone_completed",
            ),
        ),
        ToolBundle(
            tool=ProjectStatusTool(context=context),
            metadata=ToolMetadata(
                name="project_status",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=RoutineCreateTool(context=context),
            metadata=ToolMetadata(
                name="routine_create",
                domain_id="tasks",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=1,
                event_emitted="routine_created",
            ),
        ),
        ToolBundle(
            tool=RoutineCompleteTool(context=context),
            metadata=ToolMetadata(
                name="routine_complete",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=2,
                event_emitted="routine_completed",
            ),
        ),
        ToolBundle(
            tool=RoutineListTool(context=context),
            metadata=ToolMetadata(
                name="routine_list",
                domain_id="tasks",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
    ]
