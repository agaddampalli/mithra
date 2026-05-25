"""Tests for mithra/integrations/oauth_providers.py."""

from __future__ import annotations

import os

import pytest

from mithra.integrations.oauth_providers import (
    OAUTH_PROVIDERS,
    _resolve_url,
    get_configured_providers,
)


class TestOAuthProviders:
    def test_google_and_microsoft_are_defined(self):
        assert "google" in OAUTH_PROVIDERS
        assert "microsoft" in OAUTH_PROVIDERS

    def test_google_has_required_fields(self):
        cfg = OAUTH_PROVIDERS["google"]
        assert "auth_url" in cfg
        assert "token_url" in cfg
        assert "scopes" in cfg
        assert "client_id_env" in cfg
        assert "client_secret_env" in cfg

    def test_microsoft_has_required_fields(self):
        cfg = OAUTH_PROVIDERS["microsoft"]
        assert "auth_url" in cfg
        assert "token_url" in cfg
        assert "tenant_id_env" in cfg
        assert "tenant_id_default" in cfg

    def test_google_scopes_include_calendar(self):
        scopes = OAUTH_PROVIDERS["google"]["scopes"]
        assert any("calendar" in s for s in scopes)


class TestResolveUrl:
    def test_url_without_template_returns_unchanged(self):
        url = "https://accounts.google.com/o/oauth2/v2/auth"
        result = _resolve_url(url, {})
        assert result == url

    def test_url_with_tenant_id_uses_env_var(self, monkeypatch):
        monkeypatch.setenv("ONEDRIVE_TENANT_ID", "my-tenant-123")
        cfg = OAUTH_PROVIDERS["microsoft"]
        result = _resolve_url(cfg["auth_url"], cfg)
        assert "my-tenant-123" in result
        assert "{tenant_id}" not in result

    def test_url_with_tenant_id_falls_back_to_default(self, monkeypatch):
        monkeypatch.delenv("ONEDRIVE_TENANT_ID", raising=False)
        cfg = OAUTH_PROVIDERS["microsoft"]
        result = _resolve_url(cfg["auth_url"], cfg)
        assert "common" in result
        assert "{tenant_id}" not in result


class TestGetConfiguredProviders:
    def test_returns_empty_when_no_env_vars(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ONEDRIVE_CLIENT_SECRET", raising=False)
        result = get_configured_providers()
        assert result == {}

    def test_returns_google_when_google_vars_set(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
        monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ONEDRIVE_CLIENT_SECRET", raising=False)
        result = get_configured_providers()
        assert "google" in result
        assert "microsoft" not in result

    def test_configured_provider_includes_resolved_client_id(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "my-google-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "my-google-secret")
        monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
        monkeypatch.delenv("ONEDRIVE_CLIENT_SECRET", raising=False)
        result = get_configured_providers()
        assert result["google"]["client_id"] == "my-google-id"
        assert result["google"]["client_secret"] == "my-google-secret"

    def test_urls_are_resolved_not_templates(self, monkeypatch):
        monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "ms-id")
        monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "ms-secret")
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
        result = get_configured_providers()
        assert "{tenant_id}" not in result["microsoft"]["auth_url"]
        assert "{tenant_id}" not in result["microsoft"]["token_url"]
