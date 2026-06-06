"""Unit tests for mithra/domains/tasks/tools.py — T2 classification helpers."""

from datetime import date, timedelta

import pytest

from mithra.domains.tasks.tools import (
    classify_task_priority,
    classify_task_scope,
    new_milestone_id,
    new_project_id,
    new_routine_id,
    new_task_id,
    now_iso,
    should_auto_promote,
)


# ---------------------------------------------------------------------------
# classify_task_scope
# ---------------------------------------------------------------------------

class TestClassifyTaskScope:
    def test_personal_by_default(self):
        assert classify_task_scope("fix ceiling fan") == "personal"

    def test_personal_no_keywords(self):
        assert classify_task_scope("call insurance") == "personal"

    def test_household_trash(self):
        assert classify_task_scope("take out trash") == "household"

    def test_household_baby(self):
        assert classify_task_scope("mix baby formula") == "household"

    def test_household_keyword_in_user_text(self):
        assert classify_task_scope("call insurance", "for the whole family") == "household"

    def test_household_pest(self):
        assert classify_task_scope("schedule pest control") == "household"

    def test_household_rent(self):
        assert classify_task_scope("pay rent this month") == "household"

    def test_household_kids(self):
        assert classify_task_scope("school dropoff reminder") == "household"

    def test_household_utilities(self):
        assert classify_task_scope("pay utilities bill") == "household"

    def test_household_explicit_keyword(self):
        assert classify_task_scope("household maintenance check") == "household"

    def test_case_insensitive(self):
        assert classify_task_scope("TRASH day tomorrow") == "household"

    def test_empty_title_personal(self):
        assert classify_task_scope("") == "personal"

    def test_yard_household(self):
        assert classify_task_scope("mow the yard this weekend") == "household"

    def test_personal_work_task(self):
        assert classify_task_scope("finish quarterly report") == "personal"

    def test_both_keyword(self):
        assert classify_task_scope("buy tickets for both of us") == "household"


# ---------------------------------------------------------------------------
# classify_task_priority
# ---------------------------------------------------------------------------

class TestClassifyTaskPriority:
    def test_normal_default(self):
        assert classify_task_priority("fix ceiling fan") == "normal"

    def test_urgent_keyword(self):
        assert classify_task_priority("urgent fix the leak") == "urgent"

    def test_asap_keyword(self):
        assert classify_task_priority("call doctor asap") == "urgent"

    def test_today_keyword(self):
        assert classify_task_priority("submit form today") == "urgent"

    def test_tonight_keyword(self):
        assert classify_task_priority("dinner prep tonight") == "urgent"

    def test_emergency_keyword(self):
        assert classify_task_priority("emergency plumber call") == "urgent"

    def test_someday_keyword(self):
        assert classify_task_priority("someday learn woodworking") == "someday"

    def test_eventually_keyword(self):
        assert classify_task_priority("eventually repaint the fence") == "someday"

    def test_maybe_keyword(self):
        assert classify_task_priority("maybe get a NAS") == "someday"

    def test_due_date_within_7_days_urgent(self):
        due = (date.today() + timedelta(days=5)).isoformat()
        assert classify_task_priority("fix fan", due_date=due) == "urgent"

    def test_due_date_today_urgent(self):
        due = date.today().isoformat()
        assert classify_task_priority("submit form", due_date=due) == "urgent"

    def test_due_date_within_30_days_high(self):
        due = (date.today() + timedelta(days=20)).isoformat()
        assert classify_task_priority("install light", due_date=due) == "high"

    def test_due_date_far_future_normal(self):
        due = (date.today() + timedelta(days=90)).isoformat()
        assert classify_task_priority("repaint shed", due_date=due) == "normal"

    def test_invalid_due_date_ignored(self):
        assert classify_task_priority("fix fan", due_date="not-a-date") == "normal"

    def test_keyword_takes_precedence_over_due_date(self):
        due = (date.today() + timedelta(days=20)).isoformat()
        assert classify_task_priority("urgent fix this", due_date=due) == "urgent"

    def test_user_text_urgent(self):
        assert classify_task_priority("call supplier", "this is urgent") == "urgent"


# ---------------------------------------------------------------------------
# should_auto_promote
# ---------------------------------------------------------------------------

class TestShouldAutoPromote:
    def test_no_due_date_returns_none(self):
        assert should_auto_promote("normal", None) is None

    def test_already_urgent_returns_none(self):
        due = (date.today() + timedelta(days=3)).isoformat()
        assert should_auto_promote("urgent", due) is None

    def test_normal_within_7_days_promotes(self):
        due = (date.today() + timedelta(days=5)).isoformat()
        assert should_auto_promote("normal", due) == "urgent"

    def test_normal_at_7_days_promotes(self):
        due = (date.today() + timedelta(days=7)).isoformat()
        assert should_auto_promote("normal", due) == "urgent"

    def test_normal_at_8_days_no_promote(self):
        due = (date.today() + timedelta(days=8)).isoformat()
        assert should_auto_promote("normal", due) is None

    def test_high_within_3_days_promotes(self):
        due = (date.today() + timedelta(days=2)).isoformat()
        assert should_auto_promote("high", due) == "urgent"

    def test_high_at_3_days_promotes(self):
        due = (date.today() + timedelta(days=3)).isoformat()
        assert should_auto_promote("high", due) == "urgent"

    def test_high_at_4_days_no_promote(self):
        due = (date.today() + timedelta(days=4)).isoformat()
        assert should_auto_promote("high", due) is None

    def test_someday_within_7_days_no_promote(self):
        due = (date.today() + timedelta(days=3)).isoformat()
        assert should_auto_promote("someday", due) is None

    def test_invalid_due_date_returns_none(self):
        assert should_auto_promote("normal", "not-a-date") is None


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

class TestIdGenerators:
    def test_task_id_prefix(self):
        assert new_task_id().startswith("tsk_")

    def test_task_id_unique(self):
        assert new_task_id() != new_task_id()

    def test_task_id_length(self):
        assert len(new_task_id()) == 16  # "tsk_" + 12 hex chars

    def test_project_id_prefix(self):
        assert new_project_id().startswith("prj_")

    def test_project_id_unique(self):
        assert new_project_id() != new_project_id()

    def test_milestone_id_prefix(self):
        assert new_milestone_id().startswith("mst_")

    def test_milestone_id_unique(self):
        assert new_milestone_id() != new_milestone_id()

    def test_routine_id_prefix(self):
        assert new_routine_id().startswith("rtn_")

    def test_routine_id_unique(self):
        assert new_routine_id() != new_routine_id()

    def test_now_iso_is_string(self):
        ts = now_iso()
        assert isinstance(ts, str)
        assert "T" in ts  # ISO 8601 format


# ---------------------------------------------------------------------------
# Schema smoke test — verify all 5 tables created in a fresh SQLite DB
# ---------------------------------------------------------------------------

class TestTasksSchema:
    def test_all_five_tables_created(self, tmp_path):
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter

        adapter = LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        for expected in (
            "tasks", "projects", "milestones",
            "routines", "routine_completions",
        ):
            assert expected in tables, f"Missing table: {expected}"

    def test_tasks_indexes_created(self, tmp_path):
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        conn.close()
        for expected in (
            "idx_tasks_household", "idx_tasks_user",
            "idx_tasks_due", "idx_tasks_priority",
            "idx_routine_day",
        ):
            assert expected in indexes, f"Missing index: {expected}"

    def test_tasks_table_columns(self, tmp_path):
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)")
        }
        conn.close()
        for expected in (
            "id", "household_id", "user_id", "title", "notes",
            "scope", "priority", "status", "due_date",
            "project_id", "milestone_id", "snoozed_until",
            "source", "source_ref", "created_at", "updated_at",
            "completed_at",
        ):
            assert expected in cols, f"Missing column in tasks: {expected}"

    def test_routines_table_columns(self, tmp_path):
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(routines)")
        }
        conn.close()
        for expected in ("id", "household_id", "title", "scope", "reset_time", "active", "created_at"):
            assert expected in cols, f"Missing column in routines: {expected}"

    def test_routine_completions_unique_index(self, tmp_path):
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(
            "INSERT INTO routines VALUES (?,?,?,?,?,?,?)",
            ("rtn_1", "hh_1", "Baby formula", "household", "00:00", 1, "2026-06-06T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO routine_completions VALUES (?,?,?,?,?,?)",
            ("rc_1", "rtn_1", "hh_1", "usr_1", "2026-06-06", "2026-06-06T08:00:00+00:00"),
        )
        conn.commit()
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO routine_completions VALUES (?,?,?,?,?,?)",
                ("rc_2", "rtn_1", "hh_1", "usr_2", "2026-06-06", "2026-06-06T09:00:00+00:00"),
            )
            conn.commit()
        conn.close()
