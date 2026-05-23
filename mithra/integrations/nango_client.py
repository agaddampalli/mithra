"""
NangoClient — thin wrapper around the Nango OAuth token management API.

Nango handles the full OAuth web flow (consent URL, token exchange, auto-refresh).
Mithra calls Nango to get a fresh access token and passes it directly to API calls.

connection_id = f"{household_id}__{user_id}" — globally unique per household + user.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import httpx


class NangoClient:
    """Thin async HTTP client for Nango token management."""

    GOOGLE_PROVIDER = "google-calendar"
    MICROSOFT_PROVIDER = "microsoft-graph"

    def __init__(self, nango_url: str = "", secret_key: str = "", public_key: str = "") -> None:
        self._url = (nango_url or os.environ.get("NANGO_URL", "http://nango:8080")).rstrip("/")
        self._secret = secret_key or os.environ.get("NANGO_SECRET_KEY", "")
        self._public_key = public_key or os.environ.get("NANGO_PUBLIC_KEY", "")

    def get_connection_id(self, user_id: str, household_id: str) -> str:
        """Return the Nango connection ID for a user."""
        return f"{household_id}__{user_id}"

    def get_connect_url(
        self,
        provider: str,
        user_id: str,
        household_id: str,
        redirect_url: str = "",
    ) -> str:
        """Return a Nango Connect URL that starts the OAuth consent flow.

        Uses the public MITHRA_BASE_URL so the redirect goes through the
        api-gateway /oauth proxy — not the internal Docker Nango address.
        """
        connection_id = self.get_connection_id(user_id, household_id)
        public_url = os.environ.get(
            "MITHRA_BASE_URL", "https://assistant.mithra-app.com"
        ).rstrip("/")
        url = f"{public_url}/oauth/connect/{provider}?connection_id={urllib.parse.quote(connection_id)}"
        if self._public_key:
            url += f"&public_key={urllib.parse.quote(self._public_key)}"
        if redirect_url:
            url += f"&redirect_url={urllib.parse.quote(redirect_url)}"
        return url

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret}"}

    async def proxy_oauth_request(
        self,
        path: str,
        params: dict,
    ) -> tuple[bytes, int, dict]:
        """Proxy an OAuth request to Nango, injecting auth headers.

        Returns (content, status_code, headers). Gateway calls this instead
        of constructing the Nango URL and auth itself.
        """
        url = f"{self._url}/oauth/{path}"
        # /oauth/connect/* authenticates via public_key query param — no secret header.
        # /oauth/callback is handled internally by Nango — no auth header needed either.
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                follow_redirects=False,
                timeout=15,
            )
        filtered = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding",)
        }
        return resp.content, resp.status_code, filtered

    async def get_token(
        self, provider: str, user_id: str, household_id: str
    ) -> dict[str, Any] | None:
        """Fetch a fresh access token for the given provider and user.

        Returns a dict with access_token, token_type, expires_at on success,
        or None if the connection does not exist or the request fails.
        """
        connection_id = self.get_connection_id(user_id, household_id)
        url = f"{self._url}/connection/{connection_id}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"provider_config_key": provider},
                    headers=self._headers,
                    timeout=10,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                body = resp.json()
                creds = body.get("credentials", {})
                return {
                    "access_token": creds.get("access_token"),
                    "token_type": creds.get("token_type", "Bearer"),
                    "expires_at": creds.get("expires_at"),
                }
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
