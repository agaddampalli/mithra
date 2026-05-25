"""
OAuthTokenStore — encrypted token persistence via StorageAdapter.

Storage schema:
    domain:  "oauth"
    key:     "{provider_id}:{user_id}"
    layer:   "credentials"
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)

_ENCRYPTED_FIELDS = ["access_token", "refresh_token"]


class OAuthTokenStore:
    """Encrypted OAuth token storage backed by StorageAdapter."""

    def __init__(self, storage_adapter: "StorageAdapter", household_id: str) -> None:
        self._storage = storage_adapter
        self._household_id = household_id

    def save(self, provider_id: str, user_id: str, token_data: dict) -> None:
        """Persist a token record. token_data should include access_token, refresh_token, etc."""
        from mithra.storage.adapter import StorageRecord

        now = datetime.now(timezone.utc).isoformat()
        data = {
            "provider":          provider_id,
            "user_id":           user_id,
            "household_id":      self._household_id,
            "access_token":      token_data.get("access_token", ""),
            "refresh_token":     token_data.get("refresh_token", ""),
            "token_type":        token_data.get("token_type", "Bearer"),
            "expires_at":        token_data.get("expires_at", 0),
            "scopes":            token_data.get("scopes", []),
            "connected_at":      token_data.get("connected_at", now),
            "last_refreshed":    now,
            "provider_user_id":  token_data.get("provider_user_id", ""),
        }

        record = StorageRecord(
            domain="oauth",
            key=f"{provider_id}:{user_id}",
            layer="credentials",
            data=data,
            user_id=user_id,
            version=1,
            updated_at=now,
        )
        self._storage.write(record)

    def load(self, provider_id: str, user_id: str) -> dict | None:
        """Load a token record, or None if not found."""
        record = self._storage.read(
            domain="oauth",
            key=f"{provider_id}:{user_id}",
            layer="credentials",
            user_id=user_id,
        )
        return record.data if record else None

    def delete(self, provider_id: str, user_id: str) -> None:
        """Remove a token record."""
        self._storage.delete(
            domain="oauth",
            key=f"{provider_id}:{user_id}",
            user_id=user_id,
        )

    def is_connected(self, provider_id: str, user_id: str) -> bool:
        """True if a token record exists for this provider + user."""
        data = self.load(provider_id, user_id)
        return data is not None and bool(data.get("access_token", ""))
