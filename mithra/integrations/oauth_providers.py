"""
OAuth provider configuration.

Adding a new provider = one new entry in OAUTH_PROVIDERS + env vars.
Zero code changes anywhere else.
"""

from __future__ import annotations

import os

OAUTH_PROVIDERS: dict[str, dict] = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/contacts.readonly",
            "openid",
            "email",
            "profile",
        ],
        "auth_params": {
            "access_type": "offline",
            "prompt": "consent",
        },
        "client_id_env":     "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        "scopes": [
            "https://graph.microsoft.com/Files.ReadWrite",
            "https://graph.microsoft.com/Calendars.ReadWrite",
            "https://graph.microsoft.com/Contacts.ReadWrite",
            "offline_access",
        ],
        "auth_params": {},
        "client_id_env":     "ONEDRIVE_CLIENT_ID",
        "client_secret_env": "ONEDRIVE_CLIENT_SECRET",
        "tenant_id_env":     "ONEDRIVE_TENANT_ID",
        "tenant_id_default": "common",
    },
}


def _resolve_url(url_template: str, provider_cfg: dict) -> str:
    """Substitute {tenant_id} in URL templates for providers that need it."""
    if "{tenant_id}" not in url_template:
        return url_template
    tenant_id_env = provider_cfg.get("tenant_id_env", "")
    tenant_id = os.environ.get(tenant_id_env, "") or provider_cfg.get("tenant_id_default", "common")
    return url_template.replace("{tenant_id}", tenant_id)


def get_configured_providers() -> dict[str, dict]:
    """Return only providers whose client_id and client_secret env vars are set."""
    result: dict[str, dict] = {}
    for provider_id, cfg in OAUTH_PROVIDERS.items():
        client_id = os.environ.get(cfg["client_id_env"], "")
        client_secret = os.environ.get(cfg["client_secret_env"], "")
        if client_id and client_secret:
            resolved = dict(cfg)
            resolved["auth_url"] = _resolve_url(cfg["auth_url"], cfg)
            resolved["token_url"] = _resolve_url(cfg["token_url"], cfg)
            resolved["client_id"] = client_id
            resolved["client_secret"] = client_secret
            result[provider_id] = resolved
    return result
