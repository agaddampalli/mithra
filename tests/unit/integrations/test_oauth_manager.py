"""Tests for mithra/integrations/oauth_manager.py."""

from __future__ import annotations

import pytest

from mithra.integrations.oauth_manager import OAuthManager


def _make_providers() -> dict:
    return {
        "google": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/calendar", "openid"],
            "auth_params": {"access_type": "offline", "prompt": "consent"},
            "client_id": "test-client-id",
            "client_secret": "test-client-secret",
        },
    }


class _FakeTokenStore:
    """In-memory token store for unit tests."""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def save(self, provider_id, user_id, token_data):
        self._data[f"{provider_id}:{user_id}"] = dict(token_data)

    def load(self, provider_id, user_id):
        return self._data.get(f"{provider_id}:{user_id}")

    def delete(self, provider_id, user_id):
        self._data.pop(f"{provider_id}:{user_id}", None)

    def is_connected(self, provider_id, user_id):
        d = self.load(provider_id, user_id)
        return d is not None and bool(d.get("access_token", ""))


class TestAvailableProviders:
    def test_returns_configured_provider_list(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        assert mgr.available_providers() == ["google"]

    def test_empty_providers_returns_empty_list(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers={}, token_store=store)
        assert mgr.available_providers() == []


class TestGetAuthUrl:
    def test_returns_google_auth_url(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        url = mgr.get_auth_url("google", "usr_abc", "https://example.com/auth/google/callback")
        assert "accounts.google.com" in url
        assert "client_id=test-client-id" in url

    def test_raises_for_unconfigured_provider(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers={}, token_store=store)
        with pytest.raises(KeyError):
            mgr.get_auth_url("google", "usr_abc", "https://example.com/callback")

    def test_url_includes_redirect_uri(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        redirect = "https://example.com/auth/google/callback"
        url = mgr.get_auth_url("google", "usr_abc", redirect)
        import urllib.parse
        assert urllib.parse.quote(redirect, safe="") in url or redirect in url


class TestIsConnected:
    def test_not_connected_when_no_token(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        assert mgr.is_connected("google", "usr_abc") is False

    def test_connected_after_token_stored(self):
        store = _FakeTokenStore()
        store.save("google", "usr_abc", {"access_token": "ya29.test", "refresh_token": ""})
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        assert mgr.is_connected("google", "usr_abc") is True


class TestDisconnect:
    def test_disconnect_removes_token(self):
        store = _FakeTokenStore()
        store.save("google", "usr_abc", {"access_token": "ya29.test", "refresh_token": ""})
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        mgr.disconnect("google", "usr_abc")
        assert mgr.is_connected("google", "usr_abc") is False

    def test_disconnect_non_existent_does_not_raise(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        mgr.disconnect("google", "usr_nobody")  # must not raise


class TestGetTokenData:
    def test_returns_none_when_no_token(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        assert mgr.get_token_data("google", "usr_abc") is None

    def test_returns_full_token_dict(self):
        store = _FakeTokenStore()
        token = {
            "access_token": "ya29.test",
            "refresh_token": "1//refresh",
            "expires_at": 9999999999,
            "scopes": ["https://www.googleapis.com/auth/calendar"],
        }
        store.save("google", "usr_abc", token)
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        result = mgr.get_token_data("google", "usr_abc")
        assert result is not None
        assert result["access_token"] == "ya29.test"
        assert result["refresh_token"] == "1//refresh"
        assert "scopes" in result

    def test_does_not_refresh_token(self):
        """get_token_data() is a raw read — no refresh side-effects."""
        import time
        store = _FakeTokenStore()
        store.save("google", "usr_abc", {
            "access_token": "ya29.expired",
            "refresh_token": "1//refresh",
            "expires_at": int(time.time()) - 3600,  # already expired
        })
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        result = mgr.get_token_data("google", "usr_abc")
        # Returns raw data even if expired — caller decides what to do
        assert result is not None
        assert result["access_token"] == "ya29.expired"


class TestSaveTokenData:
    def test_save_then_load_round_trip(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        token = {"access_token": "ya29.new", "refresh_token": "1//r", "expires_at": 9999999999}
        mgr.save_token_data("google", "usr_abc", token)
        result = mgr.get_token_data("google", "usr_abc")
        assert result is not None
        assert result["access_token"] == "ya29.new"

    def test_save_overwrites_existing(self):
        store = _FakeTokenStore()
        store.save("google", "usr_abc", {"access_token": "old_token"})
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        mgr.save_token_data("google", "usr_abc", {"access_token": "new_token"})
        result = mgr.get_token_data("google", "usr_abc")
        assert result["access_token"] == "new_token"


class TestGetToken:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_connected(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        result = await mgr.get_token("google", "usr_abc")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_unconfigured_provider(self):
        store = _FakeTokenStore()
        mgr = OAuthManager(providers={}, token_store=store)
        result = await mgr.get_token("google", "usr_abc")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_valid_non_expired_token(self):
        import time
        store = _FakeTokenStore()
        store.save("google", "usr_abc", {
            "access_token": "ya29.valid",
            "refresh_token": "1//refresh",
            "expires_at": int(time.time()) + 3600,  # expires in 1 hour
        })
        mgr = OAuthManager(providers=_make_providers(), token_store=store)
        result = await mgr.get_token("google", "usr_abc")
        assert result == "ya29.valid"
