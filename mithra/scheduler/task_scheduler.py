"""
Task scheduler — proactive surfacing jobs for the Tasks domain.

Design constraints (ADR 11.10):
  - NO LLM calls. All jobs use SQLite queries + string formatting only.
  - All jobs are wrapped in try/except; failures log error + admin alert.
  - Duplicate prevention via scheduler_log UNIQUE INDEX on (type, ref, to, date).
  - Timezone: all "today" comparisons use household timezone, not UTC.
  - Waiting follow-up: only resurface if 7+ days since LAST followup.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytz

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telegram helper — stdlib only, no httpx dependency
# ---------------------------------------------------------------------------

def _publish_notification_event(
    user_id: str,
    chat_id: str,
    text: str,
    household_id: str,
) -> None:
    """Fire-and-forget event bus publish alongside direct Telegram send.

    Does NOT affect scheduler_log recording — that is handled by the caller.
    Allows future subscribers (WhatsApp, push etc.) to also receive notifications.
    No future.result() — never blocks the scheduler thread (LESSONS_LEARNED L-01).
    """
    from mithra.events.bus import get_event_bus, get_main_loop
    from mithra.events.base import NotificationEvent
    import asyncio

    bus = get_event_bus()
    loop = get_main_loop()
    if not bus or not loop:
        return

    try:
        evt = NotificationEvent.create(
            topic="notification.telegram.send",
            household_id=household_id,
            user_id=user_id,
            payload={"chat_id": chat_id, "text": text},
        )
        asyncio.run_coroutine_threadsafe(bus.publish(evt), loop)
    except Exception as exc:
        _logger.warning("_publish_notification_event failed: %s", exc)


def _send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not bot_token or not chat_id:
        return False
    try:
        data = json.dumps(
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        _logger.error("Telegram send failed chat_id=%s: %s", chat_id, exc)
        return False


# ---------------------------------------------------------------------------
# Duplicate prevention
# ---------------------------------------------------------------------------

def _already_sent_today(
    conn: sqlite3.Connection,
    alert_type: str,
    reference_id: str,
    sent_to: str,
) -> bool:
    """Return True if this alert was already sent to this user today."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT 1 FROM scheduler_log "
        "WHERE alert_type=? AND reference_id=? AND sent_to=? AND SUBSTR(sent_at,1,10)=?",
        (alert_type, reference_id, sent_to, today),
    ).fetchone()
    return row is not None


def _record_sent(
    conn: sqlite3.Connection,
    alert_type: str,
    reference_id: str,
    sent_to: str,
    payload: Optional[dict] = None,
) -> None:
    """Record a sent alert; silently ignores duplicate-day conflicts."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO scheduler_log "
            "(id, alert_type, reference_id, sent_to, sent_at, payload_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                "sl_" + uuid.uuid4().hex[:12],
                alert_type,
                reference_id,
                sent_to,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(payload or {}),
            ),
        )
    except sqlite3.IntegrityError:
        pass  # Duplicate on same calendar day — safe to ignore


# ---------------------------------------------------------------------------
# Household helpers
# ---------------------------------------------------------------------------

def _get_household_members(
    conn: sqlite3.Connection, household_id: str
) -> list[dict]:
    """Return active members that have a Telegram chat_id linked."""
    rows = conn.execute(
        """SELECT m.id, m.display_name, mc.channel_id AS chat_id
           FROM members m
           JOIN member_channels mc ON mc.user_id = m.id
           WHERE m.household_id = ?
             AND m.status = 'active'
             AND mc.channel = 'telegram'""",
        (household_id,),
    ).fetchall()
    return [{"user_id": r[0], "name": r[1], "chat_id": r[2]} for r in rows]


def _get_household_config(
    conn: sqlite3.Connection, household_id: str
) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM household_config WHERE household_id=?",
        (household_id,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _today(tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).strftime("%Y-%m-%d")


def _in_n_days(tz_name: str, n: int) -> str:
    tz = pytz.timezone(tz_name)
    return (datetime.now(tz) + timedelta(days=n)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Admin failure notification
# ---------------------------------------------------------------------------

def _notify_admin_of_failure(state: dict, job_name: str, exc: Exception) -> None:
    """Send Telegram alert to admin when a scheduler job fails."""
    bot_token = state.get("telegram_bot_token", "")
    admin_chat_id = state.get("admin_telegram_id", "")
    if not bot_token or not admin_chat_id:
        return
    _send_telegram(
        bot_token,
        admin_chat_id,
        f"⚠️ Scheduler job <b>{job_name}</b> failed:\n"
        f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# JOB 1 — morning_briefing (daily 7am household tz)
# ---------------------------------------------------------------------------

def job_morning_briefing(state: dict) -> None:
    """Send daily task briefing to each household member."""
    db_path = state.get("local_adapter").db_path
    household_id = state.get("household_id", "")
    bot_token = state.get("telegram_bot_token", "")

    try:
        with sqlite3.connect(db_path) as conn:
            config = _get_household_config(conn, household_id)
            tz_name = config.get("timezone", "America/Chicago")
            today = _today(tz_name)
            members = _get_household_members(conn, household_id)

            overdue = conn.execute(
                """SELECT title, due_date FROM tasks
                   WHERE household_id=?
                     AND due_date < ?
                     AND status NOT IN ('done','cancelled','snoozed')
                   ORDER BY due_date ASC LIMIT 5""",
                (household_id, today),
            ).fetchall()

            urgent = conn.execute(
                """SELECT title, due_date FROM tasks
                   WHERE household_id=?
                     AND priority='urgent'
                     AND status NOT IN ('done','cancelled','snoozed')
                   ORDER BY due_date ASC NULLS LAST LIMIT 5""",
                (household_id,),
            ).fetchall()

            if not urgent and not overdue:
                msg = "☀️ Good morning! No urgent tasks today."
            else:
                lines = ["☀️ <b>Morning briefing</b>"]
                if overdue:
                    lines.append(f"\n⚠️ <b>Overdue ({len(overdue)}):</b>")
                    for t in overdue:
                        due = f" (due {t[1]})" if t[1] else ""
                        lines.append(f"• {t[0]}{due}")
                if urgent:
                    lines.append(f"\n🔴 <b>Urgent ({len(urgent)}):</b>")
                    for t in urgent:
                        due = f" (due {t[1]})" if t[1] else ""
                        lines.append(f"• {t[0]}{due}")
                msg = "\n".join(lines)

            for member in members:
                uid = member["user_id"]
                cid = member["chat_id"]
                if _already_sent_today(conn, "morning_briefing", "daily", uid):
                    continue
                if _send_telegram(bot_token, cid, msg):
                    _record_sent(conn, "morning_briefing", "daily", uid)
                    _publish_notification_event(uid, cid, msg, household_id)

    except Exception as exc:
        _logger.error("morning_briefing job failed: %s", exc, exc_info=True)
        _notify_admin_of_failure(state, "morning_briefing", exc)


# ---------------------------------------------------------------------------
# JOB 2 — deadline_alerts (daily 8am household tz)
# ---------------------------------------------------------------------------

def job_deadline_alerts(state: dict) -> None:
    """Alert members about tasks due within 3 days and auto-promote to urgent."""
    db_path = state.get("local_adapter").db_path
    household_id = state.get("household_id", "")
    bot_token = state.get("telegram_bot_token", "")

    try:
        with sqlite3.connect(db_path) as conn:
            config = _get_household_config(conn, household_id)
            tz_name = config.get("timezone", "America/Chicago")
            today = _today(tz_name)
            in_3 = _in_n_days(tz_name, 3)
            now_iso = datetime.now(timezone.utc).isoformat()

            # Find tasks due within 3 days not already urgent
            tasks = conn.execute(
                """SELECT id, title, due_date, priority
                   FROM tasks
                   WHERE household_id=?
                     AND due_date BETWEEN ? AND ?
                     AND priority != 'urgent'
                     AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id, today, in_3),
            ).fetchall()

            if not tasks:
                return

            members = _get_household_members(conn, household_id)

            for task in tasks:
                task_id, title, due_date, _ = task[0], task[1], task[2], task[3]

                # Auto-promote to urgent
                conn.execute(
                    "UPDATE tasks SET priority='urgent', updated_at=? WHERE id=?",
                    (now_iso, task_id),
                )

                days_left = (
                    date.fromisoformat(due_date) - date.fromisoformat(today)
                ).days
                day_word = (
                    "tomorrow" if days_left == 1
                    else "today" if days_left == 0
                    else f"in {days_left} days"
                )
                msg = (
                    f"⚠️ <b>Deadline approaching</b>\n"
                    f"{title} — due {day_word}\n"
                    f"Moved to urgent."
                )

                for member in members:
                    uid = member["user_id"]
                    cid = member["chat_id"]
                    if _already_sent_today(conn, "deadline_alert", task_id, uid):
                        continue
                    if _send_telegram(bot_token, cid, msg):
                        _record_sent(
                            conn, "deadline_alert", task_id, uid,
                            {"title": title, "due_date": due_date},
                        )
                        _publish_notification_event(uid, cid, msg, household_id)

    except Exception as exc:
        _logger.error("deadline_alerts job failed: %s", exc, exc_info=True)
        _notify_admin_of_failure(state, "deadline_alerts", exc)


# ---------------------------------------------------------------------------
# JOB 3 — waiting_followup (daily 9am household tz)
# ---------------------------------------------------------------------------

def job_waiting_followup(state: dict) -> None:
    """Resurface waiting tasks that haven't had a follow-up in 7+ days."""
    db_path = state.get("local_adapter").db_path
    household_id = state.get("household_id", "")
    bot_token = state.get("telegram_bot_token", "")

    try:
        with sqlite3.connect(db_path) as conn:
            config = _get_household_config(conn, household_id)
            tz_name = config.get("timezone", "America/Chicago")
            now_utc = datetime.now(timezone.utc)
            cutoff = (now_utc - timedelta(days=7)).isoformat()

            # Waiting tasks with no followup in the past 7 days
            tasks = conn.execute(
                """SELECT t.id, t.title, t.updated_at
                   FROM tasks t
                   WHERE t.household_id=?
                     AND t.status='waiting'
                     AND t.updated_at < ?
                     AND NOT EXISTS (
                         SELECT 1 FROM scheduler_log sl
                         WHERE sl.alert_type='waiting_followup'
                           AND sl.reference_id=t.id
                           AND sl.sent_at > ?
                     )""",
                (household_id, cutoff, cutoff),
            ).fetchall()

            if not tasks:
                return

            members = _get_household_members(conn, household_id)

            for task in tasks:
                task_id, title = task[0], task[1]
                msg = (
                    f"⏳ <b>Waiting task check-in</b>\n"
                    f"{title}\n"
                    f"Any update? Still waiting?"
                )
                for member in members:
                    uid = member["user_id"]
                    cid = member["chat_id"]
                    if _send_telegram(bot_token, cid, msg):
                        _record_sent(
                            conn, "waiting_followup", task_id, uid,
                            {"title": title},
                        )
                        _publish_notification_event(uid, cid, msg, household_id)

    except Exception as exc:
        _logger.error("waiting_followup job failed: %s", exc, exc_info=True)
        _notify_admin_of_failure(state, "waiting_followup", exc)


# ---------------------------------------------------------------------------
# JOB 4 — weekly_summary (Sunday 9am household tz)
# ---------------------------------------------------------------------------

def job_weekly_summary(state: dict) -> None:
    """Sunday morning: inbox count, overdue, urgent, active projects."""
    db_path = state.get("local_adapter").db_path
    household_id = state.get("household_id", "")
    bot_token = state.get("telegram_bot_token", "")

    try:
        with sqlite3.connect(db_path) as conn:
            config = _get_household_config(conn, household_id)
            tz_name = config.get("timezone", "America/Chicago")
            today = _today(tz_name)
            members = _get_household_members(conn, household_id)

            inbox_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND status='inbox'",
                (household_id,),
            ).fetchone()[0]

            overdue_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND due_date<? "
                "AND status NOT IN ('done','cancelled','snoozed')",
                (household_id, today),
            ).fetchone()[0]

            urgent_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE household_id=? AND priority='urgent' "
                "AND status NOT IN ('done','cancelled','snoozed')",
                (household_id,),
            ).fetchone()[0]

            active_projects = conn.execute(
                "SELECT COUNT(*) FROM projects WHERE household_id=? AND status='active'",
                (household_id,),
            ).fetchone()[0]

            parts = ["📋 <b>Weekly summary</b>"]
            if inbox_count:
                parts.append(f"• {inbox_count} inbox task{'s' if inbox_count != 1 else ''} to process")
            if overdue_count:
                parts.append(f"• {overdue_count} overdue task{'s' if overdue_count != 1 else ''}")
            if urgent_count:
                parts.append(f"• {urgent_count} urgent task{'s' if urgent_count != 1 else ''}")
            if active_projects:
                parts.append(f"• {active_projects} active project{'s' if active_projects != 1 else ''}")
            if len(parts) == 1:
                parts.append("All clear! Great week ✓")

            msg = "\n".join(parts)

            for member in members:
                uid = member["user_id"]
                cid = member["chat_id"]
                if _already_sent_today(conn, "weekly_summary", "weekly", uid):
                    continue
                if _send_telegram(bot_token, cid, msg):
                    _record_sent(conn, "weekly_summary", "weekly", uid)
                    _publish_notification_event(uid, cid, msg, household_id)

    except Exception as exc:
        _logger.error("weekly_summary job failed: %s", exc, exc_info=True)
        _notify_admin_of_failure(state, "weekly_summary", exc)


# ---------------------------------------------------------------------------
# JOB 5 — auto_promote (daily 6am household tz, runs before briefing)
# ---------------------------------------------------------------------------

def job_auto_promote(state: dict) -> None:
    """Auto-promote tasks near their deadline before the morning briefing.

    Normal → urgent when ≤7 days remaining.
    High → urgent when ≤3 days remaining.
    Sends one Telegram notification per promoted task per day.
    """
    db_path = state.get("local_adapter").db_path
    household_id = state.get("household_id", "")
    bot_token = state.get("telegram_bot_token", "")

    try:
        with sqlite3.connect(db_path) as conn:
            config = _get_household_config(conn, household_id)
            tz_name = config.get("timezone", "America/Chicago")
            today = _today(tz_name)
            in_7 = _in_n_days(tz_name, 7)
            in_3 = _in_n_days(tz_name, 3)
            now_iso = datetime.now(timezone.utc).isoformat()
            members = _get_household_members(conn, household_id)

            # Select tasks to promote (normal priority, ≤7 days)
            promote_normal = conn.execute(
                """SELECT id, title, due_date FROM tasks
                   WHERE household_id=?
                     AND priority='normal'
                     AND due_date BETWEEN ? AND ?
                     AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id, today, in_7),
            ).fetchall()

            # Select tasks to promote (high priority, ≤3 days)
            promote_high = conn.execute(
                """SELECT id, title, due_date FROM tasks
                   WHERE household_id=?
                     AND priority='high'
                     AND due_date BETWEEN ? AND ?
                     AND status NOT IN ('done','cancelled','snoozed')""",
                (household_id, today, in_3),
            ).fetchall()

            all_to_promote = list(promote_normal) + list(promote_high)
            if not all_to_promote:
                return

            # Batch-promote all selected tasks
            ids_normal = [t[0] for t in promote_normal]
            ids_high = [t[0] for t in promote_high]
            if ids_normal:
                conn.execute(
                    f"UPDATE tasks SET priority='urgent', updated_at=? "
                    f"WHERE id IN ({','.join('?' * len(ids_normal))})",
                    [now_iso] + ids_normal,
                )
            if ids_high:
                conn.execute(
                    f"UPDATE tasks SET priority='urgent', updated_at=? "
                    f"WHERE id IN ({','.join('?' * len(ids_high))})",
                    [now_iso] + ids_high,
                )

            for task in all_to_promote:
                task_id, title, due_date = task[0], task[1], task[2]
                msg = (
                    f"⚠️ <b>Task promoted to urgent</b>\n"
                    f"{title} — due {due_date}"
                )
                for member in members:
                    uid = member["user_id"]
                    cid = member["chat_id"]
                    if _already_sent_today(conn, "auto_promotion", task_id, uid):
                        continue
                    if _send_telegram(bot_token, cid, msg):
                        _record_sent(
                            conn, "auto_promotion", task_id, uid,
                            {"title": title, "due_date": due_date},
                        )
                        _publish_notification_event(uid, cid, msg, household_id)

    except Exception as exc:
        _logger.error("auto_promote job failed: %s", exc, exc_info=True)
        _notify_admin_of_failure(state, "auto_promote", exc)
