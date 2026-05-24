"""
Google OAuth routes — browser-facing redirect and callback.

Option C flow:
  GET /auth/google           — redirect browser to Google consent screen
  GET /auth/google/callback  — receive auth code, exchange tokens, import into Nango

Both routes are unauthenticated (browser carries no Bearer token).
All provider-specific logic lives here, not in gateway.py.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

_logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/google")
async def google_auth(user_id: str = "household") -> RedirectResponse:
    """Redirect the browser to Google's OAuth consent screen."""
    from mithra.api.gateway import _state
    from mithra.integrations.nango_client import NangoClient

    google = _state.get("google_oauth")
    if not google:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")

    auth_url = google.get_auth_url(state=user_id)
    return RedirectResponse(auth_url)


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str = "",
    state: str = "household",
    error: str = "",
) -> HTMLResponse:
    """Receive Google's authorization code, exchange it, and import into Nango."""
    from mithra.api.gateway import _state
    from mithra.integrations.nango_client import NangoClient
    from mithra.adapters.messaging import OutboundMessage

    if error:
        _logger.warning("Google OAuth error: %s", error)
        return HTMLResponse(
            f"<h2>&#x274C; Auth failed: {error}</h2><p>Please try again.</p>",
            status_code=400,
        )

    google = _state.get("google_oauth")
    nango = _state.get("nango")
    local_adapter = _state.get("local_adapter")
    household_id = _state.get("household_id", "default")
    user_id = state

    if not google or not nango:
        raise HTTPException(status_code=503, detail="OAuth not configured")

    try:
        token = await google.exchange_code(code)
    except Exception as exc:
        _logger.error("Token exchange failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {exc}")

    expires_at = token.get("expires_at")
    if not expires_at:
        expires_at = int(time.time()) + 3600
    if isinstance(expires_at, (int, float)):
        expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()

    connection_id = await nango.import_token(
        user_id=user_id,
        household_id=household_id,
        provider=NangoClient.GOOGLE_PROVIDER,
        access_token=token.get("access_token", ""),
        refresh_token=token.get("refresh_token", ""),
        expires_at=expires_at,
        oauth_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        oauth_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        storage_adapter=local_adapter,
    )

    if connection_id:
        telegram_output = _state.get("telegram_output")
        identity = _state.get("identity_resolver")
        if telegram_output and identity:
            chat_id = identity.get_chat_id_for_user(user_id)
            if chat_id:
                telegram_output.register_chat(user_id, chat_id)
                try:
                    await telegram_output.send(
                        user_id,
                        OutboundMessage(text="✅ Google Calendar connected!"),
                    )
                except Exception as exc:
                    _logger.warning("Telegram notify failed after OAuth: %s", exc)

    return HTMLResponse("""<!DOCTYPE html>
<html><body style="font-family:sans-serif;text-align:center;padding:50px">
  <h2>&#x2705; Google Connected!</h2>
  <p>You can close this tab and return to Telegram.</p>
</body></html>""")
