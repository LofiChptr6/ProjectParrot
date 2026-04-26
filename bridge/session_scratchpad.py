"""
Session tool-call scratchpad — in-RAM ring buffer of every tool executed this
session. Feeds two consumers:

1. Mocha's prompt (via ``format_for_prompt``) so she has within-session recall
   of what's been tool-called — "play that again", "what was AAPL at earlier?"
2. The diary writer (``bridge/diary_writer.py``), which reads the day's
   scratchpad when composing the diary page.

The scratchpad is process-scoped and lives until bridge restart. Daily
finalisation clears it. Nothing persists; the diary is the durable form.

Design notes:
- Store compact strings (not dicts) so the formatter stays cheap; the diary
  writer re-parses these as it prefers.
- Args/result are truncated in the stored form (we don't need all 10KB of
  yfinance JSON for a "what did we do" recall).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Iterable, Optional

_MAX_ENTRIES = 120                 # ~day's worth of active use
_ARGS_PREVIEW_CHARS = 120
_RESULT_PREVIEW_CHARS = 160


class _Entry:
    __slots__ = ("ts", "tool_name", "args_preview", "result_preview")

    def __init__(self, tool_name: str, args_preview: str, result_preview: str) -> None:
        self.ts: float = time.time()
        self.tool_name: str = tool_name
        self.args_preview: str = args_preview
        self.result_preview: str = result_preview


_buffer: deque["_Entry"] = deque(maxlen=_MAX_ENTRIES)

# ---------------------------------------------------------------------------
#  Per-user scratchpad registry
# ---------------------------------------------------------------------------
_user_pads: dict[str, "deque[_Entry]"] = {}


def _get_pad(user_id: str | None) -> "deque[_Entry]":
    """Return the scratchpad buffer for a given user (creates on first use)."""
    if not user_id:
        return _buffer
    if user_id not in _user_pads:
        _user_pads[user_id] = deque(maxlen=_MAX_ENTRIES)
    return _user_pads[user_id]


def _shorten(s: str, n: int) -> str:
    """Single-line, length-capped preview of a value."""
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            import json
            s = json.dumps(s, separators=(",", ":"))
        except Exception:
            s = str(s)
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if len(s) > n:
        s = s[: n - 1] + "…"
    return s


def add(tool_name: str, arguments, result, user_id: str | None = None) -> None:
    """Append a tool-call record. Call from the tool executor on every
    successful dispatch. Invalid inputs are tolerated silently.
    """
    if not tool_name:
        return
    try:
        _get_pad(user_id).append(_Entry(
            tool_name=tool_name,
            args_preview=_shorten(arguments, _ARGS_PREVIEW_CHARS),
            result_preview=_shorten(result, _RESULT_PREVIEW_CHARS),
        ))
    except Exception:
        pass


def recent(n: int = 10, user_id: str | None = None) -> list[dict]:
    """Return the most recent N entries as dicts (newest first)."""
    items = list(_get_pad(user_id))[-n:][::-1]
    return [
        {
            "ts": e.ts,
            "tool_name": e.tool_name,
            "args_preview": e.args_preview,
            "result_preview": e.result_preview,
        }
        for e in items
    ]


def all_entries(user_id: str | None = None) -> list[dict]:
    """Return every entry (oldest first) — for the diary writer."""
    return [
        {
            "ts": e.ts,
            "tool_name": e.tool_name,
            "args_preview": e.args_preview,
            "result_preview": e.result_preview,
        }
        for e in _get_pad(user_id)
    ]


def clear(user_id: str | None = None) -> None:
    """Wipe all entries (called at day rollover after diary finalisation)."""
    _get_pad(user_id).clear()


def size(user_id: str | None = None) -> int:
    return len(_get_pad(user_id))


def format_for_prompt(max_entries: int = 8, user_id: str | None = None) -> str:
    """Render the most recent tool-call records as a compact block for the LLM.

    Returns an empty string if no entries — callers should skip injection in
    that case so they don't waste prompt tokens.

    Format example::

        Recent tool activity (newest first, last N calls):
        • video_player open id=vid:WutU1fmm → "Silence makes you stronger..." [3m ago]
        • get_stock_data SOXL 1mo → price=num:PSfL3OYR change=num:wwhz87Eh [8m ago]
    """
    pad = _get_pad(user_id)
    if not pad:
        return ""
    now = time.time()
    lines = []
    for rec in recent(max_entries, user_id=user_id):
        age_s = max(0, int(now - rec["ts"]))
        if age_s < 60:
            age_str = f"{age_s}s ago"
        elif age_s < 3600:
            age_str = f"{age_s // 60}m ago"
        else:
            age_str = f"{age_s // 3600}h ago"
        args = rec["args_preview"] or ""
        res = rec["result_preview"] or ""
        line = f"• {rec['tool_name']}({args}) → {res} [{age_str}]"
        lines.append(line)
    return "Recent tool activity this session (newest first):\n" + "\n".join(lines)
