"""Tests for time parsing utilities."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mithra.domains.time.tools import _range_from_period, parse_time


TZ = "America/Chicago"


class TestParseTime:
    def test_returns_datetime_for_day_of_week(self):
        result = parse_time("Tuesday 3pm", TZ)
        assert result is not None
        assert isinstance(result, datetime)

    def test_returns_timezone_aware(self):
        result = parse_time("tomorrow at 9am", TZ)
        assert result is not None
        assert result.tzinfo is not None

    def test_returns_none_for_garbage(self):
        result = parse_time("blurp blorp zork", TZ)
        assert result is None

    def test_prefer_future_returns_future_date(self):
        result = parse_time("friday 7pm", TZ)
        assert result is not None
        now = datetime.now(result.tzinfo)
        assert result > now

    def test_relative_expression(self):
        result = parse_time("in 2 hours", TZ)
        assert result is not None
        now = datetime.now(result.tzinfo)
        assert result > now

    def test_explicit_date_and_time(self):
        result = parse_time("december 31 2026 10am", TZ)
        assert result is not None
        assert result.year == 2026
        assert result.month == 12


class TestRangeFromPeriod:
    def test_today_is_single_day(self):
        start, end = _range_from_period("today", TZ)
        assert (end - start).days == 1

    def test_tomorrow_is_single_day(self):
        start, end = _range_from_period("tomorrow", TZ)
        from datetime import timedelta
        assert (end - start).days == 1

    def test_this_week_is_seven_days(self):
        start, end = _range_from_period("this week", TZ)
        from datetime import timedelta
        assert (end - start).days == 7

    def test_this_month_spans_full_month(self):
        start, end = _range_from_period("this month", TZ)
        assert start.day == 1
        assert end.day == 1  # first of next month
        assert (end - start).days >= 28

    def test_weekend_is_two_days(self):
        start, end = _range_from_period("weekend", TZ)
        from datetime import timedelta
        assert (end - start).days == 2

    def test_next_week_starts_after_this_week(self):
        this_start, _ = _range_from_period("this week", TZ)
        next_start, _ = _range_from_period("next week", TZ)
        from datetime import timedelta
        assert next_start == this_start + timedelta(weeks=1)
