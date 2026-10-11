"""Claude turn: history in, sentence-sized text chunks out.

The chunker matters more than it looks. Sarvam's TTS starts synthesising as
soon as it has text, so handing it a clause at a time is what makes the agent
feel like it answers instantly instead of after the full completion.
"""

import re
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam

from .config import settings

# Devanagari danda included -- Hindi replies end sentences with it.
_BOUNDARY = re.compile(r"[.!?।](?=\s|$)|[\n]")
# The first chunk of a turn may also break at a clause. Waiting for a full stop
# can cost seconds of dead air on a long opening sentence, and the caller hears
# that as the agent being slow. Later chunks keep sentence boundaries, since by
# then audio is already playing and prosody matters more than speed.
_CLAUSE = re.compile(r"[.!?।,;:—](?=\s)|[\n]")
_MIN_CHUNK = 15
_MIN_FIRST_CHUNK = 20  # a clause shorter than this is not worth speaking alone


class Conversation:
    """One caller's dialogue with Claude."""

    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client
        self.messages: list[MessageParam] = []

    async def respond(self, user_text: str, context: str | None = None) -> AsyncIterator[str]:
        """Stream Claude's reply as speakable chunks, accumulating history."""
        self.messages.append({"role": "user", "content": user_text})

        buffer = ""
        spoken: list[str] = []

        # Thinking and effort are Opus/Fable-class controls; Haiku rejects them.
        extra: dict = {}
        if not settings.llm_model.startswith("claude-haiku"):
            extra["thinking"] = {"type": "adaptive"}
            extra["output_config"] = {"effort": settings.llm_effort}

        async with self._client.messages.stream(
            model=settings.llm_model,
            max_tokens=settings.max_tokens,
            system=_system(context),
            messages=self.messages,
            **extra,
            # Add tools=[...] here to let the agent look things up mid-call;
            # switch to client.beta.messages.tool_runner to drive the loop.
        ) as stream:
            async for text in stream.text_stream:
                buffer += text
                while (chunk := _take_sentence(buffer, first=not spoken)) is not None:
                    chunk, buffer = chunk
                    spoken.append(chunk)
                    yield chunk

        if buffer.strip():
            spoken.append(buffer.strip())
            yield buffer.strip()

        self.messages.append({"role": "assistant", "content": " ".join(spoken)})

    def seed_assistant(self, text: str) -> None:
        """Record something we said that Claude did not generate (the greeting)."""
        self.messages.append({"role": "assistant", "content": text})

    def drop_pending_user(self) -> None:
        """A turn failed before Claude answered: forget the unanswered question."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def commit_interruption(self, spoken_so_far: str) -> None:
        """Caller talked over us: record only what they actually heard.

        Without this the history claims Claude said a full paragraph the caller
        never heard, and the next turn references things that were never said.
        """
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages[-1]["content"] = (
                spoken_so_far.strip() or "(interrupted)"
            )
        else:
            self.messages.append(
                {"role": "assistant", "content": spoken_so_far.strip() or "(interrupted)"}
            )


def _system(context: str | None) -> str:
    """System prompt plus this turn's course facts. Facts are not kept in history."""
    base = settings.full_system_prompt
    if not settings.pinecone_api_key:
        return base
    return f"{base}\n\n{context}" if context else (
        f"{base}\n\n(No course information was found for this question.)")


def _take_sentence(buffer: str, first: bool = False) -> tuple[str, str] | None:
    """Split off one speakable chunk, or None if the buffer is not ready."""
    pattern = _CLAUSE if first else _BOUNDARY
    minimum = _MIN_FIRST_CHUNK if first else _MIN_CHUNK
    for match in pattern.finditer(buffer):
        end = match.end()
        head = buffer[:end].strip()
        if len(head) >= minimum:
            return head, buffer[end:]
    return None
