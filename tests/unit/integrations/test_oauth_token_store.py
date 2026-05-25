"""Tests for mithra/integrations/oauth_token_store.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from mithra.integrations.oauth_token_store import OAuthTokenStore


def _make_store(tmp_path):
    from mithra.storage.local import LocalStorageAdapter

    db_path = str(tmp_path / "mithra.db")
    LocalStorageAdapter(str(tmp_path / "files"), db_path)
    from mithra.storage.local import LocalStorageAdapter as LSA
    adapter = LSA(str(tmp_path / "files"), db_path)
    return OAuthTokenStore(storage_adapter=adapter, household_id="hh_test")


def _sample_token() -> dict:
    return {
        "access_token": "ya29.test-access-token",
        "refresh_token": "1//test-refresh-token",
        "token_type": "Bearer",
        "expires_at": 9999999999,
        "scopes": ["https://www.googleapis.com/auth/calendar"],
    }


class TestSaveAndLoad:
    def test_save_then_load_returns_data(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        data = store.load("google", "usr_abc123")
        assert data is not None
        assert data["access_token"] == "ya29.test-access-token"

    def test_load_returns_none_when_not_stored(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.load("google", "usr_missing") is None

    def test_save_stores_provider_and_user_id(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        data = store.load("google", "usr_abc123")
        assert data["provider"] == "google"
        assert data["user_id"] == "usr_abc123"

    def test_save_stores_refresh_token(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        data = store.load("google", "usr_abc123")
        assert data["refresh_token"] == "1//test-refresh-token"

    def test_save_overwrites_existing_token(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        new_token = dict(_sample_token())
        new_token["access_token"] = "ya29.new-token"
        store.save("google", "usr_abc123", new_token)
        data = store.load("google", "usr_abc123")
        assert data["access_token"] == "ya29.new-token"

    def test_tokens_are_isolated_per_user(self, tmp_path):
        store = _make_store(tmp_path)
        t1 = dict(_sample_token()); t1["access_token"] = "token-user-a"
        t2 = dict(_sample_token()); t2["access_token"] = "token-user-b"
        store.save("google", "usr_aaa", t1)
        store.save("google", "usr_bbb", t2)
        assert store.load("google", "usr_aaa")["access_token"] == "token-user-a"
        assert store.load("google", "usr_bbb")["access_token"] == "token-user-b"

    def test_tokens_are_isolated_per_provider(self, tmp_path):
        store = _make_store(tmp_path)
        t1 = dict(_sample_token()); t1["access_token"] = "google-token"
        t2 = dict(_sample_token()); t2["access_token"] = "ms-token"
        store.save("google", "usr_aaa", t1)
        store.save("microsoft", "usr_aaa", t2)
        assert store.load("google", "usr_aaa")["access_token"] == "google-token"
        assert store.load("microsoft", "usr_aaa")["access_token"] == "ms-token"


class TestIsConnected:
    def test_is_connected_false_when_no_token(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.is_connected("google", "usr_nobody") is False

    def test_is_connected_true_after_save(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        assert store.is_connected("google", "usr_abc123") is True


class TestDelete:
    def test_delete_removes_token(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_abc123", _sample_token())
        store.delete("google", "usr_abc123")
        assert store.load("google", "usr_abc123") is None

    def test_delete_non_existent_does_not_raise(self, tmp_path):
        store = _make_store(tmp_path)
        store.delete("google", "usr_nobody")  # must not raise

    def test_delete_one_user_does_not_affect_other(self, tmp_path):
        store = _make_store(tmp_path)
        store.save("google", "usr_aaa", _sample_token())
        store.save("google", "usr_bbb", _sample_token())
        store.delete("google", "usr_aaa")
        assert store.load("google", "usr_aaa") is None
        assert store.load("google", "usr_bbb") is not None
