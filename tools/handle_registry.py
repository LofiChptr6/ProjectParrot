"""Opaque handle registry — keeps LLMs from seeing raw URLs or YouTube IDs.

Tool outputs containing URLs are post-processed: each URL (or embedded YouTube
video_id) is replaced by an opaque handle of the form ``<kind>:<nanoid8>``:

    vid:<8 chars>  → a YouTube video_id (11 chars) extracted from a URL
    img:<8 chars>  → an image URL (or known image-CDN URL)
    url:<8 chars>  → any other URL

The LLM sees the handle, not the URL. When it passes a handle back to another
tool (``video_player``, ``theme_propose``, ``show_card``, ``show_slides`` …),
the executor resolves the handle to the real value BEFORE the tool runs.

Consequences:
 * The LLM cannot hallucinate a URL or video_id — the only way it can produce
   one is to quote a handle it actually saw, and a handle only exists if the
   underlying tool output it. No more fabricated YouTube IDs, no more "404
   Video unavailable" modals, no more dead Unsplash links.
 * ``validate_url`` remains available for URLs the user types themselves, but
   is no longer the primary defence.

Registry lifetime: in-process singleton, 2 h TTL on each entry, 10 k-entry
cap. This is a single-user system — a shared registry across all tool calls
is the simple, correct scope. Crashes clear the registry; ongoing sessions
get fresh handles next turn.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from contextvars import ContextVar
from typing import Optional, Tuple

log = logging.getLogger("tools.handles")

# Per-task capture for /admin/eval. When non-None, newly minted (not
# deduplicated) handles get appended. None on the live path — zero overhead.
EVAL_HANDLES: ContextVar[list[str] | None] = ContextVar("eval_handles", default=None)

_TTL_SEC = 2 * 60 * 60
_MAX_ENTRIES = 10_000
_HANDLE_LEN = 8
_HANDLE_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_KINDS = ("vid", "img", "url", "num")

_HANDLE_EXACT_RE = re.compile(r"^(vid|img|url|num):([a-zA-Z0-9]{8})$")
_HANDLE_ANYWHERE_RE = re.compile(r"\b(vid|img|url|num):([a-zA-Z0-9]{8})\b")

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")
_IMG_EXT_RE = re.compile(r"\.(png|jpg|jpeg|webp|gif|avif|svg)$", re.IGNORECASE)

_IMG_CDN_MARKERS = (
    "images.unsplash.com",
    "cdn.pixabay.com",
    "upload.wikimedia.org/wikipedia",
    "images.pexels.com",
    "i.imgur.com",
    "live.staticflickr.com",
)

_URL_TRAILING_PUNCT = ".,;:!?)]}>\"'"


class HandleRegistry:
    """Process-singleton. Maps handle ↔ value, with TTL and dedup."""

    def __init__(self) -> None:
        self._store: dict[str, Tuple[str, float]] = {}
        self._reverse: dict[Tuple[str, str], str] = {}

    def _evict_expired(self) -> None:
        now = time.time()
        dead = [h for h, (_, t) in self._store.items() if now - t > _TTL_SEC]
        for h in dead:
            rec = self._store.pop(h, None)
            if rec is None:
                continue
            kind = h.split(":", 1)[0]
            self._reverse.pop((kind, rec[0]), None)

    def _evict_oldest(self, n: int) -> None:
        oldest = sorted(self._store.items(), key=lambda x: x[1][1])[:n]
        for h, (v, _) in oldest:
            kind = h.split(":", 1)[0]
            self._store.pop(h, None)
            self._reverse.pop((kind, v), None)

    def issue(self, kind: str, value: str) -> str:
        if kind not in _KINDS:
            raise ValueError(f"unknown handle kind: {kind!r}")
        value = (value or "").strip()
        if not value:
            raise ValueError("cannot issue handle for empty value")
        self._evict_expired()
        existing = self._reverse.get((kind, value))
        if existing and existing in self._store:
            self._store[existing] = (value, time.time())
            return existing
        if len(self._store) >= _MAX_ENTRIES:
            self._evict_oldest(_MAX_ENTRIES // 10)
        for _ in range(32):
            nid = "".join(secrets.choice(_HANDLE_ALPHABET) for _ in range(_HANDLE_LEN))
            handle = f"{kind}:{nid}"
            if handle not in self._store:
                self._store[handle] = (value, time.time())
                self._reverse[(kind, value)] = handle
                _cap = EVAL_HANDLES.get()
                if _cap is not None:
                    _cap.append(handle)
                return handle
        raise RuntimeError("handle id collision after 32 attempts")

    def resolve(self, handle: str) -> Optional[str]:
        if not handle:
            return None
        handle = handle.strip()
        if not _HANDLE_EXACT_RE.match(handle):
            return None
        rec = self._store.get(handle)
        if rec is None:
            return None
        value, created = rec
        if time.time() - created > _TTL_SEC:
            kind = handle.split(":", 1)[0]
            self._store.pop(handle, None)
            self._reverse.pop((kind, value), None)
            return None
        self._store[handle] = (value, time.time())
        return value

    def stats(self) -> dict:
        self._evict_expired()
        counts = {k: 0 for k in _KINDS}
        for h in self._store:
            counts[h.split(":", 1)[0]] = counts.get(h.split(":", 1)[0], 0) + 1
        return {"total": len(self._store), **counts}


_REGISTRY: Optional[HandleRegistry] = None


def get_registry() -> HandleRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = HandleRegistry()
    return _REGISTRY


def _classify_url(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    if _IMG_EXT_RE.search(path):
        return "img"
    if any(m in url for m in _IMG_CDN_MARKERS):
        return "img"
    return "url"


def substitute_urls_with_handles(text: str) -> str:
    """Replace every URL (and YouTube ID embedded in URLs) with an opaque handle.

    Idempotent — a handle already in the text passes through unchanged.
    """
    if not text or not isinstance(text, str):
        return text
    reg = get_registry()

    def _replace(m: re.Match) -> str:
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in _URL_TRAILING_PUNCT:
            trail = raw[-1] + trail
            raw = raw[:-1]
        if not raw:
            return trail

        yt = _YT_ID_RE.search(raw)
        if yt:
            handle = reg.issue("vid", yt.group(1))
            return handle + trail

        kind = _classify_url(raw)
        handle = reg.issue(kind, raw)
        return handle + trail

    return _URL_RE.sub(_replace, text)


def substitute_handles_in_text(text: str) -> tuple[str, list[str]]:
    """Resolve every `kind:id` handle embedded in ``text`` back to its stored value.

    Inverse of ``substitute_urls_with_handles``. Used just before TTS to swap
    placeholders Mocha emitted (e.g., ``num:abc12345``) for the real display
    value (e.g., ``$39.75``). Unknown/expired handles are replaced with an
    empty string and the handle string is appended to the returned list so
    the caller can log them as WARN signals (LLM emitted a handle that does
    not resolve → either hallucinated the handle or the registry expired).

    Returns ``(resolved_text, unmapped_handles)``.
    """
    if not text or not isinstance(text, str):
        return text, []
    reg = get_registry()
    unmapped: list[str] = []

    def _replace(m: re.Match) -> str:
        handle = m.group(0)
        resolved = reg.resolve(handle)
        if resolved is None:
            unmapped.append(handle)
            return ""
        return resolved

    return _HANDLE_ANYWHERE_RE.sub(_replace, text), unmapped


def resolve_handles_in_args(args):
    """Walk a JSON-like structure and replace any leaf string that is exactly a
    handle with its resolved value. Non-handle strings pass through unchanged.

    Applied by the executor before dispatching to the underlying tool, so tool
    implementations only ever see real URLs / IDs and can remain handle-naive.
    """
    if isinstance(args, dict):
        return {k: resolve_handles_in_args(v) for k, v in args.items()}
    if isinstance(args, list):
        return [resolve_handles_in_args(x) for x in args]
    if isinstance(args, str):
        s = args.strip()
        if _HANDLE_EXACT_RE.match(s):
            resolved = get_registry().resolve(s)
            if resolved is not None:
                return resolved
            log.warning("unknown/expired handle passed to tool: %s", s)
        return args
    return args
