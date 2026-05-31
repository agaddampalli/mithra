"""Time domain tools — 8 calendar tools wired to GoogleCalendarService.

All tools are LangChain-compatible (BaseTool subclass with Pydantic args_schema).
Each tool is a closure over a DomainContext — created fresh per request via
TimeDomain.get_tools(context).

Time parsing contract:
  Tools receive raw natural-language time strings from the LLM.
  Tools call parse_time() internally — never ask LLM for ISO8601.
  If parse_time() fails → ToolResult.fail with UNPARSEABLE_DATE.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from mithra.domains.bundle import PermissionTier, ToolBundle, ToolMetadata
from mithra.domains.context import DomainContext
from mithra.domains.result import DomainEvent, ToolResult
from mithra.domains.time.service import GoogleCalendarService

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility — time parsing
# ---------------------------------------------------------------------------

_DAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


def parse_time(
    text: str,
    timezone_name: str,
    prefer_future: bool = True,
    base: datetime | None = None,
) -> datetime | None:
    """Convert natural language time string to timezone-aware datetime.

    Deterministic given same input + timezone. Uses dateparser.
    Returns None if the string cannot be understood.

    Retry strategy: dateparser does not support "next <day>" phrases.
    When the first parse fails and the text contains "next <day>", strip
    the "next" prefix and retry (PREFER_DATES_FROM=future keeps it future).
    """
    import dateparser
    import re

    settings: dict = {
        "TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future" if prefer_future else "past",
        "PREFER_DAY_OF_MONTH": "first",
        "TO_TIMEZONE": timezone_name,
    }
    if base is not None:
        settings["RELATIVE_BASE"] = base

    def _fix_tz(dt: datetime | None) -> datetime | None:
        """Re-localize to household tz when dateparser returns UTC for local input.

        dateparser 1.4.0 ignores TO_TIMEZONE for relative expressions like
        "next Tuesday 3pm", returning the time-of-day anchored to UTC instead
        of the household timezone. Detect this and re-interpret the hour as
        local time so "3pm" → 15:00 CST, not 15:00 UTC (= 10:00 CST).
        """
        import pytz as _pytz
        if dt is None:
            return None
        if dt.tzinfo is None:
            return _pytz.timezone(timezone_name).localize(dt)
        offset_sec = dt.utcoffset().total_seconds() if dt.utcoffset() is not None else None
        if offset_sec == 0:
            tz = _pytz.timezone(timezone_name)
            tz_offset_sec = datetime.now(tz).utcoffset().total_seconds()
            if tz_offset_sec != 0:
                # Household is non-UTC: strip UTC and re-localize the hour-of-day
                naive = dt.replace(tzinfo=None)
                return tz.localize(naive)
        return dt

    try:
        result = dateparser.parse(text, settings=settings)
        if result is not None:
            return _fix_tz(result)
    except Exception as exc:
        _logger.debug("parse_time primary attempt failed for %r: %s", text, exc)

    # Retry: strip "next" prefix before a day name — dateparser handles bare
    # day names with PREFER_DATES_FROM=future, but not "next <day>" phrases.
    text_lower = text.lower()
    if "next" in text_lower:
        for day in _DAY_NAMES:
            pattern = r"\bnext\s+" + day + r"\b"
            if re.search(pattern, text_lower):
                modified = re.sub(pattern, day, text_lower, count=1)
                try:
                    result = dateparser.parse(modified, settings=settings)
                    if result is not None:
                        return _fix_tz(result)
                except Exception as exc:
                    _logger.debug("parse_time retry failed for %r: %s", modified, exc)

    _logger.warning("Could not parse datetime: %r", text)
    return None


def _range_from_period(period: str, tz_name: str) -> tuple[datetime, datetime]:
    """Convert a named period to a (start, end) datetime pair."""
    import pytz
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if period in ("today",):
        return today_start, today_start + timedelta(days=1)
    if period in ("tomorrow",):
        d = today_start + timedelta(days=1)
        return d, d + timedelta(days=1)
    if period in ("this week", "week"):
        # Monday of current week → Sunday end
        monday = today_start - timedelta(days=today_start.weekday())
        return monday, monday + timedelta(days=7)
    if period in ("next week",):
        monday = today_start - timedelta(days=today_start.weekday()) + timedelta(weeks=1)
        return monday, monday + timedelta(days=7)
    if period in ("this month", "month"):
        month_start = today_start.replace(day=1)
        if now.month == 12:
            month_end = month_start.replace(year=now.year + 1, month=1)
        else:
            month_end = month_start.replace(month=now.month + 1)
        return month_start, month_end
    if period in ("weekend",):
        # Saturday–Sunday of this week
        days_to_sat = (5 - today_start.weekday()) % 7 or 7
        sat = today_start + timedelta(days=days_to_sat)
        return sat, sat + timedelta(days=2)
    # Fallback: treat as dateparser input
    start = parse_time(period, tz_name, prefer_future=True)
    if start:
        return start, start + timedelta(days=1)
    return today_start, today_start + timedelta(days=1)


def _make_event(
    title: str,
    start_dt: datetime,
    duration_minutes: int,
    household_timezone: str,
    description: str = "",
    location: str = "",
    reminders: list | None = None,
    recurrence: list | None = None,
) -> Any:
    """Create a gcsa Event using an explicit UTC offset.

    Sends: {"dateTime": "2026-06-03T15:00:00-05:00"}
    The UTC offset is unambiguous — Google Calendar cannot misinterpret it.
    No timezone= parameter needed; the offset encodes the timezone fully.
    """
    import pytz
    from gcsa.event import Event

    tz = pytz.timezone(household_timezone)

    if start_dt.tzinfo is None:
        start_dt = tz.localize(start_dt)
    else:
        start_dt = start_dt.astimezone(tz)

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    return Event(
        summary=title,
        start=start_dt,
        end=end_dt,
        description=description or None,
        location=location or None,
        reminders=reminders,
        recurrence=recurrence,
    )


def _event_matches(event_summary: str, query: str) -> bool:
    """Match an event if any significant query word appears in the event summary.

    Full-phrase match is tried first. If that fails, individual words in the
    query are checked against the summary — after stripping common stop words
    that describe recurrence/type rather than the event itself (e.g. "weekly",
    "reminder"). This lets "cancel weekly trash reminder" find "Trash Reminder".
    """
    summary_lower = (event_summary or "").lower()
    query_lower = query.lower().strip()

    if not query_lower or not summary_lower:
        return False

    if query_lower in summary_lower:
        return True

    _STOP_WORDS = {
        "the", "a", "an", "my", "weekly", "daily", "monthly", "yearly",
        "recurring", "reminder", "appointment", "meeting", "event",
        "cancel", "delete", "remove",
    }
    words = [w for w in query_lower.split() if w not in _STOP_WORDS and len(w) > 2]
    return any(word in summary_lower for word in words)


def _format_event(event: Any, tz_name: str) -> str:
    """Format a gcsa Event for human-readable summary."""
    title = getattr(event, "summary", "Untitled") or "Untitled"
    start = getattr(event, "start", None)
    if start is None:
        return title
    if isinstance(start, datetime):
        try:
            import pytz
            tz = pytz.timezone(tz_name)
            local_start = start.astimezone(tz)
            return f"{title} at {local_start.strftime('%I:%M %p').lstrip('0')}"
        except Exception:
            return f"{title} at {start.isoformat()}"
    # all-day event — start is a date
    return f"{title} (all day)"


# ---------------------------------------------------------------------------
# Tool 1 — calendar_read_events
# ---------------------------------------------------------------------------

class _ReadEventsArgs(BaseModel):
    time_range: str = Field(
        description=(
            "Time range to read. Use: 'today', 'tomorrow', 'this week', "
            "'next week', 'this month', 'weekend', or 'from X to Y'."
        )
    )
    user_id: str = Field(default="", description="User whose calendar to read. Default: requesting user.")
    calendar: str = Field(default="primary", description="Calendar ID. Default: primary.")


class CalendarReadEventsTool(BaseTool):
    name: str = "calendar_read_events"
    description: str = (
        "List calendar events for a time range. Use when user asks what is "
        "on their calendar, what they have today/this week/this month, or wants "
        "to see upcoming appointments. Do NOT use for: checking availability "
        "(use calendar_check_availability), or adding events."
    )
    args_schema: Type[BaseModel] = _ReadEventsArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, time_range: str, user_id: str = "", calendar: str = "primary") -> ToolResult:
        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        try:
            start, end = _range_from_period(time_range, ctx.household_timezone)
            events = svc.list_events(uid, start, end, calendar_id=calendar)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            _logger.error("calendar_read_events failed: %s", exc, exc_info=True)
            return ToolResult.fail(f"Could not read calendar: {exc}", error_code="CALENDAR_ERROR")

        if not events:
            return ToolResult.ok(
                f"No events scheduled for {time_range}.",
                data={"events": [], "count": 0, "period": time_range},
            )

        formatted = [_format_event(e, ctx.household_timezone) for e in events]
        count = len(formatted)
        label = "event" if count == 1 else "events"
        summary = f"{count} {label} for {time_range}: {', '.join(formatted)}"
        return ToolResult.ok(
            summary,
            data={
                "events": [
                    {
                        "id": getattr(e, "id", ""),
                        "title": getattr(e, "summary", ""),
                        "start": str(getattr(e, "start", "")),
                        "end": str(getattr(e, "end", "")),
                    }
                    for e in events
                ],
                "count": count,
                "period": time_range,
            },
        )


# ---------------------------------------------------------------------------
# Tool 2 — calendar_create_event
# ---------------------------------------------------------------------------

class _CreateEventArgs(BaseModel):
    title: str = Field(description="Event title.")
    start_time: str = Field(description="Start time in natural language e.g. 'tuesday 3pm', 'tomorrow morning'.")
    duration_minutes: int = Field(default=60, description="Duration in minutes. Default 60.")
    description: str = Field(default="", description="Optional event description.")
    location: str = Field(default="", description="Optional location.")
    user_id: str = Field(default="", description="Whose calendar to add to. Default: requesting user.")
    add_reminder_minutes: int = Field(default=0, description="Popup reminder N minutes before. 0 = no reminder.")


class CalendarCreateEventTool(BaseTool):
    name: str = "calendar_create_event"
    description: str = (
        "Create a new calendar event. Use when user wants to schedule an "
        "appointment, meeting, reminder, or any future event. Requires Google "
        "to be connected. Do NOT use for: recurring events (use "
        "calendar_create_recurring)."
    )
    args_schema: Type[BaseModel] = _CreateEventArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        title: str,
        start_time: str,
        duration_minutes: int = 60,
        description: str = "",
        location: str = "",
        user_id: str = "",
        add_reminder_minutes: int = 0,
    ) -> ToolResult:
        from gcsa.reminders import PopupReminder

        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        start_dt = parse_time(start_time, ctx.household_timezone)
        if start_dt is None:
            return ToolResult.fail(
                f"Could not understand '{start_time}' as a date/time. "
                "Try: 'next Tuesday at 3pm' or 'tomorrow morning'.",
                error_code="UNPARSEABLE_DATE",
            )

        reminders = []
        if add_reminder_minutes > 0:
            reminders.append(PopupReminder(minutes_before_start=add_reminder_minutes))

        event = _make_event(
            title, start_dt, duration_minutes, ctx.household_timezone,
            description=description, location=location, reminders=reminders or None,
        )
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        try:
            created = svc.create_event(uid, event)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            _logger.error("calendar_create_event failed: %s", exc, exc_info=True)
            return ToolResult.fail(f"Could not create event: {exc}", error_code="CALENDAR_ERROR")

        event_id = getattr(created, "id", "")
        import pytz
        tz = pytz.timezone(ctx.household_timezone)
        local_start = start_dt.astimezone(tz)
        date_str = local_start.strftime("%A %B %-d")
        time_str = local_start.strftime("%-I:%M %p %Z")

        summary = (
            f"Created '{title}' for {date_str} at {time_str} "
            f"({duration_minutes} min). Added to Google Calendar."
        )
        domain_event = DomainEvent(
            event_type="appointment_created",
            source_domain="time",
            payload={
                "title": title,
                "start_time": start_dt.isoformat(),
                "end_time": end_dt.isoformat(),
                "event_id": event_id,
                "user_id": uid,
                "calendar_id": "primary",
            },
            user_id=uid,
            household_id=ctx.household_id,
            request_id=ctx.request_id,
        )
        return ToolResult.ok(
            summary,
            data={"event_id": event_id, "title": title, "start": start_dt.isoformat()},
            undo_token=event_id,
            events=[domain_event],
        )


# ---------------------------------------------------------------------------
# Tool 3 — calendar_create_recurring
# ---------------------------------------------------------------------------

class _CreateRecurringArgs(BaseModel):
    title: str = Field(description="Event title.")
    start_time: str = Field(description="First occurrence in natural language.")
    duration_minutes: int = Field(default=30, description="Duration in minutes. Default 30.")
    frequency: str = Field(description="Recurrence: 'daily', 'weekly', 'monthly', or 'yearly'.")
    interval: int = Field(default=1, description="Interval between occurrences. 2 = every other week.")
    by_week_day: str = Field(default="", description="Comma-separated weekdays: 'MO,TU,WE,TH,FR,SA,SU'.")
    by_month_day: int = Field(default=0, description="Day of month (1-31). For monthly events.")
    end_date: str = Field(default="", description="Optional end: 'after 10 occurrences' or 'until Dec 31 2026'.")
    description: str = Field(default="", description="Optional description.")
    user_id: str = Field(default="", description="Whose calendar. Default: requesting user.")


_FREQ_MAP = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
    "yearly": "YEARLY",
}

_WDAY_MAP = {
    "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6,
}


class CalendarCreateRecurringTool(BaseTool):
    name: str = "calendar_create_recurring"
    description: str = (
        "Create a recurring calendar event. Use for events that repeat: "
        "weekly meetings, monthly bills, annual birthdays, trash pickup, "
        "medication reminders, exercise routines. "
        "Do NOT use for: one-time events (use calendar_create_event)."
    )
    args_schema: Type[BaseModel] = _CreateRecurringArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        title: str,
        start_time: str,
        duration_minutes: int = 30,
        frequency: str = "weekly",
        interval: int = 1,
        by_week_day: str = "",
        by_month_day: int = 0,
        end_date: str = "",
        description: str = "",
        user_id: str = "",
    ) -> ToolResult:
        from gcsa.recurrence import Recurrence, DAILY, WEEKLY, MONTHLY, YEARLY

        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        start_dt = parse_time(start_time, ctx.household_timezone)
        if start_dt is None:
            return ToolResult.fail(
                f"Could not understand '{start_time}' as a date/time. "
                "Try: 'next Tuesday at 7am'.",
                error_code="UNPARSEABLE_DATE",
            )
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        freq_key = frequency.lower()
        if freq_key not in _FREQ_MAP:
            return ToolResult.fail(
                f"Unknown frequency '{frequency}'. Use: daily, weekly, monthly, yearly.",
                error_code="INVALID_FREQUENCY",
            )

        freq_const = {"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY, "yearly": YEARLY}[freq_key]

        rrule_kwargs: dict[str, Any] = {"freq": freq_const, "interval": interval}

        if by_week_day:
            days = [d.strip().upper() for d in by_week_day.split(",") if d.strip()]
            valid_days = [d for d in days if d in _WDAY_MAP]
            if valid_days:
                from gcsa.recurrence import MO, TU, WE, TH, FR, SA, SU
                day_objs = {"MO": MO, "TU": TU, "WE": WE, "TH": TH, "FR": FR, "SA": SA, "SU": SU}
                rrule_kwargs["by_week_day"] = [day_objs[d] for d in valid_days]

        if by_month_day and 1 <= by_month_day <= 31:
            rrule_kwargs["by_month_day"] = by_month_day

        if end_date:
            end_dt_parsed = parse_time(end_date, ctx.household_timezone, prefer_future=True)
            if end_dt_parsed:
                rrule_kwargs["until"] = end_dt_parsed

        recurrence = [Recurrence.rule(**rrule_kwargs)]
        event = _make_event(
            title, start_dt, duration_minutes, ctx.household_timezone,
            description=description, recurrence=recurrence,
        )

        try:
            created = svc.create_event(uid, event)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            _logger.error("calendar_create_recurring failed: %s", exc, exc_info=True)
            return ToolResult.fail(f"Could not create recurring event: {exc}", error_code="CALENDAR_ERROR")

        event_id = getattr(created, "id", "")
        import pytz
        tz = pytz.timezone(ctx.household_timezone)
        local_start = start_dt.astimezone(tz)
        first_date = local_start.strftime("%B %-d, %Y")
        freq_display = {
            "daily": "every day", "weekly": f"every week",
            "monthly": "every month", "yearly": "every year",
        }.get(freq_key, frequency)
        if interval > 1:
            freq_display = f"every {interval} {freq_key.rstrip('ly')}s"

        summary = (
            f"Created recurring '{title}' {freq_display}. "
            f"First occurrence {first_date}."
        )
        domain_event = DomainEvent(
            event_type="recurring_event_created",
            source_domain="time",
            payload={
                "title": title,
                "frequency": frequency,
                "start_date": start_dt.isoformat(),
                "event_id": event_id,
                "user_id": uid,
            },
            user_id=uid,
            household_id=ctx.household_id,
            request_id=ctx.request_id,
        )
        return ToolResult.ok(
            summary,
            data={"event_id": event_id, "title": title, "frequency": frequency},
            undo_token=event_id,
            events=[domain_event],
        )


# ---------------------------------------------------------------------------
# Tool 4 — calendar_update_event
# ---------------------------------------------------------------------------

class _UpdateEventArgs(BaseModel):
    event_query: str = Field(description="Event title or keyword to find the event.")
    new_time: str = Field(default="", description="New start time in natural language.")
    new_title: str = Field(default="", description="New event title.")
    new_duration: int = Field(default=0, description="New duration in minutes. 0 = keep current.")
    new_location: str = Field(default="", description="New location.")
    scope: str = Field(default="this", description="For recurring: 'this', 'future', or 'all'.")
    user_id: str = Field(default="", description="Whose calendar. Default: requesting user.")


class CalendarUpdateEventTool(BaseTool):
    name: str = "calendar_update_event"
    description: str = (
        "Update an existing calendar event. Use when user wants to reschedule, "
        "rename, or modify an event. For recurring events specify scope: "
        "'this', 'future', or 'all'. Do NOT use for: deleting events."
    )
    args_schema: Type[BaseModel] = _UpdateEventArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        event_query: str,
        new_time: str = "",
        new_title: str = "",
        new_duration: int = 0,
        new_location: str = "",
        scope: str = "this",
        user_id: str = "",
    ) -> ToolResult:

        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        try:
            search_start = datetime.now(timezone.utc) - timedelta(days=7)
            search_end = datetime.now(timezone.utc) + timedelta(days=90)
            all_events = svc.list_events(uid, search_start, search_end)
            matches = [e for e in all_events if _event_matches(getattr(e, "summary", ""), event_query)]
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            return ToolResult.fail(f"Could not search calendar: {exc}", error_code="CALENDAR_ERROR")

        if not matches:
            return ToolResult.fail(
                f"No event found matching '{event_query}'. "
                "Check the title and try again.",
                error_code="EVENT_NOT_FOUND",
            )

        event = matches[0]
        original_title = getattr(event, "summary", event_query)

        if new_title:
            event.summary = new_title

        if new_time:
            new_start_dt = parse_time(new_time, ctx.household_timezone)
            if new_start_dt is None:
                return ToolResult.fail(
                    f"Could not understand '{new_time}' as a date/time.",
                    error_code="UNPARSEABLE_DATE",
                )
            old_start = getattr(event, "start", None)
            old_end = getattr(event, "end", None)
            if old_start and old_end and isinstance(old_start, datetime) and isinstance(old_end, datetime):
                duration = old_end - old_start
                if new_duration:
                    duration = timedelta(minutes=new_duration)
            else:
                duration = timedelta(minutes=new_duration or 60)
            event.start = new_start_dt
            event.end = new_start_dt + duration

        if new_location:
            event.location = new_location

        try:
            updated = svc.update_event(uid, event)
        except Exception as exc:
            _logger.error("calendar_update_event failed: %s", exc, exc_info=True)
            return ToolResult.fail(f"Could not update event: {exc}", error_code="CALENDAR_ERROR")

        display_title = new_title or original_title
        if new_time:
            import pytz
            tz = pytz.timezone(ctx.household_timezone)
            local = event.start.astimezone(tz)
            time_str = f"to {local.strftime('%A %B %-d at %-I:%M %p')}"
        else:
            time_str = ""

        summary = f"Updated '{display_title}'" + (f" {time_str}" if time_str else "") + "."

        domain_event = DomainEvent(
            event_type="appointment_updated",
            source_domain="time",
            payload={
                "event_id": getattr(updated, "id", ""),
                "old_title": original_title,
                "new_title": new_title or original_title,
                "user_id": uid,
            },
            user_id=uid,
            household_id=ctx.household_id,
            request_id=ctx.request_id,
        )
        return ToolResult.ok(
            summary,
            data={"event_id": getattr(updated, "id", ""), "title": display_title},
            events=[domain_event],
        )


# ---------------------------------------------------------------------------
# Tool 5 — calendar_delete_event
# ---------------------------------------------------------------------------

class _DeleteEventArgs(BaseModel):
    event_query: str = Field(description="Event title or keyword to find the event to delete.")
    scope: str = Field(default="this", description="For recurring: 'this', 'future', or 'all'.")
    user_id: str = Field(default="", description="Whose calendar. Default: requesting user.")


class CalendarDeleteEventTool(BaseTool):
    name: str = "calendar_delete_event"
    description: str = (
        "Delete a calendar event permanently. Trigger words: cancel, delete, "
        "remove, drop, scratch, remove appointment, cancel meeting. "
        "Always requires confirmation. Cannot be undone. "
        "Use when user explicitly wants to cancel/delete/remove an event. "
        "For recurring events specify scope. "
        "Do NOT use for: rescheduling (use calendar_update_event instead)."
    )
    args_schema: Type[BaseModel] = _DeleteEventArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(self, event_query: str, scope: str = "this", user_id: str = "") -> ToolResult:

        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        try:
            search_start = datetime.now(timezone.utc) - timedelta(days=7)
            search_end = datetime.now(timezone.utc) + timedelta(days=90)
            all_events = svc.list_events(uid, search_start, search_end)
            matches = [e for e in all_events if _event_matches(getattr(e, "summary", ""), event_query)]
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            return ToolResult.fail(f"Could not search calendar: {exc}", error_code="CALENDAR_ERROR")

        if not matches:
            return ToolResult.fail(
                f"No event found matching '{event_query}'.",
                error_code="EVENT_NOT_FOUND",
            )

        event = matches[0]
        event_id = getattr(event, "id", "")
        title = getattr(event, "summary", event_query)
        original_start = getattr(event, "start", None)

        try:
            svc.delete_event(uid, event_id)
        except Exception as exc:
            _logger.error("calendar_delete_event failed: %s", exc, exc_info=True)
            return ToolResult.fail(f"Could not delete event: {exc}", error_code="CALENDAR_ERROR")

        start_str = ""
        if original_start:
            if isinstance(original_start, datetime):
                import pytz
                tz = pytz.timezone(ctx.household_timezone)
                start_str = f" on {original_start.astimezone(tz).strftime('%B %-d')}"
            else:
                start_str = f" on {original_start}"

        summary = f"Deleted '{title}'{start_str}."
        domain_event = DomainEvent(
            event_type="appointment_cancelled",
            source_domain="time",
            payload={
                "event_id": event_id,
                "title": title,
                "original_time": str(original_start) if original_start else "",
                "user_id": uid,
            },
            user_id=uid,
            household_id=ctx.household_id,
            request_id=ctx.request_id,
        )
        return ToolResult.ok(summary, data={"event_id": event_id, "title": title}, events=[domain_event])


# ---------------------------------------------------------------------------
# Tool 6 — calendar_check_availability
# ---------------------------------------------------------------------------

class _CheckAvailabilityArgs(BaseModel):
    time_range: str = Field(
        description="Time range to check e.g. 'saturday afternoon', 'this week', 'tuesday 2-4pm'."
    )
    duration_minutes: int = Field(default=0, description="Minimum free slot needed in minutes. 0 = any gap.")
    user_ids: list[str] = Field(
        default_factory=list,
        description="User IDs to check. Empty = requesting user only. ['all'] = all household members.",
    )


class CalendarCheckAvailabilityTool(BaseTool):
    name: str = "calendar_check_availability"
    description: str = (
        "Check if a user (or multiple users) is free during a time period. "
        "Use when asked about availability, finding free slots, or checking "
        "for conflicts. Do NOT use for: listing events (use calendar_read_events)."
    )
    args_schema: Type[BaseModel] = _CheckAvailabilityArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        time_range: str,
        duration_minutes: int = 0,
        user_ids: list[str] | None = None,
    ) -> ToolResult:

        ctx = self._ctx
        if not user_ids:
            user_ids = [ctx.user_id]
        elif user_ids == ["all"] and ctx.member_registry:
            user_ids = [m.id for m in ctx.member_registry.get_members()]

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        try:
            start, end = _range_from_period(time_range, ctx.household_timezone)
            busy = svc.get_free_busy(user_ids, start, end)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            return ToolResult.fail(f"Could not check availability: {exc}", error_code="CALENDAR_ERROR")

        # Compute merged busy intervals across all requested users
        all_busy: list[tuple[datetime, datetime]] = []
        for uid_busy in busy.values():
            for blk in uid_busy:
                all_busy.append((blk["start"], blk["end"]))
        all_busy.sort(key=lambda x: x[0])

        # Find free windows
        free_slots: list[tuple[datetime, datetime]] = []
        cursor = start
        for bs, be in all_busy:
            if cursor < bs:
                gap_mins = (bs - cursor).total_seconds() / 60
                if not duration_minutes or gap_mins >= duration_minutes:
                    free_slots.append((cursor, bs))
            cursor = max(cursor, be)
        if cursor < end:
            gap_mins = (end - cursor).total_seconds() / 60
            if not duration_minutes or gap_mins >= duration_minutes:
                free_slots.append((cursor, end))

        import pytz
        tz = pytz.timezone(ctx.household_timezone)

        def _fmt_slot(s: datetime, e: datetime) -> str:
            ls = s.astimezone(tz)
            le = e.astimezone(tz)
            if ls.date() == le.date():
                return f"{ls.strftime('%a %-I%p')}-{le.strftime('%-I%p')}"
            return f"{ls.strftime('%a %-I%p')} to {le.strftime('%a %-I%p')}"

        if not free_slots:
            summary = (
                f"No free time found during {time_range}"
                + (f" (minimum {duration_minutes} min)" if duration_minutes else "")
                + "."
            )
        elif len(free_slots) == 1:
            summary = f"Free during {time_range}: {_fmt_slot(*free_slots[0])}."
        else:
            slots_str = ", ".join(_fmt_slot(*s) for s in free_slots[:3])
            summary = f"Free slots during {time_range}: {slots_str}."

        return ToolResult.ok(
            summary,
            data={
                "free_slots": [
                    {"start": s.isoformat(), "end": e.isoformat()}
                    for s, e in free_slots
                ],
                "busy_count": len(all_busy),
                "period": time_range,
            },
        )


# ---------------------------------------------------------------------------
# Tool 7 — calendar_daily_briefing
# ---------------------------------------------------------------------------

class _DailyBriefingArgs(BaseModel):
    period: str = Field(
        default="today",
        description="Period to brief: 'today', 'tomorrow', 'week', or 'weekend'.",
    )
    user_id: str = Field(default="", description="Whose briefing. Default: requesting user.")
    include_household: bool = Field(default=True, description="Include other household members' events.")


class CalendarDailyBriefingTool(BaseTool):
    name: str = "calendar_daily_briefing"
    description: str = (
        "Generate a structured briefing of upcoming events. Use when user asks "
        "about their day, week, or requests a schedule summary. Includes today's "
        "events, upcoming reminders, household member events. "
        "Do NOT use for: adding or modifying events."
    )
    args_schema: Type[BaseModel] = _DailyBriefingArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        period: str = "today",
        user_id: str = "",
        include_household: bool = True,
    ) -> ToolResult:

        ctx = self._ctx
        uid = user_id or ctx.user_id

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        start, end = _range_from_period(period, ctx.household_timezone)

        lines: list[str] = []
        all_data: dict[str, Any] = {}

        try:
            my_events = svc.list_events(uid, start, end)
            if my_events:
                formatted = [_format_event(e, ctx.household_timezone) for e in my_events]
                lines.append("Your " + period + ": " + ", ".join(formatted))
            else:
                lines.append(f"Nothing on your calendar for {period}.")
            all_data["user_events"] = [getattr(e, "summary", "") for e in my_events]
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            _logger.warning("briefing: could not load events for %s: %s", uid, exc)
            lines.append(f"Could not load your {period} events.")

        if include_household and ctx.member_registry:
            members = ctx.member_registry.get_members()
            for member in members:
                if member.id == uid:
                    continue
                try:
                    member_events = svc.list_events(member.id, start, end)
                    if member_events:
                        name = getattr(member, "display_name", member.id)
                        fmt = [_format_event(e, ctx.household_timezone) for e in member_events]
                        lines.append(f"{name}: {', '.join(fmt)}")
                except Exception:
                    pass

        summary = " | ".join(lines) if lines else f"Nothing scheduled for {period}."
        return ToolResult.ok(
            summary,
            data=all_data,
        )


# ---------------------------------------------------------------------------
# Tool 8 — calendar_find_time
# ---------------------------------------------------------------------------

class _FindTimeArgs(BaseModel):
    duration_minutes: int = Field(description="Required slot duration in minutes.")
    within_period: str = Field(
        default="this week",
        description="Search window: 'today', 'this week', 'next week'.",
    )
    preferred_time: str = Field(
        default="",
        description="Optional preference: 'morning', 'afternoon', or 'evening'.",
    )
    participants: list[str] = Field(
        default_factory=list,
        description="Optional user IDs to include in availability check.",
    )


_PREF_HOURS = {"morning": (8, 12), "afternoon": (12, 17), "evening": (17, 21)}


class CalendarFindTimeTool(BaseTool):
    name: str = "calendar_find_time"
    description: str = (
        "Find the best available time slot for a new event. Use when user wants "
        "to schedule something but hasn't specified a time, or asks for suggestions. "
        "Considers all specified participants. "
        "Do NOT use for: checking specific time availability (use "
        "calendar_check_availability)."
    )
    args_schema: Type[BaseModel] = _FindTimeArgs
    _ctx: DomainContext

    def __init__(self, context: DomainContext, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_ctx", context)

    def _run(
        self,
        duration_minutes: int,
        within_period: str = "this week",
        preferred_time: str = "",
        participants: list[str] | None = None,
    ) -> ToolResult:

        ctx = self._ctx
        user_ids = list(participants or []) or [ctx.user_id]

        svc = ctx.oauth_manager and GoogleCalendarService(
            oauth_manager=ctx.oauth_manager,
            household_id=ctx.household_id,
            household_timezone=ctx.household_timezone,
        )
        if svc is None:
            return ToolResult.fail(
                "Google Calendar not connected. Send 'connect google' to Mithra.",
                error_code="GOOGLE_NOT_CONNECTED",
            )

        try:
            start, end = _range_from_period(within_period, ctx.household_timezone)
            busy = svc.get_free_busy(user_ids, start, end)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_code="GOOGLE_NOT_CONNECTED")
        except Exception as exc:
            return ToolResult.fail(f"Could not check availability: {exc}", error_code="CALENDAR_ERROR")

        # Merge all busy blocks
        all_busy: list[tuple[datetime, datetime]] = []
        for uid_busy in busy.values():
            for blk in uid_busy:
                all_busy.append((blk["start"], blk["end"]))
        all_busy.sort(key=lambda x: x[0])

        import pytz
        tz = pytz.timezone(ctx.household_timezone)
        pref_range = _PREF_HOURS.get(preferred_time.lower(), (8, 21))

        def _in_pref(dt: datetime) -> bool:
            local = dt.astimezone(tz)
            return pref_range[0] <= local.hour < pref_range[1]

        suggested: list[datetime] = []
        slot_duration = timedelta(minutes=duration_minutes)
        cursor = start

        for bs, be in all_busy:
            while cursor + slot_duration <= bs:
                if _in_pref(cursor):
                    suggested.append(cursor)
                    if len(suggested) >= 3:
                        break
                cursor += timedelta(minutes=30)
            cursor = max(cursor, be)
            if len(suggested) >= 3:
                break

        while len(suggested) < 3 and cursor + slot_duration <= end:
            if _in_pref(cursor):
                suggested.append(cursor)
            cursor += timedelta(minutes=30)

        if not suggested:
            return ToolResult.ok(
                f"No free {duration_minutes}-minute slot found in {within_period}.",
                data={"slots": [], "period": within_period},
            )

        def _fmt(dt: datetime) -> str:
            local = dt.astimezone(tz)
            return local.strftime("%A %-I%p")

        slots_str = ", ".join(_fmt(s) for s in suggested)
        summary = (
            f"Best {duration_minutes}-min slots in {within_period}: "
            f"{slots_str}. Which works for you?"
        )
        return ToolResult.ok(
            summary,
            data={
                "slots": [s.isoformat() for s in suggested],
                "duration_minutes": duration_minutes,
                "period": within_period,
            },
        )


# ---------------------------------------------------------------------------
# Factory — build all 8 ToolBundles for a given DomainContext
# ---------------------------------------------------------------------------

def build_time_tools(context: DomainContext) -> list[ToolBundle]:
    """Construct all 8 Time domain tools bound to the given context."""
    return [
        ToolBundle(
            tool=CalendarReadEventsTool(context=context),
            metadata=ToolMetadata(
                name="calendar_read_events",
                domain_id="time",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=CalendarCreateEventTool(context=context),
            metadata=ToolMetadata(
                name="calendar_create_event",
                domain_id="time",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=1,
                event_emitted="appointment_created",
            ),
        ),
        ToolBundle(
            tool=CalendarCreateRecurringTool(context=context),
            metadata=ToolMetadata(
                name="calendar_create_recurring",
                domain_id="time",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=1,
                event_emitted="recurring_event_created",
            ),
        ),
        ToolBundle(
            tool=CalendarUpdateEventTool(context=context),
            metadata=ToolMetadata(
                name="calendar_update_event",
                domain_id="time",
                permission=PermissionTier.WRITE,
                is_reversible=True,
                autonomy_level=1,
                event_emitted="appointment_updated",
            ),
        ),
        ToolBundle(
            tool=CalendarDeleteEventTool(context=context),
            metadata=ToolMetadata(
                name="calendar_delete_event",
                domain_id="time",
                permission=PermissionTier.DESTRUCTIVE,
                is_reversible=False,
                autonomy_level=1,
                event_emitted="appointment_cancelled",
            ),
        ),
        ToolBundle(
            tool=CalendarCheckAvailabilityTool(context=context),
            metadata=ToolMetadata(
                name="calendar_check_availability",
                domain_id="time",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=CalendarDailyBriefingTool(context=context),
            metadata=ToolMetadata(
                name="calendar_daily_briefing",
                domain_id="time",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
        ToolBundle(
            tool=CalendarFindTimeTool(context=context),
            metadata=ToolMetadata(
                name="calendar_find_time",
                domain_id="time",
                permission=PermissionTier.READ,
                is_reversible=False,
                autonomy_level=3,
            ),
        ),
    ]
