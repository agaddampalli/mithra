"""
TTS output adapter -- Piper TTS via Home Assistant Google Cast.

Sends synthesised speech to smart speakers through Home Assistant's
tts.speak service. The wyoming-piper container handles synthesis;
Home Assistant routes audio to the target Google Home Mini via Cast.

Streaming: speaks on sentence boundary detection (., !, ?). Does not wait
for the full response before starting playback.

Level-1 confirmations via voice:
  A VoiceConfirmationSession is stored before speaking.
  The Wyoming listener resolves the session by session_id from the response.
  Session times out after 30 seconds with explicit cancellation.

Undo strategy: cannot undo TTS (Reversibility.NONE).

Env vars: HA_URL, HA_TOKEN
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncGenerator

import httpx

from mithra.adapters.messaging import (
    ActionButton,
    DeliveryReceipt,
    OutboundMessage,
    OutputAdapter,
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VoiceConfirmationSession:
    """Tracks a spoken Level-1 confirmation awaiting a voice response."""

    session_id: str
    request_id: str
    action_index: int
    user_id: str
    device_id: str
    # the device that spoke the question -- Wyoming routes the
    # confirmation response back to this device
    expires_at: str


class _NullContext:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *args: object) -> None:
        pass


class TTSAdapter(OutputAdapter):
    """Sends text-to-speech output to smart speakers via Home Assistant."""

    SESSION_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        ha_url: str = "http://homeassistant:8123",
        ha_token: str = "",
        media_player_entity: str = "media_player.google_home_mini",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token
        self._media_player_entity = media_player_entity
        self._http_client = http_client
        self._pending_sessions: dict[str, VoiceConfirmationSession] = {}
        # Keyed by session_id for resolve_confirmation(); also indexed by user_id below.
        self._pending: dict[str, VoiceConfirmationSession] = {}
        # Keyed by user_id for send_with_actions / get_pending_confirmation.

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }

    def _client(self):
        if self._http_client is not None:
            return _NullContext(self._http_client)
        return httpx.AsyncClient()

    async def speak(
        self, text: str, target_entity: str | None = None
    ) -> None:
        """Synthesise text and play it on the target Home Assistant media player."""
        entity = target_entity or self._media_player_entity
        async with self._client() as client:
            resp = await client.post(
                f"{self.ha_url}/api/services/tts/speak",
                headers=self._headers(),
                json={
                    "entity_id": self._media_player_entity,
                    "media_player_entity_id": entity,
                    "message": text,
                    "language": "en-US",
                },
                timeout=15,
            )
            resp.raise_for_status()

    async def _speak_streaming(
        self, text_gen: AsyncGenerator[str, None], target_entity: str
    ) -> str:
        """Buffer chunks and speak on sentence boundaries."""
        buffer = ""
        full_text = ""
        async for chunk in text_gen:
            buffer += chunk
            full_text += chunk
            sentences = _SENTENCE_END.split(buffer)
            if len(sentences) > 1:
                for sentence in sentences[:-1]:
                    if sentence.strip():
                        await self.speak(sentence.strip(), target_entity)
                buffer = sentences[-1]
        if buffer.strip():
            await self.speak(buffer.strip(), target_entity)
        return full_text

    async def send(self, user_id: str, message: OutboundMessage) -> DeliveryReceipt:
        """Speak the message on the configured media player entity."""
        try:
            await self.speak(message.text, self._media_player_entity)
            return DeliveryReceipt(
                channel="tts",
                message_id=str(uuid.uuid4()),
                delivered_at=_now(),
                success=True,
                undoable=False,
                undo_strategy=None,
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="tts",
                message_id="",
                delivered_at=_now(),
                success=False,
                undoable=False,
                undo_strategy=None,
                error=str(exc),
            )

    async def send_with_actions(
        self,
        user_id: str,
        message: OutboundMessage,
        actions: list[ActionButton],
    ) -> DeliveryReceipt:
        """Speak the question, create a VoiceConfirmationSession, prompt yes/no."""
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.SESSION_TIMEOUT_SECONDS)
        ).isoformat()
        session = VoiceConfirmationSession(
            session_id=str(uuid.uuid4()),
            request_id=message.reply_to_id or "",
            action_index=0,
            user_id=user_id,
            device_id=self._media_player_entity,
            expires_at=expires_at,
        )
        # Store by both user_id (for get_pending_confirmation) and session_id (for resolve_confirmation)
        self._pending[user_id] = session
        self._pending_sessions[session.session_id] = session

        full_text = f"{message.text} Say yes or no."
        return await self.send(user_id, OutboundMessage(text=full_text))

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Stream TTS with sentence-boundary detection."""
        try:
            await self._speak_streaming(stream, self._media_player_entity)
            return DeliveryReceipt(
                channel="tts",
                message_id=str(uuid.uuid4()),
                delivered_at=_now(),
                success=True,
                undoable=False,
                undo_strategy=None,
            )
        except Exception as exc:
            return DeliveryReceipt(
                channel="tts",
                message_id="",
                delivered_at=_now(),
                success=False,
                undoable=False,
                undo_strategy=None,
                error=str(exc),
            )

    async def undo(self, receipt: DeliveryReceipt) -> bool:
        """TTS cannot be undone (Reversibility.NONE)."""
        return False

    def get_pending_confirmation(
        self, user_id: str
    ) -> VoiceConfirmationSession | None:
        """Return active (non-expired) VoiceConfirmationSession for a user."""
        session = self._pending.get(user_id)
        if session is None:
            return None
        if datetime.fromisoformat(session.expires_at) < datetime.now(timezone.utc):
            del self._pending[user_id]
            return None
        return session

    def create_confirmation_session(
        self,
        request_id: str,
        action_index: int,
        user_id: str,
        device_id: str,
    ) -> VoiceConfirmationSession:
        """Create and store a voice confirmation session before speaking the question."""
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=self.SESSION_TIMEOUT_SECONDS)
        ).isoformat()
        session = VoiceConfirmationSession(
            session_id=str(uuid.uuid4()),
            request_id=request_id,
            action_index=action_index,
            user_id=user_id,
            device_id=device_id,
            expires_at=expires_at,
        )
        self._pending_sessions[session.session_id] = session
        return session

    def resolve_confirmation(
        self, session_id: str
    ) -> VoiceConfirmationSession | None:
        """Wyoming listener calls this to retrieve and remove a pending session."""
        session = self._pending_sessions.pop(session_id, None)
        if session is None:
            return None
        if datetime.fromisoformat(session.expires_at) < datetime.now(timezone.utc):
            return None
        return session

    def cancel_expired_sessions(self) -> int:
        """Remove all sessions past their expiry. Return count cancelled."""
        now = datetime.now(timezone.utc)
        expired = [
            sid
            for sid, s in self._pending_sessions.items()
            if datetime.fromisoformat(s.expires_at) < now
        ]
        for sid in expired:
            del self._pending_sessions[sid]
        return len(expired)

    def health(self) -> dict:
        """Return TTS adapter health."""
        return {
            "status": "ok",
            "ha_url": self.ha_url,
            "media_player_entity": self._media_player_entity,
            "pending_confirmation_sessions": len(self._pending_sessions),
        }
