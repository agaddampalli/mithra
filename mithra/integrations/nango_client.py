"""
NangoClient — thin async wrapper around the Nango OAuth token management API.

Flow:
  1. create_connect_session() → returns connect_link, sent directly to user
  2. User completes OAuth in browser → Nango fires webhook to /api/v1/webhook/nango
  3. Webhook handler calls save_connection_id() with the Nango UUID
  4. get_token() calls load_connection_id() then fetches a fresh token from Nango

connection_id is the UUID Nango assigns at OAuth completion — it is NOT
predictable, so it must be stored from the webhook rather than constructed.
"""

from __future__ import annotations

import logging

import httpx

from mithra.storage.adapter import StorageRecord

_logger = logging.getLogger(__name__)


class NangoClient:
    """Async HTTP client for Nango token management."""

    GOOGLE_PROVIDER = "google-calendar"
    MICROSOFT_PROVIDER = "microsoft-graph"

    def __init__(
        self,
        secret_key: str,
        base_url: str = "http://nango:8080",
    ) -> None:
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret_key}"}

    # ------------------------------------------------------------------
    # Connect sessions
    # ------------------------------------------------------------------

    async def create_connect_session(
        self,
        user_id: str,
        household_id: str,
        provider: str,
    ) -> dict | None:
        """Create a Nango connect session. Returns {token, connect_link} or None."""
        body = {
            "tags": {"end_user_id": user_id},
            "allowed_integrations": [provider],
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/connect/sessions",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json=body,
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    return {
                        "token": data.get("token"),
                        "connect_link": data.get("connect_link"),
                    }
                _logger.error(
                    "create_connect_session failed: %d %s",
                    resp.status_code, resp.text[:300],
                )
                return None
        except Exception as exc:
            _logger.error("create_connect_session error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Token retrieval
    # ------------------------------------------------------------------

    async def get_token(
        self,
        provider: str,
        user_id: str,
        household_id: str,
        storage_adapter=None,
    ) -> dict | None:
        """Get a fresh access token for user from Nango.

        Looks up the stored Nango connection UUID first. Returns None (with a
        warning) if no connection has been saved yet — user must connect first.
        """
        connection_id = None
        if storage_adapter:
            connection_id = self.load_connection_id(user_id, household_id, storage_adapter)
        if not connection_id:
            _logger.warning(
                "No Nango connection_id stored for user=%s. "
                "User must connect via Telegram first.",
                user_id,
            )
            return None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/connection/{connection_id}",
                    headers=self._headers,
                    params={
                        "provider_config_key": provider,
                        "force_refresh": "true",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    creds = resp.json().get("credentials", {})
                    return {"access_token": creds.get("access_token")}
                _logger.error(
                    "get_token failed: %d %s", resp.status_code, resp.text[:300]
                )
                return None
        except Exception as exc:
            _logger.error("get_token error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Connection ID persistence
    # ------------------------------------------------------------------

    def save_connection_id(
        self,
        user_id: str,
        household_id: str,
        nango_connection_id: str,
        storage_adapter,
    ) -> None:
        """Persist the Nango UUID connection_id received from the OAuth webhook."""
        from datetime import datetime, timezone

        storage_adapter.write(StorageRecord(
            domain="integrations",
            key=f"nango_connection:{user_id}",
            layer="state",
            data={
                "nango_connection_id": nango_connection_id,
                "provider": self.GOOGLE_PROVIDER,
            },
            user_id=user_id,
            household_id=household_id,
            version=1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        ))
        _logger.info(
            "Saved connection_id=%s for user=%s", nango_connection_id, user_id
        )

    def load_connection_id(
        self,
        user_id: str,
        household_id: str,
        storage_adapter,
    ) -> str | None:
        """Load the stored Nango UUID connection_id for this user, or None."""
        record = storage_adapter.read(
            "integrations",
            f"nango_connection:{user_id}",
            "state",
            user_id,
            household_id,
        )
        if record:
            return record.data.get("nango_connection_id")
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def is_connected(
        self,
        provider: str,
        user_id: str,
        household_id: str,
        storage_adapter=None,
    ) -> bool:
        """Return True if Nango holds a valid token for this user."""
        token = await self.get_token(provider, user_id, household_id, storage_adapter)
        return token is not None and bool(token.get("access_token"))

    def health(self) -> dict:
        """Return Nango reachability status (synchronous probe)."""
        try:
            resp = httpx.get(f"{self._base_url}/health", timeout=5)
            return {
                "status": "ok" if resp.status_code == 200 else "error",
                "nango_reachable": resp.status_code == 200,
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
