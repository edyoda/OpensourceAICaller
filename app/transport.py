"""Transports: what a call is attached to.

CallSession does not care whether the caller is a browser tab or a phone. It
needs four things -- inbound PCM, a way to push PCM back, a way to flush audio
already queued for playback (barge-in), and an optional side-channel for UI
events. Everything protocol-specific lives here.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Protocol

from fastapi import WebSocket

from .config import settings

log = logging.getLogger(__name__)


class Disconnected(Exception):
    """Caller hung up."""


class Transport(Protocol):
    in_rate: int   # sample rate of audio arriving from the caller
    out_rate: int  # sample rate the caller expects back

    async def prepare(self) -> None: ...
    async def receive_audio(self) -> bytes: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def clear(self) -> None: ...
    async def notify(self, event: dict) -> None: ...


class BrowserTransport:
    """The web client: raw binary PCM both ways, JSON for UI events."""

    def __init__(self, ws: WebSocket, in_rate: int, out_rate: int) -> None:
        self._ws = ws
        self.in_rate = in_rate
        self.out_rate = out_rate

    async def prepare(self) -> None:
        await self._ws.send_text(json.dumps({"type": "ready", "sample_rate": self.out_rate}))

    async def receive_audio(self) -> bytes:
        return await self._ws.receive_bytes()

    async def send_audio(self, pcm: bytes) -> None:
        await self._ws.send_bytes(pcm)

    async def clear(self) -> None:
        await self.notify({"type": "interrupt"})

    async def notify(self, event: dict) -> None:
        await self._ws.send_text(json.dumps(event))


class ExotelTransport:
    """Exotel AgentStream voicebot applet, bidirectional.

    Wire format is base64 PCM16 wrapped in JSON events. Two rules bite:
    outbound chunks must be a multiple of 320 bytes, and the caller's own
    sample rate is announced in the `start` event rather than configured by us.
    """

    CHUNK = 3200  # 100ms @16k. Exotel wants 3200..100000, multiple of 320.

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self.in_rate = 8000   # replaced by the `start` event
        self.out_rate = 8000
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.caller: str | None = None
        self._out = bytearray()
        self._sent_chunks = 0
        self._sent_bytes = 0
        self._next_send = 0.0  # monotonic deadline for real-time pacing
        self.first_media = asyncio.Event()
        self._in_count = 0

    async def prepare(self) -> None:
        """Read up to the `start` event, which carries the media format."""
        while True:
            event = await self._recv()
            kind = event.get("event")
            if kind == "connected":
                log.info("exotel: connected")
            elif kind == "start":
                start = event.get("start", {})
                self.stream_sid = event.get("stream_sid") or start.get("stream_sid")
                self.call_sid = start.get("call_sid")
                self.caller = start.get("from")
                allowed = {a.strip() for a in settings.exotel_allowed_accounts.split(",") if a.strip()}
                account = start.get("account_sid")
                if allowed and account not in allowed:
                    log.warning("exotel: rejecting unknown account_sid %r", account)
                    raise Disconnected("unrecognised account")
                fmt = start.get("media_format", {})
                rate = int(fmt.get("sample_rate", 8000))
                self.in_rate = self.out_rate = rate
                log.info("exotel: start call=%s from=%s %sHz stream_sid=%r",
                         self.call_sid, self.caller, rate, self.stream_sid)
                if not self.stream_sid:
                    # Every outbound message must echo this. Without it Exotel
                    # discards our audio and the caller hears silence.
                    log.error("exotel: NO stream_sid in start event: %s",
                              json.dumps(event)[:600])
                return
            elif kind == "stop":
                raise Disconnected("stopped before start")

    async def receive_audio(self) -> bytes:
        while True:
            event = await self._recv()
            kind = event.get("event")
            if kind == "media":
                self._in_count += 1
                if not self.first_media.is_set():
                    log.info("exotel: first inbound media frame")
                    self.first_media.set()
                return base64.b64decode(event["media"]["payload"])
            if kind == "stop":
                log.info("exotel: stop (%s) after %d inbound frames, %d chunks sent",
                         event.get("stop", {}).get("reason"), self._in_count, self._sent_chunks)
                raise Disconnected("call ended")
            elif kind == "dtmf":
                log.info("exotel: dtmf %s", event.get("dtmf", {}).get("digit"))
            elif kind == "mark":
                # Exotel echoes a mark once the audio queued before it has
                # actually been played out. Proof the caller heard something.
                log.info("exotel: MARK PLAYED %s", event.get("mark", {}).get("name"))
            elif kind == "clear":
                log.info("exotel: clear ack")
            else:
                # Never swallow this: an error event here is the only warning
                # we get that Exotel dislikes what we are sending.
                log.warning("exotel: UNHANDLED inbound event %r: %s",
                            kind, json.dumps(event)[:400])

    async def send_audio(self, pcm: bytes) -> None:
        """Repacketize into legal chunks. Sarvam's chunks won't align."""
        self._out += pcm
        while len(self._out) >= self.CHUNK:
            await self._emit(bytes(self._out[: self.CHUNK]))
            del self._out[: self.CHUNK]

    async def flush(self) -> None:
        """Send the tail once synthesis is done.

        Exotel wants chunks of at least 3200 bytes, so the remainder is padded
        with silence up to CHUNK rather than merely to the 320-byte boundary.
        """
        if not self._out:
            return
        tail = bytes(self._out)
        self._out.clear()
        await self._emit(tail + b"\x00" * (self.CHUNK - len(tail)))
        log.info("exotel: turn sent %d chunks / %.2fs audio",
                 self._sent_chunks, self._sent_bytes / 2 / self.out_rate)

    async def clear(self) -> None:
        """Barge-in: drop what Exotel has buffered for playback."""
        self._out.clear()
        self._next_send = 0.0  # cancelled audio must not hold back the next turn
        await self._send({"event": "clear", "stream_sid": self.stream_sid})

    async def notify(self, event: dict) -> None:
        log.debug("exotel notify: %s", event.get("type"))

    async def mark(self, name: str) -> None:
        await self._send({"event": "mark", "stream_sid": self.stream_sid,
                          "mark": {"name": name}})

    async def _emit(self, chunk: bytes) -> None:
        """Send one chunk, paced to real time.

        Sarvam synthesizes far faster than speech plays. Dumping a whole reply
        at once hands Exotel several seconds of audio in a burst, which its
        playout buffer is not obliged to keep. Meter it at the rate it is
        actually spoken.
        """
        duration = len(chunk) / 2 / self.out_rate
        now = time.monotonic()
        if self._next_send <= now:
            self._next_send = now  # first chunk, or we fell behind: no sleep
        else:
            await asyncio.sleep(self._next_send - now)
        self._next_send += duration

        self._sent_chunks += 1
        self._sent_bytes += len(chunk)
        await self._send({
            "event": "media",
            "stream_sid": self.stream_sid,
            "media": {"payload": base64.b64encode(chunk).decode()},
        })

    async def _send(self, payload: dict) -> None:
        await self._ws.send_text(json.dumps(payload))

    def stats(self) -> str:
        return (f"in={self._in_count} frames, out={self._sent_chunks} chunks / "
                f"{self._sent_bytes / 2 / self.out_rate:.2f}s audio")

    async def _recv(self) -> dict:
        try:
            return json.loads(await self._ws.receive_text())
        except (KeyError, ValueError) as exc:
            raise Disconnected(f"bad frame: {exc}") from exc
