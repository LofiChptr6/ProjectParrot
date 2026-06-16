"""Streaming route — drives InlineTagParser + chunked TTS pipeline.

Consumes a stream of LLM tokens and yields higher-level events suitable for
direct dispatch to TTS/frontend/tool-executor. Pure plumbing — no side effects,
no HTTP, no globals. The bridge wires this into its endpoints.

Event types:
    {"type": "thinking",    "text": str}       # <think>...</think> passthrough
    {"type": "reads",       "state": str}      # one-word emotional read of user
    {"type": "emotion",     "id": str}
    {"type": "gesture",     "name": str}
    {"type": "speech_chunk","text": str}       # ready-for-TTS prose chunk
    {"type": "tool_call",   "name": str, "arguments": str, "id": str}
    {"type": "end"}                            # last event before generator returns

Speech chunking:
    - Flush on sentence terminator (.!?) followed by whitespace (via parser flush)
    - Flush on tag boundary (reads/emotion/gesture/tool_call arriving)
    - Flush on soft-cap (180 chars) at a good break (,;:—) then whitespace
    - First-chunk aggressive flush: first .!? OR 12 words, whichever first
      (minimises TTFA)
    - Flush on stream end
"""

from __future__ import annotations

import logging
import re
from typing import AsyncIterator, Optional

from bridge.inline_tag_parser import InlineTagParser

log = logging.getLogger(__name__)

_SOFT_CAP_CHARS = 180
_FIRST_CHUNK_WORD_CAP = 12
_SOFT_BREAK_CHARS = frozenset(",;:—–")


def _find_soft_break(text: str) -> int:
    """Return index just after a 'good' break near the end of text, or -1."""
    # Prefer a comma/semicolon/colon/em-dash followed by space.
    best = -1
    for i in range(len(text) - 2, max(0, len(text) - 60), -1):
        if text[i] in _SOFT_BREAK_CHARS and i + 1 < len(text) and text[i + 1].isspace():
            best = i + 2  # consume the break char + the space
            break
    if best != -1:
        return best
    # Otherwise any whitespace near the end
    for i in range(len(text) - 1, max(0, len(text) - 60), -1):
        if text[i].isspace():
            return i + 1
    return -1


class _SpeechChunker:
    """Accumulates text across parser events and decides when to flush a chunk.

    Emits chunks via a yielder — caller passes in a list that gets appended to.
    """

    def __init__(self) -> None:
        self._buf: str = ""
        self._chunk_idx: int = 0
        self._had_first_chunk: bool = False

    def add(self, text: str) -> list[str]:
        """Append text to buffer. Returns any completed chunks."""
        if not text:
            return []
        self._buf += text
        return self._try_split()

    def _try_split(self) -> list[str]:
        out: list[str] = []
        # First chunk: flush aggressively (first .!? OR 12 words, whichever first)
        if not self._had_first_chunk:
            # Look for first sentence terminator + whitespace or end
            m = re.search(r"[.!?]+(?:\s|$)", self._buf)
            if m:
                piece = self._buf[: m.end()]
                out.append(piece)
                self._buf = self._buf[m.end():]
                self._had_first_chunk = True
                self._chunk_idx += 1
                return out + self._try_split()  # may have more to flush
            # Word-cap fallback
            words = self._buf.split()
            if len(words) >= _FIRST_CHUNK_WORD_CAP:
                # Find the char position after the 12th word
                joined = ""
                wcount = 0
                for i, ch in enumerate(self._buf):
                    if ch.isspace():
                        if joined.strip():
                            wcount += 1
                            if wcount >= _FIRST_CHUNK_WORD_CAP:
                                piece = self._buf[: i + 1]
                                out.append(piece)
                                self._buf = self._buf[i + 1:]
                                self._had_first_chunk = True
                                self._chunk_idx += 1
                                return out + self._try_split()
                    joined += ch

        # Steady state: flush on soft-cap at a good break
        while len(self._buf) >= _SOFT_CAP_CHARS:
            # Try a sentence terminator first
            m = re.search(r"[.!?]+(?:\s|$)", self._buf)
            if m and m.end() <= _SOFT_CAP_CHARS + 40:
                piece = self._buf[: m.end()]
                out.append(piece)
                self._buf = self._buf[m.end():]
                self._chunk_idx += 1
                continue
            # Soft break
            bp = _find_soft_break(self._buf)
            if bp > 0:
                piece = self._buf[:bp]
                out.append(piece)
                self._buf = self._buf[bp:]
                self._chunk_idx += 1
                continue
            # Nowhere to break — give up, flush the buffer as-is
            out.append(self._buf)
            self._buf = ""
            self._chunk_idx += 1
            break
        return out

    def flush_on_terminator(self) -> list[str]:
        """Called when the parser reports a sentence terminator."""
        # If the buffer ends with a terminator already (parser inserts flush
        # AFTER terminator), emit whatever's in buf.
        if self._buf.strip():
            piece = self._buf
            self._buf = ""
            self._had_first_chunk = True
            self._chunk_idx += 1
            return [piece]
        return []

    def flush_on_tag(self) -> list[str]:
        """Called before a tag event is emitted, so gesture/emotion align to a speech pause."""
        return self.flush_on_terminator()

    def flush_final(self) -> list[str]:
        if self._buf.strip():
            piece = self._buf
            self._buf = ""
            self._chunk_idx += 1
            return [piece]
        return []


async def drive_inline_stream(
    token_stream: AsyncIterator[str],
) -> AsyncIterator[dict]:
    """Parse ``token_stream``, yield high-level events.

    The caller is responsible for dispatching events (to TTS, to frontend, to
    tool executor, to monitor). This function is pure plumbing.
    """
    parser = InlineTagParser()
    chunker = _SpeechChunker()

    def _make_chunk(text: str) -> dict:
        return {"type": "speech_chunk", "text": text}

    async for token in token_stream:
        events = parser.feed(token)
        for ev in events:
            kind = ev["kind"]
            if kind == "text_delta":
                for chunk in chunker.add(ev["text"]):
                    yield _make_chunk(chunk)
            elif kind == "flush":
                for chunk in chunker.flush_on_terminator():
                    yield _make_chunk(chunk)
            elif kind == "thinking_delta":
                yield {"type": "thinking", "text": ev["text"]}
            elif kind == "reads":
                yield {"type": "reads", "state": ev["state"]}
            elif kind == "emotion":
                for chunk in chunker.flush_on_tag():
                    yield _make_chunk(chunk)
                yield {"type": "emotion", "id": ev["id"]}
            elif kind == "gesture":
                for chunk in chunker.flush_on_tag():
                    yield _make_chunk(chunk)
                yield {"type": "gesture", "name": ev["name"]}
            elif kind == "tool_call":
                for chunk in chunker.flush_on_tag():
                    yield _make_chunk(chunk)
                yield {
                    "type": "tool_call",
                    "name": ev["name"],
                    "arguments": ev["arguments"],
                    "id": ev["id"],
                }
            elif kind == "escalate":
                yield {"type": "escalate"}

    # Drain parser
    for ev in parser.finish():
        kind = ev["kind"]
        if kind == "text_delta":
            for chunk in chunker.add(ev["text"]):
                yield _make_chunk(chunk)
        elif kind == "flush":
            for chunk in chunker.flush_on_terminator():
                yield _make_chunk(chunk)
        elif kind == "thinking_delta":
            yield {"type": "thinking", "text": ev["text"]}
        elif kind == "reads":
            yield {"type": "reads", "state": ev["state"]}
        elif kind == "emotion":
            yield {"type": "emotion", "id": ev["id"]}
        elif kind == "gesture":
            yield {"type": "gesture", "name": ev["name"]}
        elif kind == "tool_call":
            yield {
                "type": "tool_call",
                "name": ev["name"],
                "arguments": ev["arguments"],
                "id": ev["id"],
            }
        elif kind == "escalate":
            yield {"type": "escalate"}

    for chunk in chunker.flush_final():
        yield _make_chunk(chunk)

    yield {"type": "end"}
