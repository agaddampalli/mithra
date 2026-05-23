"""
NangoClient — thin wrapper around the Nango OAuth token management API.

Nango handles the full OAuth web flow (connect sessions, token exchange, auto-refresh).
Mithra calls create_connect_session() to get a connect_link, sends it to the user,
and calls get_token() to fetch fresh access tokens for API calls.

connection_id = f"{household_id}__{user_id}" — globally unique per household + user.
NANGO_UI_URL=https://app.nango.dev in the nango service makes Nango generate the
correct connect_link. Credentials stay in the self-hosted Nango PostgreSQL.
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
