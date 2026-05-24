"""
OAuth proxy routes — browser-facing endpoints that bridge the Nango OAuth flow
to the internal Nango container.

Gateway is the single HTTP entry point. These routes proxy the OAuth callback
and the Nango SDK WebSocket channel so the browser can reach internal Nango:

  GET|POST /oauth/{path}  — HTTP proxy: OAuth callback → internal Nango
  WS   /                  — WebSocket proxy: Nango SDK realtime channel
  WS   /{path}            — WebSocket proxy: Socket.IO fallback

All routes are unauthenticated — the browser carries no Bearer token, and
Nango validates the connect session token internally.

Note: /connect/google was removed. Nango's create_connect_session() now returns
a connect_link directly; users are sent that link without any intermediary page.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import Response

_logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# HTTP proxy — OAuth connect + callback
# ---------------------------------------------------------------------------

@router.api_route("/oauth/{path:path}", methods=["GET", "POST"])
async def nango_oauth_proxy(request: Request, path: str) -> Response:
    """Proxy all Nango OAuth HTTP routes to the internal Nango container.

    /oauth/connect/{provider} — SDK popup lands here; Nango 302s to Google.
    /oauth/callback           — Google returns here; Nango exchanges code for tokens.
    follow_redirects=False is critical: the 302 to Google must go back to the
    browser, not be followed server-side.
    """
    import httpx
    nango_url = os.environ.get("NANGO_URL", "http://nango:8080")
    skip = {"host", "content-length", "transfer-encoding"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method=request.method,
            url=f"{nango_url}/oauth/{path}",
            params=dict(request.query_params),
            headers=headers,
            content=await request.body(),
            follow_redirects=False,
            timeout=30,
        )
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() != "transfer-encoding"}
    return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


# ---------------------------------------------------------------------------
# WebSocket proxy — Nango SDK realtime channel
# ---------------------------------------------------------------------------

async def _proxy_ws_to_nango(websocket: WebSocket, path: str) -> None:
    """Bidirectional WebSocket proxy to the internal Nango server.

    The Nango Frontend SDK opens a WebSocket to {host} before triggering the
    OAuth popup. It uses this channel to receive the OAuth result notification.
    {host} is the public gateway URL, so the gateway must proxy the connection
    to ws://nango:8080.
    """
    import websockets as _ws
    nango_url = os.environ.get("NANGO_URL", "http://nango:8080")
    ws_base = nango_url.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    target = f"{ws_base}/{path.lstrip('/')}"
    query = str(websocket.url.query)
    if query:
        target = f"{target}?{query}"

    await websocket.accept()
    try:
        async with _ws.connect(target) as upstream:
            async def _to_upstream() -> None:
                try:
                    while True:
                        raw = await websocket.receive()
                        if raw.get("type") == "websocket.disconnect":
                            break
                        if raw.get("bytes") is not None:
                            await upstream.send(raw["bytes"])
                        elif raw.get("text") is not None:
                            await upstream.send(raw["text"])
                except Exception:
                    pass

            async def _to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(_to_upstream()),
                asyncio.create_task(_to_client()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/")
async def ws_nango_root(websocket: WebSocket) -> None:
    """Proxy root WebSocket (Nango SDK realtime channel) to internal Nango."""
    await _proxy_ws_to_nango(websocket, "")


@router.websocket("/{path:path}")
async def ws_nango_path(websocket: WebSocket, path: str) -> None:
    """Proxy any other WebSocket path (e.g. /socket.io/) to internal Nango."""
    await _proxy_ws_to_nango(websocket, path)
