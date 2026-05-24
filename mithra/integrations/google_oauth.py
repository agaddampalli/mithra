"""
Google OAuth integration using Authlib and google-api-python-client.

Uses Authlib AsyncOAuth2Client for the web OAuth flow.
Uses google-api-python-client (build()) for Calendar, Gmail, and People API calls.
Stores encrypted tokens per user_id in StorageAdapter domain="integrations".
"""

from __future__ import annotations

from authlib.integrations.httpx_client import AsyncOAuth2Client
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

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


class GoogleIntegration:
    """Handles Google OAuth and API access per user.

    Uses Authlib for the OAuth web flow.
    Uses google-api-python-client for API calls.
    Stores encrypted tokens per user_id in StorageAdapter.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        storage_adapter,
        cipher=None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._storage = storage_adapter
        self._cipher = cipher

    def get_auth_url(self, user_id: str) -> str:
        """Generate Google OAuth consent URL for this user."""
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
            state=user_id,
        )
        return uri

    async def exchange_code(self, code: str, user_id: str) -> dict:
        """Exchange auth code for tokens using Authlib."""
        async with AsyncOAuth2Client(
            client_id=self._client_id,
            client_secret=self._client_secret,
            redirect_uri=self._redirect_uri,
        ) as client:
            token = await client.fetch_token(
                _TOKEN_URL,
                code=code,
            )
        self._save_token(user_id, dict(token))
        return dict(token)

    def get_calendar_service(self, user_id: str):
        """Return an authorized Google Calendar service."""
        creds = self._get_credentials(user_id)
        if not creds:
            return None
        return build("calendar", "v3", credentials=creds)

    def get_gmail_service(self, user_id: str):
        """Return an authorized Gmail service."""
        creds = self._get_credentials(user_id)
        if not creds:
            return None
        return build("gmail", "v1", credentials=creds)

    def get_contacts_service(self, user_id: str):
        """Return an authorized People API service."""
        creds = self._get_credentials(user_id)
        if not creds:
            return None
        return build("people", "v1", credentials=creds)

    def is_connected(self, user_id: str) -> bool:
        """Return True if this user has a stored token."""
        return self._load_token(user_id) is not None

    def get_credentials(self, user_id: str) -> Credentials | None:
        """Return a valid Google Credentials object for this user, or None."""
        return self._get_credentials(user_id)

    def _get_credentials(self, user_id: str) -> Credentials | None:
        token = self._load_token(user_id)
        if not token:
            return None
        creds = Credentials(
            token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            token_uri=_TOKEN_URL,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=SCOPES,
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token["access_token"] = creds.token
            self._save_token(user_id, token)
        return creds

    def _save_token(self, user_id: str, token: dict) -> None:
        from mithra.storage.adapter import StorageRecord
        from datetime import datetime, timezone

        data = dict(token)
        if self._cipher:
            for key in ("access_token", "refresh_token"):
                if data.get(key):
                    data[key] = self._cipher.encrypt_field(data[key])
        self._storage.write(StorageRecord(
            domain="integrations",
            key=f"google:{user_id}",
            layer="credentials",
            data=data,
            user_id=user_id,
            version=1,
            updated_at=datetime.now(timezone.utc).isoformat(),
        ))

    def _load_token(self, user_id: str) -> dict | None:
        record = self._storage.read(
            "integrations",
            f"google:{user_id}",
            "credentials",
            user_id,
        )
        if not record:
            return None
        data = dict(record.data)
        if self._cipher:
            for key in ("access_token", "refresh_token"):
                if data.get(key):
                    try:
                        data[key] = self._cipher.decrypt_field(data[key])
                    except Exception:
                        return None
        return data
