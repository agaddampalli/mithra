"""
TTS output adapter — Piper TTS via Home Assistant Google Cast.

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
from typing import AsyncGenerator

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
    # the device that spoke the question — Wyoming routes the
    # confirmation response back to this device
    expires_at: str


class TTSAdapter(OutputAdapter):
    """Sends text-to-speech output to smart speakers via Home Assistant."""

    SESSION_TIMEOUT_SECONDS = 30

    def __init__(self, ha_url: str, ha_token: str) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token
        self._pending_sessions: dict[str, VoiceConfirmationSession] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json",
        }

    async def speak(self, text: str, target_entity: str) -> None:
        """Synthesise text and play it on the target Home Assistant media player entity."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.ha_url}/api/services/tts/speak",
                headers=self._headers(),
                json={
                    "entity_id": "tts.piper",
                    "media_player_entity_id": target_entity,
                    "message": text,
                },
                timeout=15,
            )
            resp.raise_for_status()

    async def _speak_streaming(self, text_gen: AsyncGenerator[str, None], target_entity: str) -> str:
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
        """Speak the message on the default media player for the user."""
        target = f"media_player.{user_id}_speaker"
        try:
            await self.speak(message.text, target)
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
        """Speak the message with action labels appended as spoken options."""
        action_text = " ".join(
            f"Say {a.label} to {a.action}." for a in actions
        )
        full_text = f"{message.text} {action_text}"
        return await self.send(user_id, OutboundMessage(
            text=full_text,
            format=message.format,
            domain=message.domain,
            priority=message.priority,
        ))

    async def send_stream(
        self, user_id: str, stream: AsyncGenerator[str, None]
    ) -> DeliveryReceipt:
        """Stream TTS with sentence-boundary detection."""
        target = f"media_player.{user_id}_speaker"
        try:
            await self._speak_streaming(stream, target)
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

    def create_confirmation_session(
        self,
        request_id: str,
        action_index: int,
        user_id: str,
        device_id: str,
    ) -> VoiceConfirmationSession:
        """Create and store a voice confirmation session before speaking the question."""
        session_id = str(uuid.uuid4())
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self.SESSION_TIMEOUT_SECONDS)
        ).isoformat()
        session = VoiceConfirmationSession(
            session_id=session_id,
            request_id=request_id,
            action_index=action_index,
            user_id=user_id,
            device_id=device_id,
            expires_at=expires_at,
        )
        self._pending_sessions[session_id] = session
        return session

    def resolve_confirmation(self, session_id: str) -> VoiceConfirmationSession | None:
        """Wyoming listener calls this to retrieve and remove a pending session."""
        session = self._pending_sessions.pop(session_id, None)
        if session is None:
            return None
        # Check not expired
        if datetime.fromisoformat(session.expires_at) < datetime.now(timezone.utc):
            return None
        return session

    def cancel_expired_sessions(self) -> int:
        """Remove all sessions past their expiry. Return count cancelled."""
        now = datetime.now(timezone.utc)
        expired = [
            sid for sid, s in self._pending_sessions.items()
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
            "pending_confirmation_sessions": len(self._pending_sessions),
        }
