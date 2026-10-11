"""Sarvam streaming TTS on a single long-lived WebSocket.

Reopening the socket per turn costs a handshake on every reply, so we keep one
connection and delimit turns instead: each turn is a run of `convert()` calls
closed by a `flush()`, and Sarvam answers every flush with a `final` event.
That event is the boundary marker that makes barge-in possible -- when a caller
interrupts, we mark the in-flight turn dead and drop its audio until `final`
arrives, rather than tearing down the connection.
"""

import asyncio
import base64
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from sarvamai.types import AudioOutput, ErrorResponse, EventResponse

from .config import settings

log = logging.getLogger(__name__)

PING_INTERVAL = 20  # Sarvam drops idle sockets at ~60s


class TtsStream:
    def __init__(self, ws, on_audio: Callable[[bytes], Awaitable[None]],
                 on_turn_complete: Callable[[int], Awaitable[None]] | None = None) -> None:
        self._ws = ws
        self._on_audio = on_audio
        # Fired when Sarvam signals `final` -- the only moment we know a turn's
        # audio has fully arrived. Anything that must happen after the last
        # sample (flushing a repacketizer, marking the stream) belongs here.
        self._on_turn_complete = on_turn_complete
        self._turns: deque[int] = deque()  # in-flight turn ids, oldest first
        self._cancelled: set[int] = set()
        self._next_id = 0

    async def configure(self, sample_rate: int | None = None) -> None:
        """Synthesize at the transport's rate so nothing needs resampling.

        Sarvam allows 8k/16k/22.05k/24k; Exotel offers 8k/16k/24k. Every rate a
        phone call can ask for is one Sarvam can produce directly.
        """
        await self._ws.configure(
            target_language_code=settings.language_code,
            speaker=settings.tts_speaker,
            speech_sample_rate=sample_rate or settings.tts_sample_rate,
            output_audio_codec="linear16",  # raw PCM16: no decode step in the browser
            min_buffer_size=50,
            max_chunk_length=150,
        )

    def begin_turn(self) -> int:
        turn_id = self._next_id
        self._next_id += 1
        self._turns.append(turn_id)
        return turn_id

    async def feed(self, turn_id: int, text: str) -> None:
        if turn_id in self._cancelled:
            return
        await self._ws.convert(text)

    async def end_turn(self, turn_id: int) -> None:
        await self._ws.flush()

    async def cancel(self, turn_id: int) -> None:
        """Stop speaking a turn. Flush so its `final` boundary still arrives."""
        if turn_id in self._turns:
            self._cancelled.add(turn_id)
            await self._ws.flush()

    @property
    def speaking(self) -> bool:
        return bool(self._turns)

    async def reader(self) -> None:
        """Forward audio for the live turn; swallow audio for cancelled ones."""
        async for message in self._ws:
            if isinstance(message, AudioOutput):
                current = self._turns[0] if self._turns else None
                if current is not None and current not in self._cancelled:
                    await self._on_audio(base64.b64decode(message.data.audio))
            elif isinstance(message, EventResponse):
                if message.data.event_type == "final" and self._turns:
                    finished = self._turns.popleft()
                    was_cancelled = finished in self._cancelled
                    self._cancelled.discard(finished)
                    if self._on_turn_complete and not was_cancelled:
                        await self._on_turn_complete(finished)
            elif isinstance(message, ErrorResponse):
                log.error("sarvam tts error: %s", message.data)

    async def pinger(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await self._ws.ping()
