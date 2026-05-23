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

    def __init__(self, nango_url: str = "", secret_key: str = "", public_key: str = "") -> None:
        self._url = (nango_url or os.environ.get("NANGO_URL", "http://nango:8080")).rstrip("/")
        self._secret = secret_key or os.environ.get("NANGO_SECRET_KEY", "")
        # Public key is the client-side identifier (safe to embed in URLs, like Stripe publishable key).
        self._public_key = public_key or os.environ.get("NANGO_PUBLIC_KEY", "")

    def get_connection_id(self, user_id: str, household_id: str) -> str:
        """Return the Nango connection ID for a user."""
        return f"{household_id}__{user_id}"

    def get_connect_url(self, provider: str, user_id: str, household_id: str) -> str:
        """Return the OAuth start URL for a user.

        Routes through the gateway's /oauth/connect proxy — Nango redirects
        the browser directly to the provider's consent screen.
        public_key is Nango's client-side identifier (safe in URLs, like Stripe publishable key).
        """
        import urllib.parse
        connection_id = self.get_connection_id(user_id, household_id)
        base = os.environ.get(
            "MITHRA_BASE_URL", "https://assistant.mithra-app.com"
        ).rstrip("/")
        url = f"{base}/oauth/connect/{provider}?connection_id={urllib.parse.quote(connection_id)}"
        if self._public_key:
            url += f"&public_key={urllib.parse.quote(self._public_key)}"
        return url

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
        # Use `tags` (the current field) rather than the deprecated `end_user` object.
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
                    token = data.get("token")
                    # Nango self-hosted doesn't serve the Connect UI frontend.
                    # Use Nango's cloud-hosted UI (app.nango.dev) which accepts
                    # self-hosted session tokens via the embedded SERVER_URL.
                    connect_ui = os.environ.get(
                        "NANGO_CONNECT_UI_URL", "https://app.nango.dev"
                    ).rstrip("/")
                    connect_link = f"{connect_ui}/?session_token={token}" if token else None
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
