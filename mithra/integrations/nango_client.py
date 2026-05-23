"""
NangoClient — thin wrapper around the Nango OAuth token management API.

Nango handles the full OAuth web flow (connect sessions, token exchange, auto-refresh).
Mithra calls create_connect_session() to get a hosted connect link, then get_token()
to fetch fresh access tokens for API calls.

connection_id = f"{household_id}__{user_id}" — globally unique per household + user.
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
        The connect_link is a hosted Nango URL the user opens to complete OAuth.
        """
        connection_id = self.get_connection_id(user_id, household_id)
        body: dict[str, Any] = {
            "end_user": {
                "id": connection_id,
                "display_name": display_name or user_id,
            },
            "allowed_integrations": [provider],
        }
        if email:
            body["end_user"]["email"] = email
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
                    token = data.get("token")
                    # Nango self-hosted returns localhost URL — rewrite to public proxy.
                    base = os.environ.get(
                        "MITHRA_BASE_URL", "https://assistant.mithra-app.com"
                    ).rstrip("/")
                    connect_link = f"{base}/connect-ui/?session_token={token}" if token else None
                    return {
                        "token": token,
                        "connect_link": connect_link,
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
