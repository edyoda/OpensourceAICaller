"""One live call: mic in -> Sarvam STT -> Claude -> Sarvam TTS -> audio out."""

import asyncio
import base64

import logging
import time

from anthropic import AsyncAnthropic
from fastapi import WebSocketDisconnect
from sarvamai import AsyncSarvamAI

from .config import settings
from .llm import Conversation
from .rag import CourseSearch
from .transport import Disconnected, ExotelTransport, Transport
from .tts import TtsStream

log = logging.getLogger(__name__)


class CallSession:
    def __init__(self, transport: Transport, sarvam: AsyncSarvamAI, claude: AsyncAnthropic,
                 courses: CourseSearch | None = None) -> None:
        self._client = transport
        self._sarvam = sarvam
        self._conversation = Conversation(claude)
        self._courses = courses
        self._keepalive: asyncio.Task | None = None
        self._last_user = ""  # previous utterance, so "and its prerequisites?" still finds the course
        self._tts: TtsStream | None = None
        self._turn: asyncio.Task | None = None
        self._tts_turn: int | None = None  # the turn Sarvam is currently voicing
        self._spoken = ""  # what the caller has actually heard this turn
        # Per-turn latency probes. Voice work lives or dies on these numbers, so
        # they are logged on every call rather than hidden behind a debug flag.
        self._t_speech_end: float | None = None
        self._t_turn: float | None = None
        self._audio_logged = False

    async def run(self) -> None:
        async with (
            self._sarvam.speech_to_text_streaming.connect(
                model=settings.stt_model,
                language_code=settings.language_code,
                mode="transcribe",
                input_audio_codec="pcm_s16le",
                sample_rate=str(self._client.in_rate),
                vad_signals="true",  # START_SPEECH/END_SPEECH -- required for barge-in
                interrupt_min_speech_frames=settings.barge_in_min_speech_frames,
            ) as stt,
            self._sarvam.text_to_speech_streaming.connect(
                model=settings.tts_model,
                send_completion_event="true",  # gives us the per-turn `final` boundary
            ) as tts_ws,
        ):
            self._tts = TtsStream(tts_ws, self._send_audio, self._on_turn_complete)
            await self._tts.configure(self._client.out_rate)

            # Not in the wait set below: the greeting finishing is normal and
            # must not be read as the call ending.
            greeter = asyncio.create_task(self._greet(), name="greet")
            # The first search after an idle spell costs ~1s of connection setup.
            # Pay it while the greeting is playing, not while the caller waits.
            if self._courses is not None:
                self._keepalive = asyncio.create_task(
                    self._courses.keepalive(), name="rag-keepalive")

            tasks = [
                asyncio.create_task(self._pump_mic(stt), name="mic"),
                asyncio.create_task(self._read_stt(stt), name="stt"),
                asyncio.create_task(self._tts.reader(), name="tts"),
                asyncio.create_task(self._tts.pinger(), name="ping"),
            ]
            try:
                # The call lasts as long as every leg is healthy; the first one
                # to finish (hangup, or a failure) ends it.
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                # Whichever leg finishes first ends the call, so always say
                # which one and why -- a leg returning cleanly is just as
                # fatal as one that raises, and is far easier to miss.
                for task in done:
                    exc = task.exception()
                    if exc is None:
                        log.warning("call ended: leg %r returned cleanly", task.get_name())
                    elif isinstance(exc, (WebSocketDisconnect, Disconnected)):
                        extra = (self._client.stats()
                                 if isinstance(self._client, ExotelTransport) else "")
                        log.info("call ended: leg %r -> %s | %s",
                                 task.get_name(), exc, extra)
                    else:
                        log.exception("call ended: leg %r failed", task.get_name(), exc_info=exc)
            finally:
                greeter.cancel()
                if self._keepalive:
                    self._keepalive.cancel()
                await self._cancel_turn()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _greet(self) -> None:
        """Say hello before the caller does. Interruptible like any other turn."""
        if not settings.greeting or self._tts is None:
            return
        # Speak only once the caller's audio is actually flowing. Talking into
        # a media path Exotel has not finished wiring up is a plausible way to
        # lose the greeting, and costs nothing to rule out.
        if isinstance(self._client, ExotelTransport):
            try:
                await asyncio.wait_for(self._client.first_media.wait(), timeout=3.0)
                log.info("greeting: inbound media flowing, speaking now")
            except asyncio.TimeoutError:
                log.warning("greeting: no inbound media after 3s, speaking anyway")
        turn_id = self._tts.begin_turn()
        self._tts_turn = turn_id
        self._t_turn = time.perf_counter()
        self._audio_logged = False
        await self._send_json({"type": "reply_text", "text": settings.greeting})
        await self._tts.feed(turn_id, settings.greeting)
        await self._tts.end_turn(turn_id)
        self._conversation.seed_assistant(settings.greeting)

    # --- caller audio -> Sarvam -----------------------------------------
    async def _pump_mic(self, stt) -> None:
        while True:
            frame = await self._client.receive_audio()  # PCM16 mono @ in_rate
            await stt.transcribe(
                audio=base64.b64encode(frame).decode(),
                encoding="audio/wav",
                sample_rate=self._client.in_rate,
            )

    # --- Sarvam transcripts -> turns ------------------------------------
    async def _read_stt(self, stt) -> None:
        async for message in stt:
            if message.type == "events":
                if message.data.signal_type == "START_SPEECH":
                    await self._on_caller_started_speaking()
                elif message.data.signal_type == "END_SPEECH":
                    self._t_speech_end = time.perf_counter()
            elif message.type == "data":
                text = (message.data.transcript or "").strip()
                if text:
                    if self._t_speech_end:
                        log.info("stt tail: %.0fms (end of speech -> transcript)",
                                 (time.perf_counter() - self._t_speech_end) * 1000)
                    await self._send_json({"type": "transcript", "role": "user", "text": text})
                    await self._start_turn(text)
            elif message.type == "error":
                log.error("sarvam stt error: %s", message.data)

    async def _on_caller_started_speaking(self) -> None:
        """Barge-in: the caller talked over the agent, so stop talking.

        Three separate things are "talking" and all of them have to stop. The
        LLM task usually finishes generating seconds before the caller stops
        hearing the reply, so gating this on the task alone lets the agent keep
        speaking straight through an interruption.
        """
        speaking = (self._tts is not None and self._tts.speaking) or (
            self._turn is not None and not self._turn.done()
        )
        if not speaking:
            # Nothing to interrupt. Sending `clear` anyway is not free on a
            # phone line, where line noise trips the VAD constantly.
            return
        log.info("barge-in")

        await self._cancel_turn()  # 1. stop generating text
        if self._tts is not None and self._tts_turn is not None:
            await self._tts.cancel(self._tts_turn)  # 2. stop synthesising audio
        # 3. drop audio already queued at the far end -- the browser's jitter
        # buffer, or Exotel's playout buffer via a `clear` event. Sent
        # unconditionally: Sarvam finishes generating well before playback ends,
        # so buffered speech outlives `tts.speaking`.
        await self._client.clear()

    async def _start_turn(self, user_text: str) -> None:
        await self._cancel_turn()
        self._turn = asyncio.create_task(self._handle_turn(user_text), name="turn")

    async def _cancel_turn(self) -> None:
        if self._turn and not self._turn.done():
            self._turn.cancel()
            await asyncio.gather(self._turn, return_exceptions=True)
        self._turn = None

    async def _handle_turn(self, user_text: str) -> None:
        assert self._tts is not None
        turn_id = self._tts.begin_turn()
        self._tts_turn = turn_id
        self._spoken = ""
        self._t_turn = time.perf_counter()
        self._audio_logged = False
        interrupted = False
        try:
            await self._send_json({"type": "speech_start"})
            context = None
            if self._courses is not None:
                # A short follow-up has no course name in it; borrow the last turn's.
                query = user_text if len(user_text.split()) >= 6 else f"{self._last_user} {user_text}"
                context = await self._courses.context_for(query)
            self._last_user = user_text
            async for chunk in self._conversation.respond(user_text, context):
                if not self._spoken:
                    log.info("llm first chunk: %.0fms",
                             (time.perf_counter() - self._t_turn) * 1000)
                self._spoken += chunk + " "
                await self._send_json({"type": "reply_text", "text": chunk})
                await self._tts.feed(turn_id, chunk)
            await self._tts.end_turn(turn_id)
        except asyncio.CancelledError:
            interrupted = True
            # Shield the cleanup: we are already being cancelled, so these
            # awaits need their own task to survive long enough to run.
            await asyncio.shield(asyncio.create_task(self._tts.cancel(turn_id)))
            self._conversation.commit_interruption(self._spoken)
            raise
        except Exception as exc:
            # Without this the task dies unobserved: the caller hears dead air,
            # the page sits on "thinking", and the only trace is asyncio's
            # "Task exception was never retrieved". Say so, and free the TTS turn.
            log.exception("turn failed")
            interrupted = True
            await asyncio.shield(asyncio.create_task(self._tts.cancel(turn_id)))
            self._conversation.drop_pending_user()
            await self._send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"[:400]})
        finally:
            if not interrupted:
                await self._send_json({"type": "speech_end"})

    async def _on_turn_complete(self, turn_id: int) -> None:
        """Sarvam finished this turn: push the tail and ask Exotel to confirm."""
        if isinstance(self._client, ExotelTransport):
            await self._client.flush()
            if settings.exotel_send_marks:
                await self._client.mark(f"turn-{turn_id}")

    # --- outbound --------------------------------------------------------
    async def _send_audio(self, pcm: bytes) -> None:
        if not self._audio_logged and self._t_turn:
            self._audio_logged = True
            log.info("tts first audio: %.0fms after turn start | DEAD AIR: %.0fms",
                     (time.perf_counter() - self._t_turn) * 1000,
                     (time.perf_counter() - self._t_speech_end) * 1000
                     if self._t_speech_end else float("nan"))
        await self._client.send_audio(pcm)

    async def _send_json(self, payload: dict) -> None:
        await self._client.notify(payload)
