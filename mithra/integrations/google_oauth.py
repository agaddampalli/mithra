"""
GoogleOAuth — minimal OAuth redirect flow and code exchange.

Handles only the browser-facing OAuth redirect and authorization code exchange.
Token storage and refresh are delegated entirely to Nango via import_token().
"""

from __future__ import annotations

from authlib.integrations.httpx_client import AsyncOAuth2Client

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "openid",
    "email",
    "profile",
]

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuth:
    """Generates the OAuth consent URL and exchanges the authorization code."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def get_auth_url(self, state: str) -> str:
        """Return the Google OAuth consent URL with state set to user_id."""
        client = AsyncOAuth2Client(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
            scope=" ".join(SCOPES),
        )
        uri, _ = client.create_authorization_url(
            _AUTH_URL,
            access_type="offline",
            prompt="consent",
            state=state,
        )
        return uri

    async def exchange_code(self, code: str) -> dict:
        """Exchange an authorization code for access + refresh tokens."""
        async with AsyncOAuth2Client(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
        ) as client:
            token = await client.fetch_token(
                _TOKEN_URL,
                code=code,
            )
        return dict(token)
