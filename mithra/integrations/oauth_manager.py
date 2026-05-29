"""
OAuthManager — generic OAuth2 orchestration using Authlib.

No provider-specific code inside. All differences are config values.
Uses AsyncOAuth2Client for token refresh via ensure_active_token().
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.integrations.oauth_token_store import OAuthTokenStore

logger = logging.getLogger(__name__)

_REFRESH_BUFFER_SECONDS = 300  # refresh 5 minutes before expiry


class OAuthManager:
    """Generic OAuth2 manager. Works for any provider in OAUTH_PROVIDERS."""

    def __init__(
        self,
        providers: dict[str, dict],
        token_store: "OAuthTokenStore",
    ) -> None:
        self._providers = providers  # already-resolved provider configs
        self._token_store = token_store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_auth_url(self, provider_id: str, user_id: str, redirect_uri: str) -> str:
        """Return the authorization URL to redirect the user to."""
        cfg = self._get_provider(provider_id)
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        client = AsyncOAuth2Client(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            scope=" ".join(cfg["scopes"]),
            redirect_uri=redirect_uri,
        )
        url, _state = client.create_authorization_url(
            cfg["auth_url"],
            state=user_id,
            **cfg.get("auth_params", {}),
        )
        return url

    async def handle_callback(
        self,
        provider_id: str,
        code: str,
        user_id: str,
        redirect_uri: str,
    ) -> bool:
        """Exchange auth code for tokens, persist them. Returns True on success."""
        cfg = self._get_provider(provider_id)
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        async with AsyncOAuth2Client(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            scope=" ".join(cfg["scopes"]),
            redirect_uri=redirect_uri,
        ) as client:
            try:
                token = await client.fetch_token(
                    cfg["token_url"],
                    code=code,
                    grant_type="authorization_code",
                )
            except Exception as exc:
                logger.error("OAuth callback failed for %s/%s: %s", provider_id, user_id, exc)
                return False

        token_data = {
            "access_token":  token.get("access_token", ""),
            "refresh_token": token.get("refresh_token", ""),
            "token_type":    token.get("token_type", "Bearer"),
            "expires_at":    int(token.get("expires_at", 0)),
            "scopes":        token.get("scope", "").split() if isinstance(token.get("scope"), str) else [],
        }
        self._token_store.save(provider_id, user_id, token_data)
        logger.info("OAuth token saved for %s/%s", provider_id, user_id)
        return True

    async def get_token(self, provider_id: str, user_id: str) -> str | None:
        """Return a valid access token, refreshing if needed. None if not connected."""
        if provider_id not in self._providers:
            return None
        data = self._token_store.load(provider_id, user_id)
        if not data or not data.get("access_token"):
            return None

        expires_at = int(data.get("expires_at", 0))
        if expires_at and time.time() < expires_at - _REFRESH_BUFFER_SECONDS:
            return data["access_token"]

        # Token expired or about to expire — refresh
        refresh_token = data.get("refresh_token", "")
        if not refresh_token:
            logger.warning("No refresh_token for %s/%s — cannot refresh", provider_id, user_id)
            return data.get("access_token") or None

        cfg = self._get_provider(provider_id)
        from authlib.integrations.httpx_client import AsyncOAuth2Client

        async with AsyncOAuth2Client(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
        ) as client:
            try:
                token = await client.refresh_token(
                    cfg["token_url"],
                    refresh_token=refresh_token,
                )
            except Exception as exc:
                logger.error("Token refresh failed for %s/%s: %s", provider_id, user_id, exc)
                return data.get("access_token") or None

        updated = {
            "access_token":  token.get("access_token", data["access_token"]),
            "refresh_token": token.get("refresh_token", refresh_token),
            "token_type":    token.get("token_type", "Bearer"),
            "expires_at":    int(token.get("expires_at", 0)),
            "scopes":        data.get("scopes", []),
            "connected_at":  data.get("connected_at", ""),
        }
        self._token_store.save(provider_id, user_id, updated)
        return updated["access_token"]

    def get_token_data(self, provider_id: str, user_id: str) -> dict | None:
        """Return full token data dict for credential building.

        Returns None if not connected. Use get_token() for just the
        access token string (with auto-refresh).
        """
        return self._token_store.load(provider_id, user_id)

    def save_token_data(
        self,
        provider_id: str,
        user_id: str,
        token_data: dict,
    ) -> None:
        """Persist updated token data (e.g. after an in-flight credential refresh).

        Used by credentials wrappers that refresh outside OAuthManager's
        own get_token() flow — e.g. google-auth refreshing inside gcsa.
        """
        self._token_store.save(provider_id, user_id, token_data)

    def is_connected(self, provider_id: str, user_id: str) -> bool:
        return self._token_store.is_connected(provider_id, user_id)

    def disconnect(self, provider_id: str, user_id: str) -> None:
        self._token_store.delete(provider_id, user_id)

    def available_providers(self) -> list[str]:
        return list(self._providers.keys())

    def is_provider_configured(self, provider_id: str) -> bool:
        """Return True if the provider is in the configured providers list."""
        return provider_id in self._providers

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_provider(self, provider_id: str) -> dict:
        if provider_id not in self._providers:
            raise KeyError(f"OAuth provider '{provider_id}' is not configured")
        return self._providers[provider_id]
