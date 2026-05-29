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


class TestRefreshPersisting:
    def test_refresh_calls_save_token_data(self):
        """_make_refresh_persisting_credentials refresh() calls save_token_data()."""
        from datetime import datetime, timezone as tz_mod
        from mithra.domains.time.service import _make_refresh_persisting_credentials

        oauth = MagicMock()
        new_expiry = datetime(2099, 1, 1, tzinfo=tz_mod.utc)

        # Stub base class — super().refresh() sets token/expiry in-place
        class _FakeBaseCredentials:
            def __init__(self, **kwargs):
                self.token = kwargs.get("token", "old")
                self.refresh_token = kwargs.get("refresh_token", "")
                self.scopes = kwargs.get("scopes", [])
                self.expiry = None

            def refresh(self, request):
                self.token = "ya29.refreshed"
                self.refresh_token = "1//newrefresh"
                self.expiry = new_expiry

        creds = _make_refresh_persisting_credentials(
            _FakeBaseCredentials,
            oauth_manager=oauth,
            user_id="usr_001",
            fallback_scopes=["https://www.googleapis.com/auth/calendar"],
            token="ya29.old",
            refresh_token="1//old",
            scopes=["https://www.googleapis.com/auth/calendar"],
        )

        creds.refresh(request=MagicMock())

        oauth.save_token_data.assert_called_once()
        _, args, _ = oauth.save_token_data.mock_calls[0]
        assert args[0] == "google"
        assert args[1] == "usr_001"
        assert args[2]["access_token"] == "ya29.refreshed"

    def test_refresh_save_failure_does_not_raise(self):
        """save_token_data() errors must not propagate out of refresh()."""
        from mithra.domains.time.service import _make_refresh_persisting_credentials

        oauth = MagicMock()
        oauth.save_token_data.side_effect = RuntimeError("store unavailable")

        class _FakeBaseCredentials:
            def __init__(self, **kwargs):
                self.token = "t"
                self.refresh_token = ""
                self.scopes = []
                self.expiry = None

            def refresh(self, request):
                pass  # no-op

        creds = _make_refresh_persisting_credentials(
            _FakeBaseCredentials,
            oauth_manager=oauth,
            user_id="usr_001",
            fallback_scopes=[],
            token="t",
        )
        creds.refresh(request=MagicMock())  # must not raise


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
