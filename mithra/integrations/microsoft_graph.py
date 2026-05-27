"""
MicrosoftGraphClient — thin async wrapper around the Microsoft Graph API.

Handles file operations only. Authentication is delegated to OAuthManager
which manages token refresh. No separate Docker service required.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from mithra.integrations.oauth_manager import OAuthManager

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphClient:
    """Async Microsoft Graph API client for OneDrive file operations."""

    def __init__(self, oauth_manager: "OAuthManager") -> None:
        self._oauth = oauth_manager

    async def _headers(self, user_id: str) -> dict[str, str]:
        token = await self._oauth.get_token("microsoft", user_id)
        if not token:
            raise RuntimeError(
                f"No Microsoft token for {user_id}. "
                f"Send 'connect microsoft' to Mithra.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def upload_file(
        self,
        user_id: str,
        remote_path: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
    ) -> dict:
        """Upload content to OneDrive at remote_path. Returns the Graph item dict."""
        headers = await self._headers(user_id)
        headers["Content-Type"] = mime_type
        url = f"{_GRAPH_BASE}/me/drive/root:/{remote_path}:/content"
        async with httpx.AsyncClient() as client:
            resp = await client.put(url, headers=headers, content=content, timeout=60)
            resp.raise_for_status()
            return resp.json()

    async def download_file(self, user_id: str, item_id: str) -> bytes:
        """Download file content by Graph item ID."""
        headers = await self._headers(user_id)
        # First get the download URL
        url = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
        async with httpx.AsyncClient() as client:
            meta_resp = await client.get(url, headers=headers, timeout=30)
            meta_resp.raise_for_status()
            download_url = meta_resp.json().get("@microsoft.graph.downloadUrl")
            if not download_url:
                raise RuntimeError(f"No download URL for item {item_id}")
            content_resp = await client.get(download_url, timeout=60)
            content_resp.raise_for_status()
            return content_resp.content

    async def delete_file(self, user_id: str, item_id: str) -> bool:
        """Delete a file by Graph item ID. Returns True on success."""
        headers = await self._headers(user_id)
        url = f"{_GRAPH_BASE}/me/drive/items/{item_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return True

    async def list_folder(
        self, user_id: str, folder_path: str
    ) -> list[dict]:
        """List items in a OneDrive folder by path. Returns list of Graph item dicts."""
        headers = await self._headers(user_id)
        url = f"{_GRAPH_BASE}/me/drive/root:/{folder_path}:/children"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def create_folder(self, user_id: str, parent_path: str, name: str) -> dict:
        """Create a folder under parent_path. Returns the created item dict."""
        headers = await self._headers(user_id)
        if parent_path:
            url = f"{_GRAPH_BASE}/me/drive/root:/{parent_path}:/children"
        else:
            url = f"{_GRAPH_BASE}/me/drive/root/children"
        payload = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "rename",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()

    async def get_share_link(self, user_id: str, item_id: str) -> str:
        """Create a view-only sharing link and return the URL."""
        headers = await self._headers(user_id)
        url = f"{_GRAPH_BASE}/me/drive/items/{item_id}/createLink"
        payload = {"type": "view", "scope": "organization"}
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json()["link"]["webUrl"]
