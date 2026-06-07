"""Unit tests for mithra/domains/tasks/tools.py — T3 tool classes."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mithra.domains.context import DomainContext
from mithra.domains.tasks.tools import (
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


# ---------------------------------------------------------------------------
# build_tasks_tools factory
# ---------------------------------------------------------------------------

class TestBuildTasksTools:
    def test_returns_7_bundles(self, ctx):
        bundles = build_tasks_tools(ctx)
        assert len(bundles) == 7

    def test_tool_names(self, ctx):
        names = {b.metadata.name for b in build_tasks_tools(ctx)}
        expected = {
            "task_create", "task_list", "task_complete",
            "task_update", "task_snooze", "task_delete", "task_suggest",
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
