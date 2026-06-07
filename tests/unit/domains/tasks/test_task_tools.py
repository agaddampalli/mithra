"""Unit tests for mithra/domains/tasks/tools.py — T3 tool classes."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mithra.domains.context import DomainContext
from mithra.domains.tasks.tools import (
    MilestoneCompleteTool,
    ProjectCreateTool,
    ProjectStatusTool,
    ProjectUpdateTool,
    RoutineCompleteTool,
    RoutineCreateTool,
    RoutineListTool,
    TaskCompleteTool,
    TaskCreateTool,
    TaskDeleteTool,
    TaskListTool,
    TaskSnoozeTool,
    TaskSuggestTool,
    TaskUpdateTool,
    _is_iso_date,
    _parse_due_date,
    build_tasks_tools,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Fresh SQLite DB with tasks tables created by LocalStorageAdapter."""
    from mithra.storage.local import LocalStorageAdapter
    adapter = LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
    return str(tmp_path / "test.db")


@pytest.fixture()
def ctx(db):
    """DomainContext wired to the temp DB."""
    return DomainContext(
        user_id="usr_test",
        household_id="hh_test",
        household_timezone="UTC",
        oauth_manager=None,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_test",
        channel="telegram",
        local_db_path=db,
    )


@pytest.fixture()
def create_tool(ctx):
    return TaskCreateTool(context=ctx)


@pytest.fixture()
def list_tool(ctx):
    return TaskListTool(context=ctx)


@pytest.fixture()
def complete_tool(ctx):
    return TaskCompleteTool(context=ctx)


@pytest.fixture()
def update_tool(ctx):
    return TaskUpdateTool(context=ctx)


@pytest.fixture()
def snooze_tool(ctx):
    return TaskSnoozeTool(context=ctx)


@pytest.fixture()
def delete_tool(ctx):
    return TaskDeleteTool(context=ctx)


@pytest.fixture()
def suggest_tool(ctx):
    return TaskSuggestTool(context=ctx)


def _seed_task(db_path, task_id="tsk_aabbccdd1234", title="Fix fan",
               priority="normal", status="inbox", due_date=None,
               scope="personal", household_id="hh_test", user_id="usr_test"):
    """Insert a task row directly for test setup."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, household_id, user_id, title, scope, priority, status,
                due_date, source, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (task_id, household_id, user_id, title, scope, priority, status,
             due_date, "manual", now, now),
        )


# ---------------------------------------------------------------------------
# _is_iso_date helper
# ---------------------------------------------------------------------------

class TestIsIsoDate:
    def test_valid(self):
        assert _is_iso_date("2026-06-15") is True

    def test_invalid_format(self):
        assert _is_iso_date("next Friday") is False

    def test_partial(self):
        assert _is_iso_date("2026-06") is False

    def test_empty(self):
        assert _is_iso_date("") is False


# ---------------------------------------------------------------------------
# _parse_due_date helper
# ---------------------------------------------------------------------------

class TestParseDueDate:
    def test_iso_passthrough(self):
        assert _parse_due_date("2026-06-15") == "2026-06-15"

    def test_empty_returns_none(self):
        assert _parse_due_date("") is None

    def test_none_equivalent(self):
        assert _parse_due_date(None) is None  # type: ignore[arg-type]

    def test_natural_language_parses(self):
        result = _parse_due_date("tomorrow")
        assert result is not None
        assert len(result) == 10  # YYYY-MM-DD

    def test_next_friday_parses(self):
        result = _parse_due_date("next friday")
        assert result is not None
        assert len(result) == 10

    def test_this_friday_parses(self):
        result = _parse_due_date("this friday")
        assert result is not None
        assert len(result) == 10

    def test_next_week_parses(self):
        result = _parse_due_date("next week")
        assert result is not None


# ---------------------------------------------------------------------------
# TaskCreateTool
# ---------------------------------------------------------------------------

class TestTaskCreateTool:
    def test_creates_task(self, create_tool, db):
        result = create_tool._run(title="Fix ceiling fan")
        assert result.success
        assert "Fix ceiling fan" in result.summary
        assert result.data["task_id"].startswith("tsk_")

    def test_no_deadline_asks_question(self, create_tool):
        result = create_tool._run(title="Buy milk")
        assert "Any deadline?" in result.summary

    def test_deadline_given_asks_priority(self, create_tool):
        result = create_tool._run(title="Submit report", due_date="2026-12-01")
        assert "Priority?" in result.summary
        assert "Any deadline?" not in result.summary

    def test_both_given_no_followup(self, create_tool):
        result = create_tool._run(title="Call doctor", due_date="2026-12-01", priority="urgent")
        assert "Any deadline?" not in result.summary
        assert "Priority?" not in result.summary

    def test_returns_task_id(self, create_tool):
        result = create_tool._run(title="Buy milk")
        assert "task_id" in result.data
        assert result.data["task_id"].startswith("tsk_")

    def test_auto_scope_household(self, create_tool):
        result = create_tool._run(title="take out trash")
        assert result.data["scope"] == "household"

    def test_auto_scope_personal(self, create_tool):
        result = create_tool._run(title="read the book")
        assert result.data["scope"] == "personal"

    def test_explicit_scope_respected(self, create_tool):
        result = create_tool._run(title="take out trash", scope="personal")
        assert result.data["scope"] == "personal"

    def test_auto_priority_urgent_keyword(self, create_tool):
        result = create_tool._run(title="urgent call doctor")
        assert result.data["priority"] == "urgent"

    def test_auto_priority_normal_default(self, create_tool):
        result = create_tool._run(title="read book")
        assert result.data["priority"] == "normal"

    def test_due_date_iso_stored(self, create_tool, db):
        result = create_tool._run(title="Submit form", due_date="2026-12-01")
        assert result.success
        assert result.data["due_date"] == "2026-12-01"

    def test_due_date_in_summary(self, create_tool):
        result = create_tool._run(title="Submit form", due_date="2026-12-01")
        assert "2026-12-01" in result.summary

    def test_task_written_to_db(self, create_tool, db):
        create_tool._run(title="DB check task")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT title FROM tasks WHERE title=?", ("DB check task",)).fetchone()
        assert row is not None

    def test_emits_task_created_event(self, create_tool):
        result = create_tool._run(title="Emit event test")
        assert len(result.events) == 1
        assert result.events[0].event_type == "task_created"

    def test_no_db_path_returns_fail(self, ctx):
        bad_ctx = DomainContext(
            user_id="usr_test", household_id="hh_test",
            household_timezone="UTC", oauth_manager=None,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_test", channel="telegram",
            local_db_path="",
        )
        tool = TaskCreateTool(context=bad_ctx)
        result = tool._run(title="Test")
        assert not result.success
        assert result.error_code == "NO_DB_PATH"

    def test_notes_stored(self, create_tool, db):
        create_tool._run(title="Important task", notes="Some details here")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT notes FROM tasks WHERE title=?", ("Important task",)).fetchone()
        assert row[0] == "Some details here"

    def test_capture_state_written_when_no_deadline(self, create_tool, db):
        result = create_tool._run(title="Fix ceiling fan")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT last_task_id, capture_step FROM conversation_state WHERE user_id=?",
                ("usr_test",),
            ).fetchone()
        assert row is not None
        assert row[0] == result.data["task_id"]
        assert row[1] == "due_date"

    def test_capture_state_written_when_no_priority(self, create_tool, db):
        result = create_tool._run(title="Fix ceiling fan", due_date="2026-12-01")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT capture_step FROM conversation_state WHERE user_id=?",
                ("usr_test",),
            ).fetchone()
        assert row is not None
        assert row[0] == "priority"

    def test_no_capture_state_when_all_fields_given(self, create_tool, db):
        create_tool._run(title="Fix ceiling fan", due_date="2026-12-01", priority="urgent")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT last_task_id FROM conversation_state WHERE user_id=?",
                ("usr_test",),
            ).fetchone()
        assert row is None


# ---------------------------------------------------------------------------
# TaskListTool
# ---------------------------------------------------------------------------

class TestTaskListTool:
    def test_inbox_filter(self, list_tool, db):
        _seed_task(db, task_id="tsk_001", title="Task A", status="inbox")
        result = list_tool._run(filter="inbox")
        assert result.success
        assert result.data["count"] >= 1

    def test_today_filter(self, list_tool, db):
        from datetime import date
        today = date.today().isoformat()
        _seed_task(db, task_id="tsk_today1", title="Today task", due_date=today, status="inbox")
        result = list_tool._run(filter="today")
        assert result.success
        titles = [t["title"] for t in result.data["tasks"]]
        assert "Today task" in titles

    def test_today_filter_includes_urgent(self, list_tool, db):
        _seed_task(db, task_id="tsk_today_urg", title="Urgent no-deadline task", priority="urgent")
        result = list_tool._run(filter="today")
        titles = [t["title"] for t in result.data["tasks"]]
        assert "Urgent no-deadline task" in titles

    def test_today_filter_includes_overdue(self, list_tool, db):
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _seed_task(db, task_id="tsk_today_over", title="Overdue task", due_date=yesterday)
        result = list_tool._run(filter="today")
        titles = [t["title"] for t in result.data["tasks"]]
        assert "Overdue task" in titles

    def test_urgent_filter(self, list_tool, db):
        _seed_task(db, task_id="tsk_urg1", title="Urgent task", priority="urgent")
        result = list_tool._run(filter="urgent")
        assert result.success
        assert any(t["priority"] == "urgent" for t in result.data["tasks"])

    def test_household_filter(self, list_tool, db):
        _seed_task(db, task_id="tsk_hh1", title="Household task", scope="household")
        result = list_tool._run(filter="household")
        assert result.success
        assert any(t["scope"] == "household" for t in result.data["tasks"])

    def test_waiting_filter(self, list_tool, db):
        _seed_task(db, task_id="tsk_wait1", title="Waiting task", status="waiting")
        result = list_tool._run(filter="waiting")
        assert result.success
        assert any(t["title"] == "Waiting task" for t in result.data["tasks"])

    def test_all_filter_capped_at_20(self, list_tool, db):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db) as conn:
            for i in range(25):
                conn.execute(
                    "INSERT INTO tasks (id, household_id, user_id, title, scope, priority, status, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"tsk_bulk{i:03d}", "hh_test", "usr_test", f"Bulk task {i}", "personal", "normal", "inbox", "manual", now, now),
                )
        result = list_tool._run(filter="all")
        assert result.data["count"] <= 20

    def test_empty_returns_ok(self, list_tool, db):
        result = list_tool._run(filter="waiting")
        assert result.success
        assert result.data["count"] == 0

    def test_overdue_filter(self, list_tool, db):
        _seed_task(db, task_id="tsk_over1", title="Overdue task", due_date="2020-01-01", status="inbox")
        result = list_tool._run(filter="overdue")
        assert result.success
        assert any(t["title"] == "Overdue task" for t in result.data["tasks"])

    def test_done_excluded_from_inbox(self, list_tool, db):
        _seed_task(db, task_id="tsk_done1", title="Done task", status="done")
        result = list_tool._run(filter="inbox")
        assert all(t["status"] != "done" for t in result.data["tasks"])

    def test_summary_no_task_ids(self, list_tool, db):
        _seed_task(db, task_id="tsk_id_check", title="Fix ceiling fan", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "tsk_" not in result.summary

    def test_summary_numbered_list(self, list_tool, db):
        _seed_task(db, task_id="tsk_num1", title="First task", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "1." in result.summary

    def test_summary_urgent_badge(self, list_tool, db):
        _seed_task(db, task_id="tsk_badge_u", title="Urgent one", priority="urgent", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "⚠️" in result.summary

    def test_summary_high_badge(self, list_tool, db):
        _seed_task(db, task_id="tsk_badge_h", title="High one", priority="high", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "🔹" in result.summary

    def test_summary_household_badge(self, list_tool, db):
        _seed_task(db, task_id="tsk_badge_hh", title="Household one", scope="household", status="inbox")
        result = list_tool._run(filter="household")
        assert "🏠" in result.summary

    def test_summary_due_date_format(self, list_tool, db):
        _seed_task(db, task_id="tsk_due_fmt", title="Has deadline", due_date="2026-07-15", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "— due 2026-07-15" in result.summary

    def test_summary_filter_label_inbox(self, list_tool, db):
        _seed_task(db, task_id="tsk_label1", title="Some task", status="inbox")
        result = list_tool._run(filter="inbox")
        assert "inbox" in result.summary

    def test_summary_filter_label_all(self, list_tool, db):
        _seed_task(db, task_id="tsk_label2", title="Any task", status="inbox")
        result = list_tool._run(filter="all")
        assert "all tasks" in result.summary


# ---------------------------------------------------------------------------
# TaskCompleteTool
# ---------------------------------------------------------------------------

class TestTaskCompleteTool:
    def test_complete_by_id(self, complete_tool, db):
        _seed_task(db, task_id="tsk_comp1", title="Complete me")
        result = complete_tool._run(task_id="tsk_comp1")
        assert result.success
        assert "Complete me" in result.summary

    def test_status_updated_in_db(self, complete_tool, db):
        _seed_task(db, task_id="tsk_comp2", title="Mark done")
        complete_tool._run(task_id="tsk_comp2")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", ("tsk_comp2",)).fetchone()
        assert row[0] == "done"

    def test_completed_at_set(self, complete_tool, db):
        _seed_task(db, task_id="tsk_comp3", title="Set timestamp")
        complete_tool._run(task_id="tsk_comp3")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id=?", ("tsk_comp3",)).fetchone()
        assert row[0] is not None

    def test_complete_by_title_match(self, complete_tool, db):
        _seed_task(db, task_id="tsk_comp4", title="Call the dentist")
        result = complete_tool._run(title_match="dentist")
        assert result.success

    def test_not_found_returns_fail(self, complete_tool):
        result = complete_tool._run(task_id="tsk_nonexistent")
        assert not result.success

    def test_no_args_returns_fail(self, complete_tool):
        result = complete_tool._run()
        assert not result.success

    def test_emits_completed_event(self, complete_tool, db):
        _seed_task(db, task_id="tsk_comp5", title="Event emitter")
        result = complete_tool._run(task_id="tsk_comp5")
        assert len(result.events) == 1
        assert result.events[0].event_type == "task_completed"

    def test_complete_wrong_household_fails(self, db):
        _seed_task(db, task_id="tsk_comp6", title="Other household task", household_id="hh_other")
        ctx2 = DomainContext(
            user_id="usr_test", household_id="hh_test",
            household_timezone="UTC", oauth_manager=None,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_test", channel="telegram",
            local_db_path=db,
        )
        tool = TaskCompleteTool(context=ctx2)
        result = tool._run(task_id="tsk_comp6")
        assert not result.success


# ---------------------------------------------------------------------------
# TaskUpdateTool
# ---------------------------------------------------------------------------

class TestTaskUpdateTool:
    def test_update_priority(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd1", title="Original title")
        result = update_tool._run(task_id="tsk_upd1", priority="urgent")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT priority FROM tasks WHERE id=?", ("tsk_upd1",)).fetchone()
        assert row[0] == "urgent"

    def test_update_title(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd2", title="Old title")
        result = update_tool._run(task_id="tsk_upd2", title="New title")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT title FROM tasks WHERE id=?", ("tsk_upd2",)).fetchone()
        assert row[0] == "New title"

    def test_update_due_date_iso(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd3", title="Due date task")
        result = update_tool._run(task_id="tsk_upd3", due_date="2026-12-31")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT due_date FROM tasks WHERE id=?", ("tsk_upd3",)).fetchone()
        assert row[0] == "2026-12-31"

    def test_no_fields_returns_fail(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd4", title="No update")
        result = update_tool._run(task_id="tsk_upd4")
        assert not result.success

    def test_not_found_returns_fail(self, update_tool):
        result = update_tool._run(task_id="tsk_nonexistent", priority="urgent")
        assert not result.success

    def test_update_status(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd5", title="Status change")
        update_tool._run(task_id="tsk_upd5", status="active")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id=?", ("tsk_upd5",)).fetchone()
        assert row[0] == "active"

    def test_summary_shows_old_title(self, update_tool, db):
        _seed_task(db, task_id="tsk_upd6", title="My Task")
        result = update_tool._run(task_id="tsk_upd6", priority="high")
        assert "My Task" in result.summary

    def test_no_task_id_and_no_capture_state_returns_fail(self, update_tool):
        result = update_tool._run(task_id="", priority="urgent")
        assert not result.success

    def test_capture_state_due_date_step_updates_task(self, update_tool, db):
        from mithra.domains.tasks.tools import _store_capture_state
        _seed_task(db, task_id="tsk_cap1", title="Fix ceiling fan")
        _store_capture_state(db, "usr_test", "tsk_cap1", "due_date", {"title": "Fix ceiling fan"})

        result = update_tool._run(task_id="", due_date="2026-12-01")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT due_date FROM tasks WHERE id=?", ("tsk_cap1",)).fetchone()
        assert row[0] == "2026-12-01"

    def test_capture_state_advances_from_due_date_to_priority(self, update_tool, db):
        from mithra.domains.tasks.tools import _store_capture_state, _get_capture_state
        _seed_task(db, task_id="tsk_cap2", title="Fix roof")
        _store_capture_state(db, "usr_test", "tsk_cap2", "due_date", {})

        update_tool._run(task_id="", due_date="2026-12-01")

        state = _get_capture_state(db, "usr_test")
        assert state is not None
        assert state["step"] == "priority"

    def test_capture_state_advances_from_priority_to_notes(self, update_tool, db):
        from mithra.domains.tasks.tools import _store_capture_state, _get_capture_state
        _seed_task(db, task_id="tsk_cap3", title="Fix gate")
        _store_capture_state(db, "usr_test", "tsk_cap3", "priority", {})

        update_tool._run(task_id="", priority="high")

        state = _get_capture_state(db, "usr_test")
        assert state is not None
        assert state["step"] == "notes"

    def test_capture_state_cleared_after_notes_step(self, update_tool, db):
        from mithra.domains.tasks.tools import _store_capture_state, _get_capture_state
        _seed_task(db, task_id="tsk_cap4", title="Fix fence")
        _store_capture_state(db, "usr_test", "tsk_cap4", "notes", {})

        result = update_tool._run(task_id="", notes="Call contractor first")

        state = _get_capture_state(db, "usr_test")
        assert state is None
        assert result.success

    def test_capture_next_question_in_summary(self, update_tool, db):
        from mithra.domains.tasks.tools import _store_capture_state
        _seed_task(db, task_id="tsk_cap5", title="Fix HVAC")
        _store_capture_state(db, "usr_test", "tsk_cap5", "due_date", {})

        result = update_tool._run(task_id="", due_date="2026-12-01")

        assert "urgent" in result.summary.lower() or "priority" in result.summary.lower()


# ---------------------------------------------------------------------------
# TaskSnoozeTool
# ---------------------------------------------------------------------------

class TestTaskSnoozeTool:
    def test_snooze_task(self, snooze_tool, db):
        _seed_task(db, task_id="tsk_snz1", title="Snooze me")
        result = snooze_tool._run(task_id="tsk_snz1", until="2026-12-01")
        assert result.success
        assert "2026-12-01" in result.summary

    def test_status_set_to_snoozed(self, snooze_tool, db):
        _seed_task(db, task_id="tsk_snz2", title="Status check")
        snooze_tool._run(task_id="tsk_snz2", until="2026-12-01")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status, snoozed_until FROM tasks WHERE id=?", ("tsk_snz2",)).fetchone()
        assert row[0] == "snoozed"
        assert row[1] == "2026-12-01"

    def test_unparseable_date_returns_fail(self, snooze_tool, db):
        _seed_task(db, task_id="tsk_snz3", title="Bad date")
        result = snooze_tool._run(task_id="tsk_snz3", until="not a date xyz")
        assert not result.success
        assert result.error_code == "UNPARSEABLE_DATE"

    def test_not_found_returns_fail(self, snooze_tool):
        result = snooze_tool._run(task_id="tsk_nonexistent", until="2026-12-01")
        assert not result.success


# ---------------------------------------------------------------------------
# TaskDeleteTool
# ---------------------------------------------------------------------------

class TestTaskDeleteTool:
    def test_delete_task(self, delete_tool, db):
        _seed_task(db, task_id="tsk_del1", title="Delete me")
        result = delete_tool._run(task_id="tsk_del1")
        assert result.success
        assert "Delete me" in result.summary

    def test_task_removed_from_db(self, delete_tool, db):
        _seed_task(db, task_id="tsk_del2", title="Gone")
        delete_tool._run(task_id="tsk_del2")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT id FROM tasks WHERE id=?", ("tsk_del2",)).fetchone()
        assert row is None

    def test_not_found_returns_fail(self, delete_tool):
        result = delete_tool._run(task_id="tsk_nonexistent")
        assert not result.success

    def test_wrong_household_fails(self, db):
        _seed_task(db, task_id="tsk_del3", title="Other HH", household_id="hh_other")
        ctx2 = DomainContext(
            user_id="usr_test", household_id="hh_test",
            household_timezone="UTC", oauth_manager=None,
            storage_adapter=None, file_router=None, member_registry=None,
            file_search=None, request_id="req_test", channel="telegram",
            local_db_path=db,
        )
        tool = TaskDeleteTool(context=ctx2)
        result = tool._run(task_id="tsk_del3")
        assert not result.success


# ---------------------------------------------------------------------------
# TaskSuggestTool
# ---------------------------------------------------------------------------

class TestTaskSuggestTool:
    def test_returns_up_to_3(self, suggest_tool, db):
        for i in range(5):
            _seed_task(db, task_id=f"tsk_sug{i:02d}", title=f"Task {i}", priority="normal")
        result = suggest_tool._run()
        assert result.success
        assert len(result.data["suggestions"]) <= 3

    def test_urgent_comes_first(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_n", title="Normal task", priority="normal")
        _seed_task(db, task_id="tsk_sug_u", title="Urgent task", priority="urgent")
        result = suggest_tool._run()
        assert result.data["suggestions"][0]["priority"] == "urgent"

    def test_empty_list_ok(self, suggest_tool, db):
        result = suggest_tool._run()
        assert result.success
        assert "caught up" in result.summary.lower() or result.data["count"] == 0 or "suggestions" in result.data

    def test_done_tasks_excluded(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_d", title="Done task", status="done", priority="urgent")
        result = suggest_tool._run()
        titles = [t["title"] for t in result.data.get("suggestions", [])]
        assert "Done task" not in titles

    def test_snoozed_excluded(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_s", title="Snoozed task", status="snoozed", priority="urgent")
        result = suggest_tool._run()
        titles = [t["title"] for t in result.data.get("suggestions", [])]
        assert "Snoozed task" not in titles

    def test_summary_contains_task_title(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_t", title="Fix the leak")
        result = suggest_tool._run()
        assert "Fix the leak" in result.summary

    def test_summary_no_task_ids(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_id", title="Check emails")
        result = suggest_tool._run()
        assert "tsk_" not in result.summary

    def test_summary_urgent_badge_suggest(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_ub", title="Critical job", priority="urgent")
        result = suggest_tool._run()
        assert "⚠️" in result.summary

    def test_summary_household_badge_suggest(self, suggest_tool, db):
        _seed_task(db, task_id="tsk_sug_hh", title="Fix gate", scope="household")
        result = suggest_tool._run()
        assert "🏠" in result.summary


# ---------------------------------------------------------------------------
# build_tasks_tools factory
# ---------------------------------------------------------------------------

class TestBuildTasksTools:
    def test_returns_14_bundles_legacy(self, ctx):
        bundles = build_tasks_tools(ctx)
        assert len(bundles) == 14

    def test_tool_names(self, ctx):
        names = {b.metadata.name for b in build_tasks_tools(ctx)}
        expected = {
            "task_create", "task_list", "task_complete",
            "task_update", "task_snooze", "task_delete", "task_suggest",
            "project_create", "project_update", "milestone_complete", "project_status",
            "routine_create", "routine_complete", "routine_list",
        }
        assert names == expected

    def test_delete_is_destructive(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        delete = next(b for b in bundles if b.metadata.name == "task_delete")
        assert delete.metadata.permission == PermissionTier.DESTRUCTIVE

    def test_create_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        create = next(b for b in bundles if b.metadata.name == "task_create")
        assert create.metadata.permission == PermissionTier.READ

    def test_list_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        lst = next(b for b in bundles if b.metadata.name == "task_list")
        assert lst.metadata.permission == PermissionTier.READ

    def test_all_domain_ids_are_tasks(self, ctx):
        for bundle in build_tasks_tools(ctx):
            assert bundle.metadata.domain_id == "tasks"

    def test_create_emits_event(self, ctx):
        bundles = build_tasks_tools(ctx)
        create = next(b for b in bundles if b.metadata.name == "task_create")
        assert create.metadata.event_emitted == "task_created"

    def test_complete_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        complete = next(b for b in bundles if b.metadata.name == "task_complete")
        assert complete.metadata.permission == PermissionTier.READ

    def test_project_create_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "project_create")
        assert b.metadata.permission == PermissionTier.READ

    def test_project_update_is_write(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "project_update")
        assert b.metadata.permission == PermissionTier.WRITE

    def test_milestone_complete_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "milestone_complete")
        assert b.metadata.permission == PermissionTier.READ

    def test_project_status_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "project_status")
        assert b.metadata.permission == PermissionTier.READ

    def test_project_create_emits_event(self, ctx):
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "project_create")
        assert b.metadata.event_emitted == "project_created"

    def test_milestone_complete_emits_event(self, ctx):
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "milestone_complete")
        assert b.metadata.event_emitted == "milestone_completed"


# ---------------------------------------------------------------------------
# _find_project — disambiguation helper
# ---------------------------------------------------------------------------

class TestFindProject:
    def test_prefers_project_with_milestones(self, db):
        import sqlite3
        from mithra.domains.tasks.tools import _find_project
        _seed_project(db, project_id="prj_empty", title="Backyard Deck")
        _seed_project(db, project_id="prj_rich", title="Build Backyard Deck")
        _seed_milestone(db, "ms_r1", "prj_rich", "Phase 1")
        _seed_milestone(db, "ms_r2", "prj_rich", "Phase 2")
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            result = _find_project(conn, "hh_test", "backyard deck")
        assert result is not None
        assert result["id"] == "prj_rich"

    def test_returns_only_match_when_one_project(self, db):
        import sqlite3
        from mithra.domains.tasks.tools import _find_project
        _seed_project(db, project_id="prj_solo", title="Garden Wall")
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            result = _find_project(conn, "hh_test", "Garden Wall")
        assert result is not None
        assert result["id"] == "prj_solo"

    def test_returns_none_when_no_match(self, db):
        import sqlite3
        from mithra.domains.tasks.tools import _find_project
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            result = _find_project(conn, "hh_test", "Nonexistent XYZ")
        assert result is None

    def test_excludes_completed_projects(self, db):
        import sqlite3
        from mithra.domains.tasks.tools import _find_project
        _seed_project(db, project_id="prj_done", title="Finished Deck", status="completed")
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            result = _find_project(conn, "hh_test", "Finished Deck")
        assert result is None

    def test_milestone_complete_uses_milestone_rich_project(self, db, ctx):
        _seed_project(db, project_id="prj_empty2", title="Deck")
        _seed_project(db, project_id="prj_rich2", title="Backyard Deck")
        _seed_milestone(db, "ms_e1", "prj_rich2", "Buy materials")
        result = MilestoneCompleteTool(context=ctx)._run(project_query="deck")
        assert result.success
        assert "Buy materials" in result.summary


# ---------------------------------------------------------------------------
# Helpers shared by project tool tests
# ---------------------------------------------------------------------------

def _seed_project(db_path, project_id="prj_aabb", title="Backyard Deck",
                  scope="household", status="active", target_date=None,
                  household_id="hh_test", user_id="usr_test"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO projects
               (id, household_id, user_id, title, scope, status, target_date, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (project_id, household_id, user_id, title, scope, status, target_date, now, now),
        )


def _seed_milestone(db_path, ms_id, project_id, title, order_index=1,
                    status="pending", household_id="hh_test"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO milestones
               (id, project_id, household_id, title, order_index, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ms_id, project_id, household_id, title, order_index, status, now, now),
        )


# ---------------------------------------------------------------------------
# ProjectCreateTool
# ---------------------------------------------------------------------------

class TestProjectCreateTool:
    @pytest.fixture()
    def tool(self, ctx):
        return ProjectCreateTool(context=ctx)

    def test_creates_project(self, tool, db):
        result = tool._run(title="Backyard Deck")
        assert result.success
        assert result.data["project_id"].startswith("prj_")

    def test_project_written_to_db(self, tool, db):
        result = tool._run(title="Woodworking Bench")
        pid = result.data["project_id"]
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        assert row is not None

    def test_target_date_parsed(self, tool, db):
        result = tool._run(title="Deck", target_date="2026-07-04")
        assert result.data["target_date"] == "2026-07-04"

    def test_scope_auto_household(self, tool, db):
        result = tool._run(title="Backyard deck renovation")
        assert result.data["scope"] == "household"

    def test_scope_explicit(self, tool, db):
        result = tool._run(title="Personal project", scope="personal")
        assert result.data["scope"] == "personal"

    def test_milestones_created(self, tool, db):
        ms = ["Buy wood", "Cut pieces", "Assemble"]
        result = tool._run(title="Bench", milestones=ms)
        pid = result.data["project_id"]
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT * FROM milestones WHERE project_id=? ORDER BY order_index", (pid,)
            ).fetchall()
        assert len(rows) == 3
        assert rows[0][3] == "Buy wood"   # title column

    def test_milestones_order_index(self, tool, db):
        result = tool._run(title="Bench2", milestones=["A", "B", "C", "D"])
        pid = result.data["project_id"]
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT order_index FROM milestones WHERE project_id=? ORDER BY order_index", (pid,)
            ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3, 4]

    def test_no_milestones_asks_question(self, tool, db):
        result = tool._run(title="New project")
        assert "milestone" in result.summary.lower()

    def test_milestones_in_summary(self, tool, db):
        result = tool._run(title="Deck", milestones=["Frame", "Paint"])
        assert "Frame" in result.summary
        assert "Paint" in result.summary

    def test_household_badge_in_summary(self, tool, db):
        result = tool._run(title="yard fence", scope="household")
        assert "🏠" in result.summary

    def test_no_db_path_returns_fail(self, ctx):
        from mithra.domains.context import DomainContext
        ctx2 = DomainContext(
            user_id="usr_test", household_id="hh_test",
            household_timezone="UTC", oauth_manager=None,
            storage_adapter=None, file_router=None,
            member_registry=None, file_search=None,
            request_id="r", channel="telegram", local_db_path="",
        )
        result = ProjectCreateTool(context=ctx2)._run(title="Test")
        assert not result.success

    def test_milestone_count_in_data(self, tool, db):
        result = tool._run(title="Proj", milestones=["A", "B"])
        assert result.data["milestone_count"] == 2

    def test_capture_state_stored_when_no_milestones(self, tool, db):
        from mithra.domains.tasks.tools import _get_capture_state
        result = tool._run(title="New Project")
        assert result.success
        state = _get_capture_state(db, "usr_test")
        assert state is not None
        assert state["step"] == "milestone"
        assert state["task_id"] == result.data["project_id"]

    def test_no_capture_state_when_milestones_provided(self, tool, db):
        from mithra.domains.tasks.tools import _get_capture_state
        tool._run(title="Bench", milestones=["A", "B"])
        state = _get_capture_state(db, "usr_test")
        assert state is None


# ---------------------------------------------------------------------------
# ProjectUpdateTool
# ---------------------------------------------------------------------------

class TestProjectUpdateTool:
    @pytest.fixture()
    def tool(self, ctx):
        return ProjectUpdateTool(context=ctx)

    def test_update_status_on_hold(self, tool, db):
        _seed_project(db, project_id="prj_u1", title="Backyard Deck")
        result = tool._run(project_query="Backyard Deck", status="on_hold")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status FROM projects WHERE id='prj_u1'").fetchone()
        assert row[0] == "on_hold"

    def test_update_title(self, tool, db):
        _seed_project(db, project_id="prj_u2", title="Old Title")
        result = tool._run(project_query="Old Title", title="New Title")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT title FROM projects WHERE id='prj_u2'").fetchone()
        assert row[0] == "New Title"

    def test_complete_status_summary(self, tool, db):
        _seed_project(db, project_id="prj_u3", title="Done Project")
        result = tool._run(project_query="Done Project", status="completed")
        assert "complete" in result.summary.lower()

    def test_not_found_returns_fail(self, tool, db):
        result = tool._run(project_query="Nonexistent Project XYZ")
        assert not result.success

    def test_no_fields_returns_fail(self, tool, db):
        _seed_project(db, project_id="prj_u4", title="Empty Update")
        result = tool._run(project_query="Empty Update")
        assert not result.success

    def test_target_date_parsed_and_stored(self, tool, db):
        _seed_project(db, project_id="prj_u5", title="Date Update")
        result = tool._run(project_query="Date Update", target_date="2026-08-01")
        assert result.success
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT target_date FROM projects WHERE id='prj_u5'").fetchone()
        assert row[0] == "2026-08-01"

    def test_completed_projects_excluded_from_search(self, tool, db):
        _seed_project(db, project_id="prj_u6", title="Finished", status="completed")
        result = tool._run(project_query="Finished", title="New Name")
        assert not result.success  # completed projects not found


# ---------------------------------------------------------------------------
# MilestoneCompleteTool
# ---------------------------------------------------------------------------

class TestMilestoneCompleteTool:
    @pytest.fixture()
    def tool(self, ctx):
        return MilestoneCompleteTool(context=ctx)

    def test_completes_first_pending(self, tool, db):
        _seed_project(db, project_id="prj_m1", title="Deck")
        _seed_milestone(db, "ms_m1a", "prj_m1", "Buy materials", order_index=1)
        _seed_milestone(db, "ms_m1b", "prj_m1", "Build frame", order_index=2)
        result = tool._run(project_query="Deck")
        assert result.success
        assert "Buy materials" in result.summary

    def test_milestone_status_updated(self, tool, db):
        _seed_project(db, project_id="prj_m2", title="Bench")
        _seed_milestone(db, "ms_m2a", "prj_m2", "Cut wood", order_index=1)
        tool._run(project_query="Bench")
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status FROM milestones WHERE id='ms_m2a'").fetchone()
        assert row[0] == "done"

    def test_progress_in_summary(self, tool, db):
        _seed_project(db, project_id="prj_m3", title="Fence")
        _seed_milestone(db, "ms_m3a", "prj_m3", "Phase 1", order_index=1)
        _seed_milestone(db, "ms_m3b", "prj_m3", "Phase 2", order_index=2)
        result = tool._run(project_query="Fence")
        assert "1/2" in result.summary

    def test_percentage_correct(self, tool, db):
        _seed_project(db, project_id="prj_m4", title="Wall")
        _seed_milestone(db, "ms_m4a", "prj_m4", "A", order_index=1)
        _seed_milestone(db, "ms_m4b", "prj_m4", "B", order_index=2)
        _seed_milestone(db, "ms_m4c", "prj_m4", "C", order_index=3)
        _seed_milestone(db, "ms_m4d", "prj_m4", "D", order_index=4)
        result = tool._run(project_query="Wall")
        assert "25%" in result.summary

    def test_auto_completes_project_when_all_done(self, tool, db):
        _seed_project(db, project_id="prj_m5", title="Shed")
        _seed_milestone(db, "ms_m5a", "prj_m5", "Only step", order_index=1)
        result = tool._run(project_query="Shed")
        assert result.data["auto_completed"] is True
        assert "🎉" in result.summary
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT status FROM projects WHERE id='prj_m5'").fetchone()
        assert row[0] == "completed"

    def test_milestone_query_selects_by_title(self, tool, db):
        _seed_project(db, project_id="prj_m6", title="Pool")
        _seed_milestone(db, "ms_m6a", "prj_m6", "Dig hole", order_index=1)
        _seed_milestone(db, "ms_m6b", "prj_m6", "Fill water", order_index=2)
        result = tool._run(project_query="Pool", milestone_query="Fill")
        assert "Fill water" in result.summary

    def test_no_pending_returns_fail(self, tool, db):
        _seed_project(db, project_id="prj_m7", title="Done Proj")
        _seed_milestone(db, "ms_m7a", "prj_m7", "Already done", order_index=1, status="done")
        result = tool._run(project_query="Done Proj")
        assert not result.success

    def test_project_not_found_returns_fail(self, tool, db):
        result = tool._run(project_query="Nonexistent XYZ")
        assert not result.success

    def test_ordinal_phase_1_resolves_to_order_index_1(self, tool, db):
        _seed_project(db, project_id="prj_o1", title="Backyard Deck")
        _seed_milestone(db, "ms_o1a", "prj_o1", "Buy materials", order_index=1)
        _seed_milestone(db, "ms_o1b", "prj_o1", "Build frame", order_index=2)
        result = tool._run(project_query="Backyard Deck", milestone_query="phase 1")
        assert result.success
        assert "Buy materials" in result.summary

    def test_ordinal_phase_2_resolves_to_order_index_2(self, tool, db):
        _seed_project(db, project_id="prj_o2", title="Patio Project")
        _seed_milestone(db, "ms_o2a", "prj_o2", "Dig foundation", order_index=1)
        _seed_milestone(db, "ms_o2b", "prj_o2", "Lay stones", order_index=2)
        result = tool._run(project_query="Patio", milestone_query="phase 2")
        assert result.success
        assert "Lay stones" in result.summary

    def test_ordinal_word_second_resolves_to_order_index_2(self, tool, db):
        _seed_project(db, project_id="prj_o3", title="Garage Build")
        _seed_milestone(db, "ms_o3a", "prj_o3", "Clear space", order_index=1)
        _seed_milestone(db, "ms_o3b", "prj_o3", "Frame walls", order_index=2)
        result = tool._run(project_query="Garage", milestone_query="second")
        assert result.success
        assert "Frame walls" in result.summary

    def test_ordinal_word_first_resolves_to_order_index_1(self, tool, db):
        _seed_project(db, project_id="prj_o4", title="Shed Construction")
        _seed_milestone(db, "ms_o4a", "prj_o4", "Pour slab", order_index=1)
        _seed_milestone(db, "ms_o4b", "prj_o4", "Erect frame", order_index=2)
        result = tool._run(project_query="Shed", milestone_query="first")
        assert result.success
        assert "Pour slab" in result.summary

    def test_title_match_takes_priority_over_ordinal(self, tool, db):
        _seed_project(db, project_id="prj_o5", title="Phase Project")
        _seed_milestone(db, "ms_o5a", "prj_o5", "Phase 2 prep", order_index=1)
        _seed_milestone(db, "ms_o5b", "prj_o5", "Install gear", order_index=2)
        # "Phase 2 prep" title matches "phase 2" LIKE before ordinal logic kicks in
        result = tool._run(project_query="Phase Project", milestone_query="Phase 2 prep")
        assert result.success
        assert "Phase 2 prep" in result.summary

    def test_no_query_falls_back_to_next_pending(self, tool, db):
        _seed_project(db, project_id="prj_o6", title="Fence Build")
        _seed_milestone(db, "ms_o6a", "prj_o6", "Order posts", order_index=1, status="done")
        _seed_milestone(db, "ms_o6b", "prj_o6", "Dig holes", order_index=2)
        result = tool._run(project_query="Fence")
        assert result.success
        assert "Dig holes" in result.summary


# ---------------------------------------------------------------------------
# ProjectStatusTool
# ---------------------------------------------------------------------------

class TestProjectStatusTool:
    @pytest.fixture()
    def tool(self, ctx):
        return ProjectStatusTool(context=ctx)

    def test_shows_project_title(self, tool, db):
        _seed_project(db, project_id="prj_s1", title="Backyard Deck")
        result = tool._run(project_query="Backyard")
        assert "Backyard Deck" in result.summary

    def test_shows_all_milestones(self, tool, db):
        _seed_project(db, project_id="prj_s2", title="Garden")
        _seed_milestone(db, "ms_s2a", "prj_s2", "Dig beds", order_index=1)
        _seed_milestone(db, "ms_s2b", "prj_s2", "Plant seeds", order_index=2)
        result = tool._run(project_query="Garden")
        assert "Dig beds" in result.summary
        assert "Plant seeds" in result.summary

    def test_done_icon(self, tool, db):
        _seed_project(db, project_id="prj_s3", title="Garage")
        _seed_milestone(db, "ms_s3a", "prj_s3", "Clear space", order_index=1, status="done")
        result = tool._run(project_query="Garage")
        assert "✓" in result.summary

    def test_pending_icon(self, tool, db):
        _seed_project(db, project_id="prj_s4", title="Studio")
        _seed_milestone(db, "ms_s4a", "prj_s4", "Buy paint", order_index=1, status="pending")
        result = tool._run(project_query="Studio")
        assert "○" in result.summary

    def test_progress_fraction_shown(self, tool, db):
        _seed_project(db, project_id="prj_s5", title="Deck2")
        _seed_milestone(db, "ms_s5a", "prj_s5", "M1", order_index=1, status="done")
        _seed_milestone(db, "ms_s5b", "prj_s5", "M2", order_index=2, status="done")
        _seed_milestone(db, "ms_s5c", "prj_s5", "M3", order_index=3, status="pending")
        _seed_milestone(db, "ms_s5d", "prj_s5", "M4", order_index=4, status="pending")
        result = tool._run(project_query="Deck2")
        assert "2/4" in result.summary
        assert "50%" in result.summary

    def test_household_badge_shown(self, tool, db):
        _seed_project(db, project_id="prj_s6", title="Shed2", scope="household")
        result = tool._run(project_query="Shed2")
        assert "🏠" in result.summary

    def test_target_date_shown(self, tool, db):
        _seed_project(db, project_id="prj_s7", title="Fence2", target_date="2026-09-01")
        result = tool._run(project_query="Fence2")
        assert "2026-09-01" in result.summary

    def test_not_found_returns_fail(self, tool, db):
        result = tool._run(project_query="Totally Nonexistent XYZ")
        assert not result.success

    def test_data_contains_pct(self, tool, db):
        _seed_project(db, project_id="prj_s8", title="Gate")
        _seed_milestone(db, "ms_s8a", "prj_s8", "Step", order_index=1, status="done")
        result = tool._run(project_query="Gate")
        assert result.data["pct"] == 100


# ---------------------------------------------------------------------------
# Routine seed helper
# ---------------------------------------------------------------------------

def _seed_routine(db_path, routine_id, title, scope="household",
                  reset_time="00:00", active=1, household_id="hh_test"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO routines
               (id, household_id, title, scope, reset_time, active, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (routine_id, household_id, title, scope, reset_time, active, now),
        )


def _seed_routine_completion(db_path, routine_id, completion_date,
                              completed_by="usr_test", household_id="hh_test"):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO routine_completions
               (id, routine_id, household_id, completed_by, completion_date, completed_at)
               VALUES (?,?,?,?,?,?)""",
            (f"rc_{routine_id}", routine_id, household_id, completed_by, completion_date, now),
        )


# ---------------------------------------------------------------------------
# RoutineCreateTool
# ---------------------------------------------------------------------------

class TestRoutineCreateTool:
    @pytest.fixture()
    def tool(self, ctx):
        return RoutineCreateTool(context=ctx)

    def test_creates_household_routine(self, tool, db):
        result = tool._run(title="mix baby formula", scope="household")
        assert result.success
        assert "mix baby formula" in result.summary
        assert "🏠 Household" in result.summary

    def test_creates_personal_routine(self, tool, db):
        result = tool._run(title="morning meditation", scope="personal")
        assert result.success
        assert "👤 Personal" in result.summary

    def test_auto_classifies_household_via_keyword(self, tool, db):
        result = tool._run(title="warm water for baby")
        assert result.success
        assert result.data["scope"] == "household"

    def test_auto_classifies_personal_via_keyword(self, tool, db):
        result = tool._run(title="gym workout")
        assert result.success
        assert result.data["scope"] == "personal"

    def test_midnight_reset_label(self, tool, db):
        result = tool._run(title="soak lentils", scope="household")
        assert "Resets daily at midnight" in result.summary

    def test_custom_reset_time_label(self, tool, db):
        result = tool._run(title="evening wind-down", scope="household", reset_time="21:00")
        assert "Resets at 21:00" in result.summary
        assert result.data["reset_time"] == "21:00"

    def test_invalid_reset_time_defaults_to_midnight(self, tool, db):
        result = tool._run(title="morning check", scope="household", reset_time="badtime")
        assert result.data["reset_time"] == "00:00"

    def test_routine_id_in_data(self, tool, db):
        result = tool._run(title="mix formula", scope="household")
        assert result.data["routine_id"].startswith("rtn_")

    def test_routine_persisted_in_db(self, tool, db):
        tool._run(title="soak lentils", scope="household")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT title, scope FROM routines WHERE title='soak lentils'"
            ).fetchone()
        assert row is not None
        assert row[0] == "soak lentils"
        assert row[1] == "household"

    def test_emits_routine_created_event(self, tool, db):
        result = tool._run(title="evening check", scope="household")
        assert any(e.event_type == "routine_created" for e in result.events)

    def test_no_db_path_returns_fail(self, ctx):
        ctx2 = DomainContext(
            user_id=ctx.user_id, household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
            oauth_manager=None, storage_adapter=None, file_router=None,
            member_registry=None, file_search=None,
            request_id="", channel="telegram", local_db_path="",
        )
        result = RoutineCreateTool(context=ctx2)._run(title="test")
        assert not result.success

    def test_returns_14_bundles(self, ctx):
        bundles = build_tasks_tools(ctx)
        assert len(bundles) == 14

    def test_routine_tool_names_in_factory(self, ctx):
        bundles = build_tasks_tools(ctx)
        names = {b.metadata.name for b in bundles}
        assert {"routine_create", "routine_complete", "routine_list"}.issubset(names)

    def test_routine_create_is_write(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "routine_create")
        assert b.metadata.permission == PermissionTier.WRITE

    def test_routine_complete_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "routine_complete")
        assert b.metadata.permission == PermissionTier.READ

    def test_routine_list_is_read(self, ctx):
        from mithra.domains.bundle import PermissionTier
        bundles = build_tasks_tools(ctx)
        b = next(b for b in bundles if b.metadata.name == "routine_list")
        assert b.metadata.permission == PermissionTier.READ


# ---------------------------------------------------------------------------
# RoutineCompleteTool
# ---------------------------------------------------------------------------

class TestRoutineCompleteTool:
    @pytest.fixture()
    def tool(self, ctx):
        return RoutineCompleteTool(context=ctx)

    def test_marks_routine_done(self, tool, db):
        _seed_routine(db, "rtn_c1", "mix baby formula")
        result = tool._run(routine_query="baby formula")
        assert result.success
        assert "✓" in result.summary
        assert "mix baby formula" in result.summary

    def test_completion_row_written(self, tool, db):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _seed_routine(db, "rtn_c2", "soak lentils")
        tool._run(routine_query="lentils")
        with sqlite3.connect(db) as conn:
            row = conn.execute(
                "SELECT completion_date FROM routine_completions WHERE routine_id='rtn_c2'"
            ).fetchone()
        assert row is not None
        assert row[0] == today

    def test_idempotent_same_user(self, tool, db):
        _seed_routine(db, "rtn_c3", "warm water")
        tool._run(routine_query="warm water")
        result = tool._run(routine_query="warm water")
        assert result.success
        assert "already marked done today" in result.summary

    def test_idempotent_different_user(self, tool, db, ctx):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _seed_routine(db, "rtn_c4", "baby formula dup")
        _seed_routine_completion(db, "rtn_c4", today, completed_by="usr_wife")
        result = tool._run(routine_query="baby formula dup")
        assert result.success
        assert "by another member" in result.summary

    def test_no_match_returns_fail(self, tool, db):
        result = tool._run(routine_query="totally nonexistent xyz")
        assert not result.success
        assert "No routine found" in result.summary

    def test_inactive_routine_not_matched(self, tool, db):
        _seed_routine(db, "rtn_c5", "old routine", active=0)
        result = tool._run(routine_query="old routine")
        assert not result.success

    def test_completion_date_uses_household_timezone(self, tool, db, ctx):
        _seed_routine(db, "rtn_c6", "tz routine")
        result = tool._run(routine_query="tz routine")
        assert result.success
        # completion_date is a YYYY-MM-DD string (in household tz = UTC for test)
        from datetime import datetime, timezone
        expected = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert result.data["completion_date"] == expected

    def test_no_db_path_returns_fail(self, ctx):
        ctx2 = DomainContext(
            user_id=ctx.user_id, household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
            oauth_manager=None, storage_adapter=None, file_router=None,
            member_registry=None, file_search=None,
            request_id="", channel="telegram", local_db_path="",
        )
        result = RoutineCompleteTool(context=ctx2)._run(routine_query="anything")
        assert not result.success


# ---------------------------------------------------------------------------
# RoutineListTool
# ---------------------------------------------------------------------------

class TestRoutineListTool:
    @pytest.fixture()
    def tool(self, ctx):
        return RoutineListTool(context=ctx)

    def test_empty_returns_friendly_message(self, tool, db):
        result = tool._run()
        assert result.success
        assert "No routines" in result.summary

    def test_shows_pending_routines(self, tool, db):
        _seed_routine(db, "rtn_l1", "mix baby formula")
        result = tool._run()
        assert "○" in result.summary
        assert "mix baby formula" in result.summary

    def test_shows_done_routines(self, tool, db):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _seed_routine(db, "rtn_l2", "soak lentils")
        _seed_routine_completion(db, "rtn_l2", today)
        result = tool._run()
        assert "✓" in result.summary
        assert "soak lentils" in result.summary

    def test_done_pending_split(self, tool, db):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _seed_routine(db, "rtn_l3", "formula")
        _seed_routine(db, "rtn_l4", "lentils")
        _seed_routine_completion(db, "rtn_l3", today)
        result = tool._run()
        assert "1/2 done" in result.summary

    def test_pending_listed_before_done(self, tool, db):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _seed_routine(db, "rtn_l5", "Alpha routine")
        _seed_routine(db, "rtn_l6", "Beta routine")
        _seed_routine_completion(db, "rtn_l5", today)
        result = tool._run()
        # Beta (pending) should appear before Alpha (done)
        assert result.summary.index("Beta routine") < result.summary.index("Alpha routine")

    def test_household_badge_shown(self, tool, db):
        _seed_routine(db, "rtn_l7", "household task", scope="household")
        result = tool._run()
        assert "🏠" in result.summary

    def test_personal_no_household_badge(self, tool, db):
        _seed_routine(db, "rtn_l8", "my yoga", scope="personal")
        result = tool._run()
        assert "my yoga" in result.summary
        # Personal routines get no 🏠 badge
        assert "🏠" not in result.summary

    def test_scope_filter_household(self, tool, db):
        _seed_routine(db, "rtn_l9a", "household item", scope="household")
        _seed_routine(db, "rtn_l9b", "personal item", scope="personal")
        result = tool._run(scope_filter="household")
        assert "household item" in result.summary
        assert "personal item" not in result.summary

    def test_scope_filter_personal(self, tool, db):
        _seed_routine(db, "rtn_l10a", "household item2", scope="household")
        _seed_routine(db, "rtn_l10b", "personal item2", scope="personal")
        result = tool._run(scope_filter="personal")
        assert "personal item2" in result.summary
        assert "household item2" not in result.summary

    def test_data_fields_present(self, tool, db):
        _seed_routine(db, "rtn_l11", "check data")
        result = tool._run()
        assert "count" in result.data
        assert "done_count" in result.data
        assert "date" in result.data

    def test_inactive_routine_excluded(self, tool, db):
        _seed_routine(db, "rtn_l12", "inactive one", active=0)
        result = tool._run()
        assert "inactive one" not in result.summary

    def test_no_db_path_returns_fail(self, ctx):
        ctx2 = DomainContext(
            user_id=ctx.user_id, household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
            oauth_manager=None, storage_adapter=None, file_router=None,
            member_registry=None, file_search=None,
            request_id="", channel="telegram", local_db_path="",
        )
        result = RoutineListTool(context=ctx2)._run()
        assert not result.success
