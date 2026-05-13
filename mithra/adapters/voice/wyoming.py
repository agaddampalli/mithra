"""
Wyoming protocol voice input adapter.

Receives audio events from the Home Assistant Assist pipeline via the Wyoming
protocol (port 10300). Converts recognised speech into a normalised AgentRequest
and dispatches it through the gateway lifecycle.

STT: Whisper via wyoming-whisper container.
Wake word: openWakeWord (handled by Home Assistant, not this adapter).
TTS: Piper via wyoming-piper container — handled by TTSAdapter.

Level-1 voice confirmations: resolved here against an active
VoiceConfirmationSession (stored by TTSAdapter) keyed by session_id.
30-second timeout with explicit cancellation.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Coroutine

from mithra.adapters.messaging import InputAdapter
from mithra.agent.interface import AgentRequest, InputChannel


class WyomingAdapter(InputAdapter):
    """Listens for Wyoming protocol events and converts them to AgentRequests."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 10300,
        on_request: Callable[[AgentRequest], Coroutine] | None = None,
        tts_adapter: Any | None = None,
        # TTSAdapter instance — used to resolve VoiceConfirmationSessions
    ) -> None:
        self.host = host
        self.port = port
        self._on_request = on_request
        self._tts = tts_adapter
        self._server: asyncio.AbstractServer | None = None
        self._running = False

    async def start(self) -> None:
        """Start listening for incoming Wyoming protocol connections."""
        self._running = True
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        asyncio.create_task(self._server.serve_forever())

    async def stop(self) -> None:
        """Gracefully stop the listener."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single Wyoming protocol connection from Home Assistant."""
        try:
            # Wyoming uses newline-delimited JSON frames
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                import json
                try:
                    frame = json.loads(line.decode())
                except Exception:
                    continue
                event_type = frame.get("type")
                if event_type == "transcript":
                    transcript = frame.get("data", {}).get("text", "")
                    device_id = frame.get("device_id", "wyoming-default")
                    user_id = frame.get("user_id", "household")
                    session_id = frame.get("session_id", "")

                    # Check if this resolves a VoiceConfirmationSession
                    if session_id and self._tts:
                        session = self._tts.resolve_confirmation(session_id)
                        if session:
                            request = AgentRequest(
                                user_id=session.user_id,
                                device_id=session.device_id,
                                input_channel=InputChannel.VOICE,
                                text=transcript,
                                request_id=str(uuid.uuid4()),
                                session_id=session_id,
                                signal={
                                    "confirmation_response": True,
                                    "request_id": session.request_id,
                                    "action_index": session.action_index,
                                },
                            )
                            if self._on_request:
                                await self._on_request(request)
                            continue

                    request = self.normalise(user_id, device_id, transcript)
                    if self._on_request:
                        asyncio.create_task(self._on_request(request))
        except Exception:
            pass
        finally:
            writer.close()

    def normalise(self, user_id: str, device_id: str, transcript: str) -> AgentRequest:
        """Convert a Wyoming transcript event into a normalised AgentRequest."""
        return AgentRequest(
            user_id=user_id,
            device_id=device_id,
            input_channel=InputChannel.VOICE,
            text=transcript,
            request_id=str(uuid.uuid4()),
            session_id=f"voice:{device_id}",
        )

    async def acknowledge(self, raw: Any) -> None:
        """Play a soft chime to signal Mithra is processing the voice command.

        In practice, HA Assist handles the acknowledgement sound. This is a no-op
        unless a chime URL is explicitly wired up.
        """

    def health(self) -> dict:
        """Return health status for the health dashboard."""
        return {
            "status": "ok" if self._running else "stopped",
            "host": self.host,
            "port": self.port,
        }
