"""Unit tests for mithra/scheduler/task_scheduler.py.

All tests use an in-memory SQLite database seeded with the exact schema from
local.py DDL. No Telegram calls are made — _send_telegram is patched out.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from mithra.scheduler.task_scheduler import (
    _already_sent_today,
    _get_household_config,
    _get_household_members,
    _in_n_days,
    _record_sent,
    _today,
    job_auto_promote,
    job_deadline_alerts,
    job_morning_briefing,
    job_waiting_followup,
    job_weekly_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HOUSEHOLD_ID = "hh_test"
USER_A = "usr_a"
USER_B = "usr_b"
CHAT_A = "111111"
CHAT_B = "222222"
TZ = "America/Chicago"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_log (
    id           TEXT PRIMARY KEY,
    alert_type   TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    sent_to      TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    payload_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduler_log_daily
    ON scheduler_log(alert_type, reference_id, sent_to, DATE(sent_at));

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    title         TEXT NOT NULL,
    priority      TEXT DEFAULT 'normal',
    status        TEXT DEFAULT 'inbox',
    due_date      TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role         TEXT DEFAULT 'member',
    status       TEXT DEFAULT 'active',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_channels (
    user_id      TEXT NOT NULL,
    household_id TEXT NOT NULL,
    channel      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    verified     INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, channel)
);

CREATE TABLE IF NOT EXISTS household_config (
    household_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (household_id, key)
);
"""


@pytest.fixture()
def db():
    """In-memory SQLite DB with full scheduler schema."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()

    # Household config
    conn.execute(
        "INSERT INTO household_config VALUES (?,?,?,?)",
        (HOUSEHOLD_ID, "timezone", TZ, now),
    )

    # Two active members with Telegram channels
    for uid, name, cid in [(USER_A, "Abhi", CHAT_A), (USER_B, "Wife", CHAT_B)]:
        conn.execute(
            "INSERT INTO members VALUES (?,?,?,?,?,?,?)",
            (uid, HOUSEHOLD_ID, name, "member", "active", now, now),
        )
        conn.execute(
            "INSERT INTO member_channels VALUES (?,?,?,?,?,?)",
            (uid, HOUSEHOLD_ID, "telegram", cid, 1, now),
        )

    conn.commit()
    yield conn
    conn.close()


@pytest.fixture()
def state(db, tmp_path):
    """Minimal _state dict wiring a real temp DB file for job tests."""
    db_file = str(tmp_path / "test.db")
    # Write in-memory schema to a real file so jobs can open it
    file_conn = sqlite3.connect(db_file)
    file_conn.executescript(_SCHEMA)
    now = datetime.now(timezone.utc).isoformat()
    file_conn.execute("INSERT INTO household_config VALUES (?,?,?,?)", (HOUSEHOLD_ID, "timezone", TZ, now))
    for uid, name, cid in [(USER_A, "Abhi", CHAT_A), (USER_B, "Wife", CHAT_B)]:
        file_conn.execute("INSERT INTO members VALUES (?,?,?,?,?,?,?)", (uid, HOUSEHOLD_ID, name, "member", "active", now, now))
        file_conn.execute("INSERT INTO member_channels VALUES (?,?,?,?,?,?)", (uid, HOUSEHOLD_ID, "telegram", cid, 1, now))
    file_conn.commit()
    file_conn.close()

    local_adapter = MagicMock()
    local_adapter.db_path = db_file
    return {
        "local_adapter": local_adapter,
        "household_id": HOUSEHOLD_ID,
        "telegram_bot_token": "tok123",
        "admin_telegram_id": "admin999",
        "household_timezone": TZ,
    }


def _seed_task(conn, task_id=None, title="Test task", priority="normal",
               status="inbox", due_date=None, updated_at=None):
    now = updated_at or datetime.now(timezone.utc).isoformat()
    tid = task_id or ("tsk_" + uuid.uuid4().hex[:8])
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
        (tid, HOUSEHOLD_ID, USER_A, title, priority, status, due_date, now),
    )
    conn.commit()
    return tid


def _seed_project(conn, project_id=None, title="Test project", status="active"):
    now = datetime.now(timezone.utc).isoformat()
    pid = project_id or ("prj_" + uuid.uuid4().hex[:8])
    conn.execute(
        "INSERT INTO projects VALUES (?,?,?,?,?,?,?)",
        (pid, HOUSEHOLD_ID, USER_A, title, status, now, now),
    )
    conn.commit()
    return pid


def _seed_task_in_state(state, **kwargs):
    """Seed a task into the state's DB file."""
    conn = sqlite3.connect(state["local_adapter"].db_path)
    now = kwargs.pop("updated_at", datetime.now(timezone.utc).isoformat())
    tid = kwargs.pop("task_id", "tsk_" + uuid.uuid4().hex[:8])
    title = kwargs.pop("title", "Test task")
    priority = kwargs.pop("priority", "normal")
    status = kwargs.pop("status", "inbox")
    due_date = kwargs.pop("due_date", None)
    conn.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)",
        (tid, HOUSEHOLD_ID, USER_A, title, priority, status, due_date, now),
    )
    conn.commit()
    conn.close()
    return tid


def _seed_project_in_state(state, title="Active project", status="active"):
    conn = sqlite3.connect(state["local_adapter"].db_path)
    now = datetime.now(timezone.utc).isoformat()
    pid = "prj_" + uuid.uuid4().hex[:8]
    conn.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?)", (pid, HOUSEHOLD_ID, USER_A, title, status, now, now))
    conn.commit()
    conn.close()
    return pid


# ---------------------------------------------------------------------------
# _already_sent_today / _record_sent
# ---------------------------------------------------------------------------

class TestDuplicatePrevention:
    def test_not_sent_initially(self, db):
        assert _already_sent_today(db, "morning_briefing", "daily", USER_A) is False

    def test_sent_after_record(self, db):
        _record_sent(db, "morning_briefing", "daily", USER_A)
        assert _already_sent_today(db, "morning_briefing", "daily", USER_A) is True

    def test_different_user_not_blocked(self, db):
        _record_sent(db, "morning_briefing", "daily", USER_A)
        assert _already_sent_today(db, "morning_briefing", "daily", USER_B) is False

    def test_different_alert_type_not_blocked(self, db):
        _record_sent(db, "morning_briefing", "daily", USER_A)
        assert _already_sent_today(db, "weekly_summary", "daily", USER_A) is False

    def test_different_reference_id_not_blocked(self, db):
        _record_sent(db, "deadline_alert", "tsk_001", USER_A)
        assert _already_sent_today(db, "deadline_alert", "tsk_002", USER_A) is False

    def test_record_sent_idempotent(self, db):
        # Second call with same args should not raise — INSERT OR IGNORE
        _record_sent(db, "morning_briefing", "daily", USER_A)
        _record_sent(db, "morning_briefing", "daily", USER_A)
        count = db.execute("SELECT COUNT(*) FROM scheduler_log WHERE alert_type='morning_briefing'").fetchone()[0]
        assert count == 1

    def test_record_sent_stores_payload(self, db):
        _record_sent(db, "deadline_alert", "tsk_x", USER_A, {"title": "Fix fan"})
        row = db.execute("SELECT payload_json FROM scheduler_log WHERE reference_id='tsk_x'").fetchone()
        import json
        assert json.loads(row[0])["title"] == "Fix fan"

    def test_record_sent_id_prefix(self, db):
        _record_sent(db, "morning_briefing", "daily", USER_A)
        row = db.execute("SELECT id FROM scheduler_log LIMIT 1").fetchone()
        assert row[0].startswith("sl_")


# ---------------------------------------------------------------------------
# _get_household_members
# ---------------------------------------------------------------------------

class TestGetHouseholdMembers:
    def test_returns_both_members(self, db):
        members = _get_household_members(db, HOUSEHOLD_ID)
        assert len(members) == 2

    def test_returns_chat_id(self, db):
        members = _get_household_members(db, HOUSEHOLD_ID)
        chat_ids = {m["chat_id"] for m in members}
        assert CHAT_A in chat_ids and CHAT_B in chat_ids

    def test_skips_inactive_member(self, db):
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO members VALUES (?,?,?,?,?,?,?)",
            ("usr_inactive", HOUSEHOLD_ID, "Gone", "member", "inactive", now, now),
        )
        db.execute(
            "INSERT INTO member_channels VALUES (?,?,?,?,?,?)",
            ("usr_inactive", HOUSEHOLD_ID, "telegram", "999999", 1, now),
        )
        db.commit()
        members = _get_household_members(db, HOUSEHOLD_ID)
        assert all(m["user_id"] != "usr_inactive" for m in members)

    def test_skips_member_without_telegram(self, db):
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT INTO members VALUES (?,?,?,?,?,?,?)",
            ("usr_notg", HOUSEHOLD_ID, "NoTelegram", "member", "active", now, now),
        )
        db.commit()
        members = _get_household_members(db, HOUSEHOLD_ID)
        assert all(m["user_id"] != "usr_notg" for m in members)

    def test_wrong_household_returns_empty(self, db):
        members = _get_household_members(db, "hh_other")
        assert members == []


# ---------------------------------------------------------------------------
# _today / _in_n_days
# ---------------------------------------------------------------------------

class TestDateHelpers:
    def test_today_returns_iso_date(self):
        result = _today("America/Chicago")
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"

    def test_in_n_days_offset(self):
        today = _today("America/Chicago")
        tomorrow = _in_n_days("America/Chicago", 1)
        assert tomorrow > today

    def test_in_n_days_7(self):
        today_dt = date.fromisoformat(_today("America/Chicago"))
        in_7_dt = date.fromisoformat(_in_n_days("America/Chicago", 7))
        assert (in_7_dt - today_dt).days == 7


# ---------------------------------------------------------------------------
# job_morning_briefing
# ---------------------------------------------------------------------------

class TestJobMorningBriefing:
    def test_sends_positive_message_when_no_tasks(self, state):
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_morning_briefing(state)
        # Two members → two sends
        assert mock_send.call_count == 2
        for call in mock_send.call_args_list:
            assert "No urgent tasks" in call.args[2]

    def test_sends_briefing_with_urgent_tasks(self, state):
        _seed_task_in_state(state, title="Fix leak", priority="urgent", status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_morning_briefing(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("Fix leak" in t for t in texts)
        assert any("Morning briefing" in t for t in texts)

    def test_sends_briefing_with_overdue_tasks(self, state):
        old_date = (date.today() - timedelta(days=2)).isoformat()
        _seed_task_in_state(state, title="Old task", due_date=old_date, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_morning_briefing(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("Old task" in t for t in texts)

    def test_no_duplicate_on_second_run(self, state):
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_morning_briefing(state)
            job_morning_briefing(state)
        # Second run should send 0 — already recorded in scheduler_log
        assert mock_send.call_count == 2  # only the first run's 2 sends

    def test_does_not_crash_when_db_unavailable(self):
        bad_state = {
            "local_adapter": MagicMock(db_path="/nonexistent/path/to.db"),
            "household_id": HOUSEHOLD_ID,
            "telegram_bot_token": "tok",
            "admin_telegram_id": "",
        }
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=False):
            # Should not raise — wrapped in try/except
            job_morning_briefing(bad_state)


# ---------------------------------------------------------------------------
# job_deadline_alerts
# ---------------------------------------------------------------------------

class TestJobDeadlineAlerts:
    def test_promotes_task_due_in_2_days(self, state):
        due = _in_n_days(TZ, 2)
        tid = _seed_task_in_state(state, title="Warranty expires", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True):
            job_deadline_alerts(state)
        conn = sqlite3.connect(state["local_adapter"].db_path)
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "urgent"

    def test_sends_alert_per_member(self, state):
        due = _in_n_days(TZ, 2)
        _seed_task_in_state(state, title="Deadline task", priority="high", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_deadline_alerts(state)
        assert mock_send.call_count == 2  # one per member

    def test_does_not_promote_already_urgent(self, state):
        due = _in_n_days(TZ, 1)
        tid = _seed_task_in_state(state, title="Already urgent", priority="urgent", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_deadline_alerts(state)
        # No tasks found → no sends
        assert mock_send.call_count == 0

    def test_no_duplicate_alert_same_day(self, state):
        due = _in_n_days(TZ, 2)
        _seed_task_in_state(state, title="Task X", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_deadline_alerts(state)
            job_deadline_alerts(state)
        # Second run: task is now 'urgent' so no match; original sends were 2
        assert mock_send.call_count == 2

    def test_tomorrow_shows_correct_word(self, state):
        due = _in_n_days(TZ, 1)
        _seed_task_in_state(state, title="Due tomorrow", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_deadline_alerts(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("tomorrow" in t for t in texts)


# ---------------------------------------------------------------------------
# job_waiting_followup
# ---------------------------------------------------------------------------

class TestJobWaitingFollowup:
    def test_resurfaces_old_waiting_task(self, state):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _seed_task_in_state(state, title="Waiting for Amazon", status="waiting", updated_at=old_ts)
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_waiting_followup(state)
        assert mock_send.call_count == 2  # one per member
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("Waiting for Amazon" in t for t in texts)

    def test_does_not_resurface_recently_updated_task(self, state):
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        _seed_task_in_state(state, title="Recent waiting", status="waiting", updated_at=recent_ts)
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_waiting_followup(state)
        assert mock_send.call_count == 0

    def test_does_not_resurface_after_recent_followup(self, state):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        tid = _seed_task_in_state(state, title="Already followed up", status="waiting", updated_at=old_ts)
        # Seed a recent followup log entry
        conn = sqlite3.connect(state["local_adapter"].db_path)
        recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO scheduler_log VALUES (?,?,?,?,?,?)",
            ("sl_existing", "waiting_followup", tid, USER_A, recent, "{}"),
        )
        conn.commit()
        conn.close()
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_waiting_followup(state)
        # USER_A excluded by log; USER_B still gets it but since NOT EXISTS fires on task level...
        # The NOT EXISTS checks all sent_at globally for that task_id, not per-user
        assert mock_send.call_count == 0

    def test_skips_non_waiting_tasks(self, state):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        _seed_task_in_state(state, title="Inbox task", status="inbox", updated_at=old_ts)
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_waiting_followup(state)
        assert mock_send.call_count == 0


# ---------------------------------------------------------------------------
# job_weekly_summary
# ---------------------------------------------------------------------------

class TestJobWeeklySummary:
    def test_sends_all_clear_when_empty(self, state):
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_weekly_summary(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("All clear" in t for t in texts)

    def test_includes_inbox_count(self, state):
        for i in range(3):
            _seed_task_in_state(state, title=f"Inbox {i}", status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_weekly_summary(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("3 inbox tasks" in t for t in texts)

    def test_includes_project_count(self, state):
        _seed_project_in_state(state, title="Backyard deck")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_weekly_summary(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("project" in t.lower() for t in texts)

    def test_no_duplicate_same_day(self, state):
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_weekly_summary(state)
            job_weekly_summary(state)
        assert mock_send.call_count == 2  # only the first run

    def test_singular_task_grammar(self, state):
        _seed_task_in_state(state, title="Single inbox task", status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_weekly_summary(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("1 inbox task to process" in t for t in texts)


# ---------------------------------------------------------------------------
# job_auto_promote
# ---------------------------------------------------------------------------

class TestJobAutoPromote:
    def test_promotes_normal_task_within_7_days(self, state):
        due = _in_n_days(TZ, 5)
        tid = _seed_task_in_state(state, title="Fix fan", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True):
            job_auto_promote(state)
        conn = sqlite3.connect(state["local_adapter"].db_path)
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "urgent"

    def test_promotes_high_task_within_3_days(self, state):
        due = _in_n_days(TZ, 2)
        tid = _seed_task_in_state(state, title="High priority", priority="high", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True):
            job_auto_promote(state)
        conn = sqlite3.connect(state["local_adapter"].db_path)
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "urgent"

    def test_does_not_promote_high_task_at_6_days(self, state):
        due = _in_n_days(TZ, 6)
        tid = _seed_task_in_state(state, title="High far out", priority="high", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True):
            job_auto_promote(state)
        conn = sqlite3.connect(state["local_adapter"].db_path)
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "high"  # unchanged

    def test_does_not_promote_done_task(self, state):
        due = _in_n_days(TZ, 2)
        tid = _seed_task_in_state(state, title="Done task", priority="normal", due_date=due, status="done")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True):
            job_auto_promote(state)
        conn = sqlite3.connect(state["local_adapter"].db_path)
        row = conn.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row[0] == "normal"  # unchanged

    def test_sends_promotion_notification(self, state):
        due = _in_n_days(TZ, 3)
        _seed_task_in_state(state, title="Return package", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_auto_promote(state)
        texts = [c.args[2] for c in mock_send.call_args_list]
        assert any("Return package" in t for t in texts)
        assert any("promoted to urgent" in t for t in texts)

    def test_no_duplicate_promotion_notification(self, state):
        due = _in_n_days(TZ, 3)
        _seed_task_in_state(state, title="Task once", priority="normal", due_date=due, status="inbox")
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_auto_promote(state)
            job_auto_promote(state)
        # Task is now urgent after first run → not re-promoted; 2 sends only (one per member)
        assert mock_send.call_count == 2

    def test_no_tasks_no_sends(self, state):
        with patch("mithra.scheduler.task_scheduler._send_telegram", return_value=True) as mock_send:
            job_auto_promote(state)
        assert mock_send.call_count == 0
