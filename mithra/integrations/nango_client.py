"""
NangoClient — thin wrapper around the Nango OAuth token management API.

Nango handles the full OAuth web flow (connect sessions, token exchange, auto-refresh).
Mithra calls create_connect_session() to get a connect_link, sends it to the user,
and calls get_token() to fetch fresh access tokens for API calls.

connection_id = f"{household_id}__{user_id}" — globally unique per household + user.

Integration setup: Nango stores OAuth credentials and scopes per integration
(provider_config_key). ensure_integration() must be called at startup to register
the credentials and scopes before any user initiates OAuth — without scopes, Nango
sends an invalid request to Google that gets rejected immediately.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class NangoClient:
    """Thin async HTTP client for Nango token management."""

    GOOGLE_PROVIDER = "google-calendar"
    MICROSOFT_PROVIDER = "microsoft-graph"

    def __init__(self, nango_url: str = "", secret_key: str = "") -> None:
        self._url = (nango_url or os.environ.get("NANGO_URL", "http://nango:8080")).rstrip("/")
        self._secret = secret_key or os.environ.get("NANGO_SECRET_KEY", "")

    def get_connection_id(self, user_id: str, household_id: str) -> str:
        """Return the Nango connection ID for a user."""
        return f"{household_id}__{user_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret}"}

    async def ensure_integration(
        self,
        provider_config_key: str,
        provider: str,
        oauth_client_id: str,
        oauth_client_secret: str,
        oauth_scopes: str,
    ) -> bool:
        """Create or update a Nango integration with credentials and scopes.

        Idempotent — safe to call on every startup. Without this, Nango sends
        OAuth requests to Google with no scope parameter and Google rejects them.
        Returns True on success, False if the call fails (non-fatal — Nango may
        already have the integration from a previous startup).
        """
        body: dict[str, Any] = {
            "provider_config_key": provider_config_key,
            "provider": provider,
            "oauth_client_id": oauth_client_id,
            "oauth_client_secret": oauth_client_secret,
            "oauth_scopes": oauth_scopes,
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.put(
                    f"{self._url}/config",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=body,
                    timeout=10,
                )
                return resp.status_code in (200, 201, 204)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Integration setup
    # ------------------------------------------------------------------

    async def ensure_integration(
        self,
        provider_config_key: str,
        provider: str,
        oauth_client_id: str,
        oauth_client_secret: str,
        oauth_scopes: str,
    ) -> bool:
        """Create or update a Nango integration with credentials and scopes.

        Idempotent — safe to call on every startup. Without scopes configured,
        Nango sends OAuth requests to Google with no scope parameter and Google
        rejects them immediately with a 400 error.

        Nango's API distinguishes create (POST) from update (PUT). PUT returns
        404 when the integration doesn't exist yet, so we check first and pick
        the right method.
        """
        import logging
        _log = logging.getLogger(__name__)
        body: dict[str, Any] = {
            "provider_config_key": provider_config_key,
            "provider": provider,
            "oauth_client_id": oauth_client_id,
            "oauth_client_secret": oauth_client_secret,
            "oauth_scopes": oauth_scopes,
        }
        try:
            async with httpx.AsyncClient() as client:
                exists_resp = await client.get(
                    f"{self._url}/config/{provider_config_key}",
                    headers=self._headers,
                    timeout=10,
                )
                exists = exists_resp.status_code == 200
                method = "put" if exists else "post"
                resp = await getattr(client, method)(
                    f"{self._url}/config",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=body,
                    timeout=10,
                )
                if resp.status_code not in (200, 201, 204):
                    _log.warning(
                        "ensure_integration %s %s → %d: %s",
                        method.upper(), provider_config_key,
                        resp.status_code, resp.text[:300],
                    )
                    return False
                return True
        except Exception as exc:
            _log.warning("ensure_integration exception: %s", exc)
            return False

    async def setup_configured_integrations(self) -> None:
        """Register all integrations whose credentials are present in env vars.

        Called once at startup. Each provider block is independent — a missing
        credential set skips that provider without affecting others.
        Logs outcome per provider; failures are non-fatal (Nango may already
        have the integration from a previous startup).
        """
        import logging
        _log = logging.getLogger(__name__)

        # Google — Calendar + Gmail scopes sufficient for all Mithra use cases.
        google_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        google_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        if google_id and google_secret:
            ok = await self.ensure_integration(
                provider_config_key=self.GOOGLE_PROVIDER,
                provider="google",
                oauth_client_id=google_id,
                oauth_client_secret=google_secret,
                oauth_scopes=" ".join([
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/userinfo.profile",
                    "https://www.googleapis.com/auth/calendar",
                    "https://www.googleapis.com/auth/calendar.events",
                ]),
            )
            _log.info("Nango %s integration: %s", self.GOOGLE_PROVIDER, "ok" if ok else "failed")

    async def create_connect_session(
        self,
        user_id: str,
        household_id: str,
        provider: str,
        display_name: str = "",
        email: str = "",
    ) -> dict | None:
        """Create a Nango connect session.

        Returns {token, connect_link, expires_at, connection_id} or None.
        connect_link points to app.nango.dev (configured via NANGO_UI_URL in
        the nango service). The UI is a shell only — credentials stay in the
        self-hosted Nango PostgreSQL.
        """
        connection_id = self.get_connection_id(user_id, household_id)
        tags: dict[str, str] = {"end_user_id": connection_id}
        if display_name:
            tags["display_name"] = display_name
        if email:
            tags["email"] = email
        body: dict[str, Any] = {
            "tags": tags,
            "allowed_integrations": [provider],
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._url}/connect/sessions",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=body,
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    return {
                        "token": data.get("token"),
                        "connect_link": data.get("connect_link"),
                        "expires_at": data.get("expires_at"),
                        "connection_id": connection_id,
                    }
                return None
        except Exception:
            return None

    async def get_token(
        self, provider: str, user_id: str, household_id: str
    ) -> dict[str, Any] | None:
        """Fetch a fresh access token for the given provider and user.

        Returns a dict with access_token, refresh_token on success,
        or None if the connection does not exist or the request fails.
        """
        connection_id = self.get_connection_id(user_id, household_id)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._url}/connection/{connection_id}",
                    headers=self._headers,
                    params={"provider_config_key": provider},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    creds = data.get("credentials", {})
                    return {
                        "access_token": creds.get("access_token"),
                        "refresh_token": creds.get("refresh_token"),
                    }
                return None
        except Exception:
            return None

    async def is_connected(
        self, provider: str, user_id: str, household_id: str
    ) -> bool:
        """Return True if Nango has a valid connection for this user."""
        token = await self.get_token(provider, user_id, household_id)
        return token is not None and bool(token.get("access_token"))

    def health(self) -> dict:
        """Return Nango client health status."""
        return {
            "status": "ok" if self._url and self._secret else "not_configured",
            "url": self._url,
            "configured": bool(self._secret),
        }
