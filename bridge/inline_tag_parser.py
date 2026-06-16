"""Streaming parser for inline-tag LLM output.

The LLM emits plaintext sprinkled with tags:

    <reads>STATE</reads>
    <emotion>ID</emotion>
    <gesture>FUNCTION_NAME</gesture>
    <tool_call name="TOOL">arguments_payload</tool_call>
    <think>reasoning...</think>            # Qwen3 passthrough (kept for compat; ignored on Llama)

The parser consumes tokens incrementally via ``feed(chunk)`` and returns a list
of events. Call ``finish()`` at end-of-stream to flush any trailing buffer.

Event kinds:
    {"kind": "text_delta",     "text": str}
    {"kind": "thinking_delta", "text": str}
    {"kind": "reads",          "state": str}
    {"kind": "emotion",        "id":   str}
    {"kind": "gesture",        "name": str}
    {"kind": "tool_call",      "name": str, "arguments": str, "id": str}
    {"kind": "flush"}                      # sentence terminator or tag boundary

Design:
    - Bounded look-ahead: if '<' isn't resolved within MAX_TAG_LOOKAHEAD chars,
      the '<' is flushed as literal and scanning resumes one char later.
    - No nesting. A new open tag of the same type auto-closes the prior
      (and emits a warning). Matches the user's back-to-back gesture example.
    - Malformed closes auto-close the active tag and log a warning.
    - Empty bodies are skipped silently.
    - Unclosed tag at stream-end: its literal representation is flushed as text.
    - No LLM repair round-trip. Graceful degradation beats a retry.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


_ATTR_RE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')


def _parse_attrs(blob: Optional[str]) -> dict:
    """Parse `key="val"` pairs from a tag's attribute span into a lowercased dict."""
    return {k.lower(): v for k, v in _ATTR_RE.findall(blob or "")}


def _merge_attrs_into_args(body: str, extra: dict) -> str:
    """Fold extra tag attributes into a tool_call's JSON arguments.

    The model sometimes writes a tool param as a TAG attribute instead of a JSON
    key — e.g. `<tool_call name="video_player" action="open">{"query": "..."}` —
    which used to leave `action` stranded (and, before the regex was widened,
    leaked the whole tag as text). We merge those stray attributes into the args
    so the tool still receives them; explicit JSON keys win on conflict.
    """
    body = (body or "").strip()
    if not extra:
        return body
    try:
        obj = json.loads(body) if body else {}
    except Exception:
        obj = None
    if isinstance(obj, dict):
        for k, v in extra.items():
            obj.setdefault(k, v)
        return json.dumps(obj)
    # Body isn't a JSON object — keep it if present, else synthesize from attrs.
    return body or json.dumps(extra)

_KNOWN_TAGS = frozenset({"reads", "emotion", "gesture", "tool_call", "think", "escalate"})
_SIMPLE_BODY_TAGS = frozenset({"reads", "emotion", "gesture"})  # close by exact </name>
_MAX_TAG_LOOKAHEAD = 128
_SENTENCE_TERMINATORS = frozenset(".!?")

_TAG_OPEN_RE = re.compile(
    r"<\s*(/?)\s*(reads|emotion|gesture|tool_call|think|escalate)\b"
    r"((?:\s+[A-Za-z_][\w-]*\s*=\s*\"[^\"]*\")*)"   # group 3: any attributes
    r"\s*(/?)\s*>",
    re.IGNORECASE,
)


class _State(Enum):
    TEXT = 1
    THINK_BODY = 2
    EMOTION_BODY = 3
    GESTURE_BODY = 4
    TOOL_CALL_BODY = 5
    READS_BODY = 6


class InlineTagParser:
    """Streaming parser. Feed tokens, receive events."""

    def __init__(self) -> None:
        self._buf: str = ""
        self._state: _State = _State.TEXT
        self._body_accum: str = ""
        self._tool_call_name: Optional[str] = None
        self._tool_call_extra_attrs: dict = {}
        self._pending: list[dict] = []
        self._last_text_ended_on_terminator: bool = False
        # Track the tail of the last emitted text_delta so finish() can detect
        # a trailing terminator even after _pending has been drained.
        self._last_emitted_text_tail: str = ""

    # ---------- public API ----------

    def feed(self, chunk: str) -> list[dict]:
        if not chunk:
            return []
        self._buf += chunk
        self._process()
        return self._drain()

    def finish(self) -> list[dict]:
        # Best-effort flush depending on state
        if self._state == _State.TEXT:
            # Emit any trailing unresolved '<' prefix (shouldn't happen often:
            # _process flushes on exceeded look-ahead).
            if self._buf:
                self._emit_text(self._buf)
                self._buf = ""
            # If the last emitted text ended in a terminator, the LLM's final
            # sentence closed cleanly — emit a synthetic flush so the downstream
            # chunker can flush that sentence as a boundary.
            tail = self._last_emitted_text_tail.rstrip()
            if tail and tail[-1] in _SENTENCE_TERMINATORS:
                prev_char = tail[-2] if len(tail) >= 2 else ""
                if not prev_char.isdigit():
                    self._pending.append({"kind": "flush"})
            self._last_emitted_text_tail = ""
        elif self._state == _State.THINK_BODY:
            # Flush remaining body as thinking text
            if self._body_accum:
                self._pending.append({"kind": "thinking_delta", "text": self._body_accum})
            self._body_accum = ""
            self._state = _State.TEXT
            log.warning("inline-tag: stream ended inside <think> body")
        elif self._state == _State.TOOL_CALL_BODY:
            # LLM forgot to emit </tool_call>. Fire the tool call anyway —
            # the body has the arguments; trailing close is just a formality.
            body = self._body_accum.strip()
            if body or self._tool_call_name:
                self._pending.append({
                    "kind": "tool_call",
                    "name": self._tool_call_name or "",
                    "arguments": _merge_attrs_into_args(body, self._tool_call_extra_attrs),
                    "id": f"tc_{uuid.uuid4().hex[:12]}",
                })
                self._emit_flush_on_tag_boundary()
                log.info("inline-tag: stream ended inside <tool_call>; firing anyway (name=%r)",
                         self._tool_call_name)
            self._body_accum = ""
            self._tool_call_name = None
            self._tool_call_extra_attrs = {}
            self._state = _State.TEXT
        elif self._state in (_State.EMOTION_BODY, _State.GESTURE_BODY, _State.READS_BODY):
            # Stream ended inside a simple tag body — the body is a short ID
            # (emotion name, gesture function, or reads state). Treat it as
            # an implicit close.
            tag = _state_to_tag(self._state)
            body = self._body_accum.strip()
            if body:
                if tag == "emotion":
                    self._pending.append({"kind": "emotion", "id": body})
                elif tag == "gesture":
                    self._pending.append({"kind": "gesture", "name": body})
                elif tag == "reads":
                    self._pending.append({"kind": "reads", "state": body})
                self._emit_flush_on_tag_boundary()
                log.info("inline-tag: stream ended inside <%s>; implicit close (value=%r)",
                         tag, body)
            self._body_accum = ""
            self._state = _State.TEXT
        return self._drain()

    # ---------- internals ----------

    def _drain(self) -> list[dict]:
        out = self._pending
        self._pending = []
        return out

    def _emit_text(self, text: str) -> None:
        """Emit a text_delta, inserting synthetic flush events at sentence terminators.

        A terminator counts as a sentence boundary only when followed by whitespace
        (or the stream is about to end — handled in finish()). During streaming we
        don't flush on terminator-at-end-of-buffer because the next token might be
        a digit (decimals like "$202.06") or a continuation ("e.g. more words").
        Similarly, ``.!?`` preceded AND followed by digits is likely a decimal or
        version number — not a sentence end.
        """
        if not text:
            return
        idx = 0
        while idx < len(text):
            term_idx = -1
            i = idx
            while i < len(text):
                if text[i] in _SENTENCE_TERMINATORS:
                    # Consume repeated terminators (e.g. "!!" or "?!")
                    j = i
                    while j + 1 < len(text) and text[j + 1] in _SENTENCE_TERMINATORS:
                        j += 1
                    # Must be followed by whitespace to qualify as a sentence boundary.
                    if j + 1 < len(text) and text[j + 1].isspace():
                        # Decimal check: digit on both sides means don't flush.
                        prev_char = text[i - 1] if i > 0 else ""
                        # (No next-char digit case possible here because we already
                        # checked whitespace follows — decimals fail that check.)
                        if not prev_char.isdigit():
                            term_idx = j
                    i = j + 1
                    if term_idx >= 0:
                        break
                else:
                    i += 1
            if term_idx < 0:
                # No terminator found — emit the rest as one delta, no flush.
                piece = text[idx:]
                self._pending.append({"kind": "text_delta", "text": piece})
                self._last_emitted_text_tail = piece[-4:]
                self._last_text_ended_on_terminator = False
                break
            else:
                piece = text[idx : term_idx + 1]
                self._pending.append({"kind": "text_delta", "text": piece})
                self._pending.append({"kind": "flush"})
                self._last_emitted_text_tail = ""  # already flushed
                self._last_text_ended_on_terminator = True
                idx = term_idx + 1

    def _emit_flush_on_tag_boundary(self) -> None:
        if self._last_text_ended_on_terminator:
            return
        tail = self._last_emitted_text_tail.rstrip()
        # No text emitted yet → nothing to flush. Spurious empty flushes
        # accumulate otherwise and inflate chunk counts in tests / logs.
        if not tail:
            return
        # Decimal guard: if the last emitted text ended on a digit+period
        # (e.g. "$39." while the LLM is about to continue with "75"), do NOT
        # flush at this tag boundary. Flushing here would split "$39.75"
        # across two chunks. The downstream chunker will flush at the next
        # genuine sentence boundary instead.
        if len(tail) >= 2 and tail[-1] == "." and tail[-2].isdigit():
            return
        # Mid-sentence guard: if the last emitted text does NOT end on a
        # sentence terminator (. ! ?), the LLM has emitted a tag in the
        # middle of a sentence. Flushing here would split the sentence
        # across two chunks (observed with Qwen3 on cron-scheduler replies:
        # "…the cron scheduler isn't" / "running."). The downstream chunker
        # will flush at the real terminator instead.
        if tail[-1] not in _SENTENCE_TERMINATORS:
            return
        self._pending.append({"kind": "flush"})
        self._last_text_ended_on_terminator = True  # ensure we don't double-flush

    def _process(self) -> None:
        """Consume self._buf into events. Stops when it needs more input."""
        while self._buf:
            if self._state == _State.TEXT:
                if not self._process_text():
                    return
            elif self._state == _State.THINK_BODY:
                if not self._process_body("think"):
                    return
            elif self._state == _State.EMOTION_BODY:
                if not self._process_body("emotion"):
                    return
            elif self._state == _State.GESTURE_BODY:
                if not self._process_body("gesture"):
                    return
            elif self._state == _State.READS_BODY:
                if not self._process_body("reads"):
                    return
            elif self._state == _State.TOOL_CALL_BODY:
                if not self._process_body("tool_call"):
                    return

    def _process_text(self) -> bool:
        """Consume buffer in TEXT state. Returns False when more input needed."""
        lt = self._buf.find("<")
        if lt < 0:
            # All text — emit and consume
            self._emit_text(self._buf)
            self._buf = ""
            return False  # nothing more to do; wait for more input

        if lt > 0:
            # Emit text up to the '<'
            self._emit_text(self._buf[:lt])
            self._buf = self._buf[lt:]
            # continue in TEXT state; now buffer starts with '<'

        # Attempt to parse tag at position 0
        m = _TAG_OPEN_RE.match(self._buf)
        if m:
            is_close = bool(m.group(1))
            tag = m.group(2).lower()
            attrs = _parse_attrs(m.group(3))
            attr_name = attrs.get("name")
            # Non-`name` attributes on a <tool_call> (e.g. a stray action="open")
            # are folded into the tool arguments rather than dropped.
            extra_attrs = {k: v for k, v in attrs.items() if k != "name"}
            is_self_close = bool(m.group(4))
            end = m.end()

            if is_close:
                # Stray closing tag in TEXT state — treat as literal
                log.debug("inline-tag: stray </%s> in text; flushing as literal", tag)
                self._emit_text(self._buf[:end])
                self._buf = self._buf[end:]
                return True

            if is_self_close:
                if tag == "tool_call":
                    # Self-closing tool_call with no body — the LLM treats
                    # no-arg tools like show_diary as `<tool_call name="X"/>`.
                    # Fire with empty arguments so the executor gets a chance.
                    if attr_name:
                        self._emit_flush_on_tag_boundary()
                        self._pending.append({
                            "kind": "tool_call",
                            "name": attr_name,
                            "arguments": _merge_attrs_into_args("", extra_attrs),
                            "id": f"tc_{uuid.uuid4().hex[:12]}",
                        })
                    else:
                        log.debug("inline-tag: self-closing <tool_call/> without name; ignored")
                elif tag == "escalate":
                    # Self-handoff signal: the model judged this beyond it. Emit
                    # once; the graph re-runs the turn on the deep model.
                    self._emit_flush_on_tag_boundary()
                    self._pending.append({"kind": "escalate"})
                else:
                    # Self-closing reads/emotion/gesture/think — ignore content, emit nothing
                    log.debug("inline-tag: self-closing <%s/> ignored", tag)
                self._buf = self._buf[end:]
                return True

            # Opening tag — enter body state
            self._emit_flush_on_tag_boundary()
            self._body_accum = ""
            if tag == "think":
                self._state = _State.THINK_BODY
            elif tag == "reads":
                self._state = _State.READS_BODY
            elif tag == "emotion":
                self._state = _State.EMOTION_BODY
            elif tag == "gesture":
                self._state = _State.GESTURE_BODY
            elif tag == "tool_call":
                self._state = _State.TOOL_CALL_BODY
                self._tool_call_name = attr_name or ""
                self._tool_call_extra_attrs = extra_attrs
            elif tag == "escalate":
                # Tolerate a non-self-closed <escalate> — it carries no body; emit
                # the signal and stay in TEXT (a trailing </escalate> is harmless).
                self._pending.append({"kind": "escalate"})
            self._buf = self._buf[end:]
            return True

        # Tag parse failed. Two reasons:
        #   (a) Not enough bytes yet (tag still streaming in)
        #   (b) It's a bogus '<' (e.g. "2<3") — treat as literal
        # Decide: if buffer is short AND contains no '>' yet, wait for more.
        gt = self._buf.find(">", 1)
        if gt < 0:
            if len(self._buf) > _MAX_TAG_LOOKAHEAD:
                # Bounded look-ahead exceeded — flush '<' as literal and continue
                self._emit_text(self._buf[0])
                self._buf = self._buf[1:]
                return True
            return False  # wait for more input

        # We have '>' but regex didn't match → unknown tag name or malformed.
        # Flush '<' as literal, resume scanning after.
        log.debug("inline-tag: unknown/malformed tag near %r — flushing '<' as literal",
                  self._buf[: min(gt + 1, 40)])
        self._emit_text(self._buf[0])
        self._buf = self._buf[1:]
        return True

    def _process_body(self, tag: str) -> bool:
        """Consume buffer looking for </tag>. Returns False when more input needed."""
        close_re = re.compile(rf"<\s*/\s*{tag}\s*>", re.IGNORECASE)
        close_m = close_re.search(self._buf)

        # For reads/emotion/gesture: a new open tag of the same type auto-closes
        # the active one (no-nesting rule). Prefer reopen when it comes BEFORE close.
        reopen_m = None
        if tag in ("reads", "emotion", "gesture"):
            reopen_re = re.compile(rf"<\s*{tag}\s*>", re.IGNORECASE)
            reopen_m = reopen_re.search(self._buf)

        if reopen_m and (not close_m or reopen_m.start() < close_m.start()):
            self._body_accum += self._buf[: reopen_m.start()]
            self._buf = self._buf[reopen_m.start():]
            self._close_body(tag, auto_closed=True)
            return True

        if close_m:
            self._body_accum += self._buf[: close_m.start()]
            self._buf = self._buf[close_m.end():]
            self._close_body(tag, auto_closed=False)
            return True

        # No close and no earlier reopen — consume all but a tail that might be
        # a partial close tag, wait for more input.
        tail_keep = 0
        for i in range(max(0, len(self._buf) - (len(tag) + 4)), len(self._buf)):
            if self._buf[i] == "<":
                tail_keep = len(self._buf) - i
                break
        if tail_keep == 0:
            self._body_accum += self._buf
            self._buf = ""
        else:
            self._body_accum += self._buf[:-tail_keep]
            self._buf = self._buf[-tail_keep:]
        return False

    def _close_body(self, tag: str, auto_closed: bool) -> None:
        body = self._body_accum.strip()
        if auto_closed:
            log.warning("inline-tag: <%s> auto-closed by next open tag (no </%s> seen)", tag, tag)

        if tag == "think":
            if self._body_accum:
                self._pending.append({"kind": "thinking_delta", "text": self._body_accum})
        elif tag == "reads":
            if body:
                self._pending.append({"kind": "reads", "state": body})
                self._emit_flush_on_tag_boundary()
        elif tag == "emotion":
            if body:
                self._pending.append({"kind": "emotion", "id": body})
                self._emit_flush_on_tag_boundary()
        elif tag == "gesture":
            if body:
                self._pending.append({"kind": "gesture", "name": body})
                self._emit_flush_on_tag_boundary()
        elif tag == "tool_call":
            if body or self._tool_call_name:
                self._pending.append({
                    "kind": "tool_call",
                    "name": self._tool_call_name or "",
                    "arguments": _merge_attrs_into_args(body, self._tool_call_extra_attrs),
                    "id": f"tc_{uuid.uuid4().hex[:12]}",
                })
                self._emit_flush_on_tag_boundary()
            self._tool_call_name = None
            self._tool_call_extra_attrs = {}

        self._body_accum = ""
        self._state = _State.TEXT


def _state_to_tag(s: _State) -> str:
    return {
        _State.THINK_BODY: "think",
        _State.READS_BODY: "reads",
        _State.EMOTION_BODY: "emotion",
        _State.GESTURE_BODY: "gesture",
        _State.TOOL_CALL_BODY: "tool_call",
    }.get(s, "?")
