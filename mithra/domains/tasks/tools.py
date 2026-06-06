"""Tasks domain — classification helpers and ID generators.

These are pure functions with no I/O. They are called by task tools
to classify scope and priority at capture time, and by the proactive
surfacing scheduler to auto-promote tasks near their deadlines.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

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
