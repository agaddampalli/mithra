"""Tests for GoogleCalendarService — no real network calls."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from mithra.domains.time.service import GoogleCalendarService


class TestGoogleCalendarServiceInit:
    def test_constructs_with_oauth_manager(self):
        oauth = MagicMock()
        svc = GoogleCalendarService(oauth_manager=oauth, household_id="hh_001")
        assert svc._oauth is oauth

    def test_no_oauth_manager_raises_on_get_gc(self):
        svc = GoogleCalendarService(oauth_manager=None, household_id="hh_001")
        with pytest.raises(RuntimeError, match="Google not connected"):
            svc._get_gc("usr_001")

    def test_missing_token_raises_runtime_error(self):
        oauth = MagicMock()
        oauth.get_token_data.return_value = None
        svc = GoogleCalendarService(oauth_manager=oauth, household_id="hh_001")
        with pytest.raises(RuntimeError, match="Google not connected"):
            svc._get_gc("usr_001")


class TestGoogleCalendarServiceCaching:
    def test_cached_instance_returned_on_second_call(self):
        """_get_gc() returns the same object on second call — no rebuild."""
        oauth = MagicMock()
        svc = GoogleCalendarService(oauth_manager=oauth, household_id="hh_001")
        # Pre-populate cache to skip network calls
        fake_gc = MagicMock()
        svc._gc_cache["usr_001"] = fake_gc
        assert svc._get_gc("usr_001") is fake_gc
        assert svc._get_gc("usr_001") is fake_gc

    def test_invalidate_cache_removes_entry(self):
        """invalidate_cache() clears the cached instance."""
        oauth = MagicMock()
        svc = GoogleCalendarService(oauth_manager=oauth, household_id="hh_001")
        svc._gc_cache["usr_001"] = MagicMock()
        svc.invalidate_cache("usr_001")
        assert "usr_001" not in svc._gc_cache

    def test_invalidate_nonexistent_user_is_safe(self):
        """invalidate_cache() on unknown user does not raise."""
        svc = GoogleCalendarService(oauth_manager=MagicMock(), household_id="hh_001")
        svc.invalidate_cache("usr_unknown")  # should not raise
