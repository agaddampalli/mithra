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
    "household", "family", "both", "we", "us",
    "everyone", "kids", "children", "baby", "shared",
    "trash", "recycling", "garbage", "bins",
    "rent", "mortgage", "utilities", "bills",
    "school", "pickup", "dropoff", "daycare",
    "pest", "plumber", "contractor", "lawn",
    "yard", "garden", "together",
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
        parts = [f'Task added: "{title}"']
        if parsed_due:
            parts.append(f"due {parsed_due}")
        if task_priority != "normal":
            parts.append(f"priority: {task_priority}")
        if task_scope == "household":
            parts.append("(household)")

        return ToolResult.ok(
            " — ".join(parts) if len(parts) > 1 else parts[0],
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
                    # "today" = tasks due today + all urgent + all overdue
                    rows = conn.execute(
                        """SELECT * FROM tasks
                           WHERE household_id=? AND status NOT IN ('done','cancelled')
                             AND (due_date=? OR priority='urgent'
                                  OR (due_date IS NOT NULL AND due_date<?))
                           ORDER BY
                             CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END ASC,
                             due_date ASC""",
                        (ctx.household_id, today, today),
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
            return ToolResult.ok(
                f"No tasks for filter '{filter}'.",
                data={"tasks": [], "count": 0},
            )

        lines = []
        for t in tasks:
            line = f"[{t['id']}] {t['title']}"
            if t.get("due_date"):
                line += f" (due {t['due_date']})"
            if t.get("priority") and t["priority"] != "normal":
                line += f" [{t['priority']}]"
            lines.append(line)

        summary = f"{len(tasks)} task(s) — {filter}:\n" + "\n".join(lines)
        return ToolResult.ok(summary, data={"tasks": tasks, "count": len(tasks)})


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
            f'Done: "{title}"',
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
    task_id: str = Field(description="Task ID to update (tsk_...). Use task_list to find it.")
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
        task_id: str,
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
        changed = ", ".join(f"{col}→{val}" for col, val in updates)
        return ToolResult.ok(
            f'Updated "{old_title}": {changed}',
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
            f'Snoozed "{title}" until {snooze_date}.',
            data={"task_id": task_id, "snoozed_until": snooze_date},
        )


# ---------------------------------------------------------------------------
# Tool 6 — task_delete  (DESTRUCTIVE — always requires confirmation)
# ---------------------------------------------------------------------------

class _TaskDeleteArgs(BaseModel):
    task_id: str = Field(description="Task ID to permanently delete (tsk_...).")


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

    def _run(self, task_id: str) -> ToolResult:
        import sqlite3

        ctx = self._ctx
        if not ctx.local_db_path:
            return ToolResult.fail("Tasks storage not available.", error_code="NO_DB_PATH")

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
                    "DELETE FROM tasks WHERE id=? AND household_id=?",
                    (task_id, ctx.household_id),
                )
        except Exception as exc:
            _logger.error("task_delete DB error", exc_info=True)
            return ToolResult.fail("Could not delete task.", error=str(exc))

        _logger.info("task_delete: %s title=%r", task_id, title)
        return ToolResult.ok(
            f'Deleted task: "{title}".',
            data={"task_id": task_id, "title": title},
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
                line += f" (due {t['due_date']})"
            if t.get("priority") and t["priority"] != "normal":
                line += f" [{t['priority']}]"
            lines.append(line)

        summary = "Top tasks to focus on:\n" + "\n".join(lines)
        return ToolResult.ok(summary, data={"suggestions": tasks})


# ---------------------------------------------------------------------------
# Factory — build all 7 ToolBundles for a given DomainContext
# ---------------------------------------------------------------------------

def build_tasks_tools(context: DomainContext) -> list[ToolBundle]:
    """Construct all 7 Tasks domain tools bound to the given context."""
    return [
        ToolBundle(
            tool=TaskCreateTool(context=context),
            metadata=ToolMetadata(
                name="task_create",
                domain_id="tasks",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=2,
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
                permission=PermissionTier.WRITE,
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
    ]
