"""
Bridge / Orchestrator — Connects STT, LLM, TTS, Memory, and browser clients.

Data flow:
  Mic -> [STT] -> text -> [Memory query + LLM] -> response -> [TTS] -> audio -> [browser]
                                                            -> [Memory store]

Speech pipeline:
  The LLM streams inline-tagged text; the parser cuts it into per-sentence
  chunks (bridge/inline_route.py). Each chunk is synthesized and emitted as
  soon as it exists; lip-sync alignment runs in the background and arrives as
  a follow-up `viseme_update` event (see graph._synth_and_emit).

Barge-in:
  When the user sends new input while a response is still being processed, the
  bridge cancels the in-flight generation task and sends an "interrupt" message
  to the client so it can stop playback.  The new input is then processed normally.

Debug state:
  The bridge broadcasts "debug_state" messages to all connected WebSocket
  clients at each pipeline stage with timing info.

Exposes:
  GET  /chat/stream          -- SSE streaming chat (dashboard + unified hub)
  POST /voice               -- audio in, text + audio out
  POST /channel             -- text channels (Telegram/Discord/CLI); optional tools
  WS   /ws/live             -- phone-call mode: stream PCM16; VAD → STT → LLM
  WS   /ws/voice-stream     -- WebSocket: chunked audio → STT → LLM
  WS   /ws/monitor          -- pipeline dashboard
  GET  /                     -- redirects to /monitor
  GET  /health              -- health check
  GET  /debug/state         -- current pipeline state + timing
"""

import asyncio
import base64
import dataclasses
import io
import json
import logging
import os
import random
import re
import shutil
import time
from collections import defaultdict
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth.models import CurrentUser, jwt_decode
from auth.routes import router as _auth_router
from auth.db import get_user_by_id as _auth_get_user_by_id
from auth import quota as _auth_quota

from animation.ingest import parse_actions_file
from .audio_utils import pcm16_mono_to_wav
from .llm_client import LLMClient
from .vad_segmenter import VadUtteranceSegmenter
from .call_log import CallContext
from . import call_log
from .inline_route import drive_inline_stream
from .inline_tag_parser import InlineTagParser
from character.context import build_chat_prompt, build_system_prompt
from tools.handle_registry import substitute_handles_in_text

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bridge")

ROOT = Path(__file__).resolve().parent.parent
full_config = yaml.safe_load((ROOT / "config.yaml").read_text())
config = full_config["bridge"]
llm_config = full_config["llm"]
anim_config = full_config.get("animation", {})
live_config = config.get("live", {})

# Runtime-adjustable config (updated via dashboard Config tab)
# `silence_ms` is the documented key in config.yaml's bridge.live block; honor it
# as the final-endpoint default (it was silently ignored for months — the config
# said 450 while the VAD actually waited the 900 default).
_cfg_vad_final_ms = int(live_config.get("silence_ms_final",
                                        live_config.get("silence_ms", 900)))
_cfg_vad_interim_ms = int(live_config.get("silence_ms_interim", 350))

# ---------------------------------------------------------------------------
#  Network addresses — derived from the central "network" block so every URL
#  stays consistent when you move from LAN to WAN or back.
# ---------------------------------------------------------------------------
_net = full_config.get("network", {})
INTERNAL_HOST = _net.get("internal_host", "127.0.0.1")

def _resolve_url(cfg_key: str, service_section: str, default_port: int) -> str:
    """Return explicit bridge.<cfg_key> if set, else derive from internal_host."""
    explicit = config.get(cfg_key)
    if explicit:
        return explicit.rstrip("/")
    port = full_config.get(service_section, {}).get("port", default_port)
    return f"http://{INTERNAL_HOST}:{port}"

config["stt_url"] = _resolve_url("stt_url", "stt", 8091)
_STT_ENABLED: bool = bool(full_config.get("stt", {}).get("enabled", True))
config["tts_url"] = _resolve_url("tts_url", "tts", 8092)
# memory: in-process via memory/mem0_store.py — no URL to resolve.
# animation: FBX function mode uses character/animation_functions.csv directly;
#   no HTTP service, no vector DB.

# LLM client (OpenAI-compatible API — vLLM, etc.)
llm_client = LLMClient(llm_config)
# Expose URL for logging / health display
config["llm_url"] = llm_client._base_url.rsplit("/v1", 1)[0]

# ── Model routing ──────────────────────────────────────────────────────────
# Default = the fast/friendly model above (Llama-3.2-3B, Mocha's own :8893).
# Deep = opus's shared Qwen-32B (smarter + better at the tool format), used for
# turns that need reasoning or data tools. Heuristic router (no extra LLM call,
# so no TTFT cost); the graph also auto-escalates to deep once a tool fires.
# The deep client inherits the isolation guard (it hits the shared trading vLLM).
_deep_cfg = {**llm_config, **(llm_config.get("deep") or {})}
llm_deep = (LLMClient(_deep_cfg)
            if (llm_config.get("deep") or {}).get("base_url") else llm_client)

# Fast→deep fallback (config llm.fallback_to_deep): when the local fast lane is
# down — e.g. the box's GPU is held by a training run and :8893 never came up —
# serve fast/background passes on the deep remote endpoint instead of failing.
LLM_FALLBACK_TO_DEEP = (bool(llm_config.get("fallback_to_deep"))
                        and llm_deep is not llm_client)


async def active_fast_client() -> LLMClient:
    """The client for fast/cheap passes (chat first pass, interpret, stall
    rephrase, diary, autonomy composer): the local fast lane when healthy,
    else the deep remote when fallback is enabled. TTL-cached health probe —
    effectively free when the local vLLM is up."""
    if LLM_FALLBACK_TO_DEEP:
        try:
            down = llm_client.circuit_open or not await llm_client.healthy_cached()
        except AttributeError:
            down = False  # test doubles / partial clients: treat as healthy
        if down:
            return llm_deep
    return llm_client
_routing_cfg = llm_config.get("routing", {}) or {}
_ROUTE_KEYWORDS = [k.lower() for k in _routing_cfg.get("keywords", [])]
_TICKER_RE = re.compile(r"\$[A-Za-z]{1,5}\b")  # $NVDA-style cashtags only

# Tool/lookup intent → route the FIRST (tool-emitting) pass to Qwen, not the 3B.
# This closes the gap where casual lookups ("got any science news") landed on the
# 3B, which fumbles the inline tool-call JSON. Kept narrow (genuine tool intent,
# not all chit-chat) so we don't needlessly load opus's shared Qwen.
_DEFAULT_TOOL_KEYWORDS = [
    "news", "headline", "weather", "forecast", "temperature", "look up", "lookup",
    "search", "google", "schedule", "remind", "reminder", "calendar", "diary",
    "show me", "pull up", "what's the", "whats the", "how much", "how many",
    "price of", "latest", "who won", "score", "stock", "chart", "play ",
    # Company names + market slang — "is nvidia still ripping?" has no cashtag
    # and no formal keyword; the name/slang IS the market signal. Substring
    # match, so short/common fragments (intel, ath) are deliberately absent.
    "nvidia", "tesla", "apple", "amazon", "alphabet", "microsoft", "meta",
    "facebook", "netflix", "broadcom", "qualcomm", "micron", "tsmc", "amd",
    "palantir", "boeing", "berkshire", "jpmorgan", "goldman", "blackrock",
    "nasdaq", "s&p", "dow ", "russell", "spy", "qqq", "soxl", "bitcoin",
    "btc", "ethereum", "crypto",
    "ripping", "ripped", "tanking", "tanked", "mooning", "dumping", "pumping",
    "rallying", "rally", "sell-off", "selling off", "sold off",
    "all time high", "all-time high", "new high", "new low",
    "crash", "crashing", "cool off", "cooled off", "earnings",
]
_TOOL_INTENT_KEYWORDS = [k.lower() for k in
                         _routing_cfg.get("tool_keywords", _DEFAULT_TOOL_KEYWORDS)]


# ── Held-ticker cache (portfolio awareness) ─────────────────────────────────
# So Mocha recognizes a ticker Ika mentions as one of the DESK's positions
# (e.g. BIRK = a holding, not a random stock) and grounds the answer instead of
# guessing. Read-only, lazy (refreshes only when Mocha is active), fail-safe —
# respects the prime directive (a stale/empty list just means less context).
_held_tickers: list[str] = []
_held_tickers_at: float = 0.0
_held_refresh_inflight: bool = False
_held_ticker_re: "re.Pattern | None" = None
_HELD_TTL_S = 300.0


# Held tickers that collide with everyday English words — these stay
# UPPERCASE-only in the route match; everything else matches case-insensitively
# (Ika types "soxl"/"nvda" in chat, and a missed held-name route is how the
# fast lane fabricated a SOXL price history on 07-04 — see
# docs/audits/2026-07-05-llm-reasoning-audit.md).
_AMBIGUOUS_TICKER_WORDS = frozenset({
    "ALL", "ANY", "ARE", "BE", "BY", "CAN", "CAT", "COST", "DAY", "EAT", "FAST",
    "FOR", "GO", "GOOD", "HAS", "HE", "IT", "KEY", "LOVE", "LOW", "MAIN", "MAN",
    "NEW", "NICE", "NOW", "ON", "ONE", "OPEN", "OR", "OUT", "PLAY", "REAL",
    "RUN", "SEE", "SO", "STAY", "TALK", "TELL", "TRIP", "TRUE", "TWO", "UP",
    "WELL", "WORK", "YOU",
})


def _rebuild_held_ticker_re() -> None:
    global _held_ticker_re
    safe = [re.escape(t) for t in _held_tickers
            if len(t) >= 2 and t not in _AMBIGUOUS_TICKER_WORDS]
    # Word-colliding tickers keep the case-SENSITIVE uppercase whole-word match
    # (ticker "ALL" must not match "all"); unambiguous ones match any case.
    risky = [re.escape(t) for t in _held_tickers
             if len(t) >= 2 and t in _AMBIGUOUS_TICKER_WORDS]
    parts = []
    if safe:
        parts.append(r"(?i:" + "|".join(safe) + r")")
    if risky:
        parts.append("|".join(risky))
    _held_ticker_re = (re.compile(r"\b(?:" + "|".join(parts) + r")\b")
                       if parts else None)


async def _refresh_held_tickers() -> None:
    global _held_tickers, _held_tickers_at, _held_refresh_inflight
    try:
        raw = await execute_tool("get_positions", {})
        data = json.loads(raw) if raw and raw.lstrip().startswith("{") else {}
        syms = [str(p.get("symbol")).strip().upper()
                for p in (data.get("positions") or []) if p.get("symbol")]
        if syms:
            _held_tickers = sorted(set(syms))
            _held_tickers_at = time.monotonic()
            _rebuild_held_ticker_re()
            log.info("held-tickers cache refreshed: %d positions", len(_held_tickers))
    except Exception as exc:
        log.debug("held-tickers refresh failed: %s", exc)
    finally:
        _held_refresh_inflight = False


def held_tickers() -> list[str]:
    """Cached held tickers (may be stale). Kicks a non-blocking refresh when
    stale; never blocks or raises."""
    global _held_refresh_inflight
    if (time.monotonic() - _held_tickers_at) > _HELD_TTL_S and not _held_refresh_inflight:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # no running loop (import-time / standalone) — skip refresh
        if loop is not None:
            _held_refresh_inflight = True
            loop.create_task(_refresh_held_tickers())
    return _held_tickers


def _mentions_held_ticker(text: str) -> bool:
    held_tickers()  # opportunistic refresh
    return bool(_held_ticker_re and text and _held_ticker_re.search(text))


# ── Ungrounded live-claim detector (deterministic backstop) ─────────────────
# The fast model has NO tools; any live-data value it states is fabricated by
# definition. The LLM verifier proved too forgiving here (a hedge like "around
# $640" reads as "softened" and passes), so this is a hard regex check: a
# number-ish claim in a live-data context, from a fast pass that ran no tools,
# forces an escalate-with-tools rerun instead of reaching the user.
_LIVE_NUMBER_RE = re.compile(
    r"\$\s?\d"                                  # dollar amounts
    r"|\d+(?:\.\d+)?\s?%"                       # percentages
    r"|\b(?:up|down|gained|lost|dropped|rose|fell)\s+\d"   # "down 3", "up 40 points"
    r"|\b(?:trading|closed|closing|opened|sitting|hovering)\s+at\s+\d"  # "trading at 42.10"
    r"|\bat\s+\d+\.\d+\b"                       # "is at 42.10" (decimal after 'at')
    r"|\bscored?\s+\d"                          # "scored 3"
    r"|\b\d+(?:\.\d+)?\s?(?:degrees|°[CF]?)\b",  # weather temps
    re.IGNORECASE,
)
_LIVE_CONTEXT_RE = re.compile(
    r"\b(?:price|prices|stock|shares?|ticker|trading|traded|market|markets|"
    r"close|closed|premarket|after[- ]hours|session|desk|portfolio|position|"
    r"pnl|p&l|nav|index|futures|weather|forecast|temperature|score|scored)\b"
    r"|\$[A-Z]{1,5}\b",
    re.IGNORECASE,
)


# Self-contextual live values — the unit alone marks them as a live-data claim
# ("it's 107°F out" carries no market/weather keyword but can only be a
# fabricated reading on a tool-less pass).
_SELF_EVIDENT_LIVE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:degrees|°[CF]?)\b"    # temperatures
    r"|\$\s?\d+(?:[,.]\d+)+\b",                   # precise dollar values ($194.83)
    re.IGNORECASE,
)


_BARE_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers_in(text: str) -> set[str]:
    """Normalized numeric literals in ``text`` (commas stripped) for
    grounded-vs-novel comparison."""
    return {m.group(0).replace(",", "") for m in _BARE_NUMBER_RE.finditer(text or "")}


def _ungrounded_live_claim(text: str, known_numbers: set[str] | None = None) -> bool:
    """True when ``text`` asserts a NOVEL number in a live-data context (price,
    move, temperature, score). Two-factor (number AND context) so pure-chat
    numbers ("I'm 100% sure", "give me 5 minutes") don't trip it; unit-marked
    values (temperatures, precise dollar amounts) count on their own.

    ``known_numbers``: numeric literals already present in the conversation
    context (history, memories, today-block). A claim whose numbers ALL appear
    there is a quote of something already said — repeating it ("that drop
    again? −2.21") is legitimate recall, not fabrication, so it passes."""
    if not text:
        return False
    suspicious = bool(_SELF_EVIDENT_LIVE_RE.search(text)
                      or (_LIVE_NUMBER_RE.search(text) and _LIVE_CONTEXT_RE.search(text)))
    if not suspicious:
        return False
    if known_numbers:
        novel = _numbers_in(text) - known_numbers
        if not novel:
            return False
    return True


def _route_model(user_text: str) -> str:
    """Heuristic: 'deep' (Qwen-32B) for reasoning/tool turns, else 'fast' (3B).
    Defaults to fast for speed; misroutes are caught by tool-escalation."""
    if not _routing_cfg.get("enabled", False) or llm_deep is llm_client:
        return "fast"
    t = user_text or ""
    tl = t.lower()
    if any(k in tl for k in _ROUTE_KEYWORDS):
        return "deep"
    if any(k in tl for k in _TOOL_INTENT_KEYWORDS):
        return "deep"
    if _TICKER_RE.search(t):
        return "deep"
    if _mentions_held_ticker(t):   # a ticker the desk actually holds → ground it
        return "deep"
    if len(t.split()) >= int(_routing_cfg.get("min_words", 40)):
        return "deep"
    return "fast"


# ── Output verifier (non-realtime only) ─────────────────────────────────────
# On surfaces where the user isn't waiting on live TTS (telegram/discord/cli,
# and the test harness), we can afford a Qwen pass that checks the drafted reply
# for leaked artifacts (<think>, raw JSON, stray tags) and claims unsupported by
# the tool results, then repairs it BEFORE it's sent. The webapp (typed + voice)
# stays fully live and skips this. See bridge/graph.py:verify_node.
_verifier_cfg = (llm_config.get("verifier") or {})
_REALTIME_SOURCES = set(_verifier_cfg.get(
    "realtime_sources", ["web", "ws_live", "voice", "voice-stream"]))


def _is_realtime_source(source: str) -> bool:
    """True for webapp/voice surfaces that stream live (verifier skipped)."""
    return (source or "") in _REALTIME_SOURCES


def _verifier_enabled() -> bool:
    return bool(_verifier_cfg.get("enabled", True)) and (llm_deep is not llm_client)


# ── Deterministic outgoing-text scrub (every surface, no LLM) ────────────────
# Last line of defense so internal artifacts never reach the user: leaked
# <think> blocks, stray inline tags, code fences, or a whole {"segments":[…]}
# object the model emitted instead of speaking. Cheap; safe to call on any text
# we're about to send or speak (conversational AND autonomous/idle).
_SEG_KEY_RE = re.compile(r"""["']segments["']\s*:""")
# Internal tags carry content that must NOT be spoken (the reasoning, the emotion
# id, the gesture name, the tool args). Strip the WHOLE block — tags + content —
# not just the markers (else "<emotion>happy</emotion>" would leak "happy").
# Unclosed (model ran out of tokens mid-tag) → strip to end.
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?(?:</think>|\Z)", re.IGNORECASE | re.DOTALL)
_TAG_BLOCK_RE = re.compile(
    r"<(reads|emotion|gesture|tool_call)\b[^>]*>.*?(?:</\1>|\Z)", re.IGNORECASE | re.DOTALL)
_STRAY_TAG_RE = re.compile(
    r"</?(?:think|reads|emotion|gesture|tool_call)\b[^>]*/?>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```$")
# `[past #]` is the stale-number redaction placeholder we inject into history;
# the model sometimes mimics it as "a number goes here" and emits it verbatim.
# Strip it (with optional surrounding backticks) so it never reaches the user.
_REDACT_LEAK_RE = re.compile(r"`?\[\s*past\s*#\s*\]`?", re.IGNORECASE)
# The model sometimes formats with markdown/HTML it learned from the prompt
# (e.g. `<strong>footwear</strong>`, `**bold**`) — wrong for spoken/streamed text.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^>]*>")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")


# Round over-precise numbers (raw opus floats like 1.1389465, 48.8988) to ≤2 dp.
# Only touches numbers with 3+ decimals; 0.32 / 1.14 / 513.32 pass through. A
# number never splits across a chunk (the chunker breaks on whitespace, and "."
# mid-number isn't a flush point), so this is safe per-chunk.
_LONG_DECIMAL_RE = re.compile(r"\d+\.\d{3,}")


def _round_decimals(text: str) -> str:
    def _r(m: "re.Match") -> str:
        try:
            return (f"{round(float(m.group(0)), 2):.2f}".rstrip("0").rstrip(".")) or "0"
        except Exception:
            return m.group(0)
    return _LONG_DECIMAL_RE.sub(_r, text or "")


def _clean_spoken(text: str) -> str:
    """Per-chunk scrub applied to STREAMED speech (the webapp shows these live, so
    finalize's full-text scrub is too late). Strips the [past #] placeholder,
    leaked HTML tags (<strong>…), markdown bold, and rounds over-precise numbers
    to ≤2 decimals. Deterministic, safe."""
    s = text or ""
    s = _REDACT_LEAK_RE.sub("", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _MD_BOLD_RE.sub(r"\1", s)
    s = _round_decimals(s)
    s = _clean_leaked_handles(s)   # scrub fabricated/leaked num:/vid: handles LIVE
    return re.sub(r"[ \t]{2,}", " ", s)


def _unwrap_segments_json(s: str):
    """If ``s`` is a {"segments":[…]} object (the silence-signal shape the model
    sometimes uses to *speak*), return the joined segment strings; else None."""
    if not _SEG_KEY_RE.search(s):
        return None
    obj = _first_json_object(s)
    if not obj:
        return None
    try:
        data = json.loads(obj)
    except Exception:
        return None
    segs = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segs, list):
        return None
    out = []
    for it in segs:
        if isinstance(it, str) and it.strip():
            out.append(it.strip())
        elif isinstance(it, dict) and (it.get("text") or "").strip():
            out.append(it["text"].strip())
    return " ".join(out) if out else None


def _sanitize_outgoing(text: str) -> str:
    """Strip leaked artifacts from text about to be sent/spoken. Deterministic,
    no LLM — safe to call on every surface."""
    s = (text or "").strip()
    if not s:
        return s
    unwrapped = _unwrap_segments_json(s)
    if unwrapped is not None:
        s = unwrapped
    s = _THINK_BLOCK_RE.sub("", s)   # drop reasoning blocks (incl. unclosed)
    s = _TAG_BLOCK_RE.sub("", s)     # drop reads/emotion/gesture/tool_call blocks + content
    s = _STRAY_TAG_RE.sub("", s)     # drop any remaining lone tag markers
    s = _REDACT_LEAK_RE.sub("", s)   # drop a mimicked [past #] redaction placeholder
    s = _HTML_TAG_RE.sub("", s)      # drop leaked HTML (<strong>/<em>/…) — wrong for TTS
    s = _MD_BOLD_RE.sub(r"\1", s)    # **bold** → bold
    s = _round_decimals(s)          # 1.1389465 → 1.14 (≤2 decimals everywhere)
    s = _clean_leaked_handles(s)    # scrub fabricated/leaked num:/vid: handles
    s = _FENCE_RE.sub("", s).strip()
    return re.sub(r"[ \t]{2,}", " ", s).strip()


# ── Leaked-handle cleanup ───────────────────────────────────────────────────
# A real handle is `kind:` + exactly 8 alphanumerics and is resolved before TTS.
# But the model sometimes FABRICATES handle syntax around a raw value it saw —
# e.g. `num:513.3236726242992` (the `.` breaks the 8-char match, so it never
# resolves and leaks verbatim). And a valid handle can fail to resolve (registry
# expiry). Both must be scrubbed before the user sees them.
_LEAKED_NUM_RE = re.compile(r"\bnum:\s*(-?\d[\d,]*\.?\d*)")
# Fabricated PLACEHOLDER handles the model templates when it has no real value —
# e.g. `num: [recent price]`, `num:<id>`, `url: [link]`. They never resolve and
# leak verbatim (the space/brackets dodge the value/handle regexes below).
_LEAKED_PLACEHOLDER_RE = re.compile(
    r"\b(?:vid|img|url|num):\s*(?:\[[^\]]{0,60}\]|<[^>]{0,60}>)", re.IGNORECASE)
_LEAKED_HANDLE_RE = re.compile(r"\b(?:vid|img|url|num):[A-Za-z0-9._-]{2,40}")
# Catch-all for a dangling handle PREFIX with no valid 8-char id after it (e.g.
# left behind when the HTML scrub already stripped `<id>` from `num:<id>`).
_LEAKED_BARE_RE = re.compile(r"\b(?:vid|img|url|num):(?![A-Za-z0-9]{8}\b)", re.IGNORECASE)


def _fmt_leaked_num(raw: str) -> str:
    """Render a fabricated num: value cleanly (round long floats to 2 dp)."""
    v = raw.replace(",", "")
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.2f}"
    except Exception:
        return raw


def _clean_leaked_handles(text: str) -> str:
    """Resolve any straggler valid handles, turn fabricated `num:<value>` into a
    plain number, and drop any remaining unresolved `kind:id` leak. Deterministic;
    safe on every surface."""
    if not text:
        return text
    text, _ = substitute_handles_in_text(text)          # resolve real 8-char handles
    text = _LEAKED_PLACEHOLDER_RE.sub("", text)         # drop `num: [recent price]` / `<id>`
    text = _LEAKED_NUM_RE.sub(lambda m: _fmt_leaked_num(m.group(1)), text)  # num:513.32 → 513.32
    text = _LEAKED_HANDLE_RE.sub("", text)              # drop fabricated/expired leftovers
    text = _LEAKED_BARE_RE.sub("", text)                # drop a dangling `num:`/`url:` prefix
    text = re.sub(r"^\s*\[(?!past)\s*", "", text)       # strip a leaked leading "[" artifact
    text = re.sub(r"\s+([,.;!?])", r"\1", text)         # tidy space-before-punct from removals
    return re.sub(r"\s{2,}", " ", text).strip()


# Spoken when a (realtime) turn think-truncates to nothing — better than silence.
# The non-realtime path is already rescued in verify_node; this is the webapp net.
_EMPTY_FALLBACKS = (
    "Hm — that one tangled up on me. Mind saying it again?",
    "Sorry, I lost the thread there for a second. What was that?",
    "That came out as nothing on my end — give me another go?",
)


def _empty_fallback(seed: int) -> str:
    return _EMPTY_FALLBACKS[abs(int(seed)) % len(_EMPTY_FALLBACKS)]


# Spoken cover when the DEEP model (Qwen-32B, shared with opus) is unreachable
# or errored — the turn produced nothing, so speak this instead of leaving the
# user in silence. Honest and low-key; the backend may just be restarting.
_BACKEND_DOWN_LINES = (
    "Hmm, I can't reach my deep-thinking side right now — give me a moment and ask again.",
    "My deeper reasoning's offline for a second — mind trying that again shortly?",
    "I can't get to the heavy-lifting model right this second. One moment, then try again.",
)


def _backend_down_line(seed: int) -> str:
    return _BACKEND_DOWN_LINES[abs(int(seed)) % len(_BACKEND_DOWN_LINES)]


# Spoken cover while the fast model hands a hard turn to the deep model.
_ESCALATE_STALLS = (
    "Hm — good question. Let me actually think about that.",
    "Okay, that one deserves a real answer. One sec.",
    "Let me think that through properly.",
)


def _escalate_stall_line(seed: int) -> str:
    return _ESCALATE_STALLS[abs(int(seed)) % len(_ESCALATE_STALLS)]


# The tool-running stall filler is now DERIVED from the structured action in
# bridge/graph.py (_stall_phrase) rather than free-generated by the 3B — the old
# LLM prompt's stock-only few-shots made it say "getting Tesla's price" while
# finding a nocturne. No system prompt needed here anymore.


BRIDGE_INTERNAL_URL = f"http://{INTERNAL_HOST}:{config['port']}"


def _live_mode_enabled() -> bool:
    """Treat missing / null as on; only explicit false disables /ws/live."""
    v = live_config.get("enabled", True)
    if v is None:
        return True
    return bool(v)

# ---------------------------------------------------------------------------
#  Echo / feedback handling (room vs headphone mode)
# ---------------------------------------------------------------------------
LIVE_ECHO_MODE = str(live_config.get("echo_mode", "room") or "room").lower()
_mute_override = live_config.get("mute_mic_while_agent_talking", None)
if _mute_override is None:
    # room => mute mic while TTS is playing (prevents acoustic loopback)
    # headphone => don't mute (lets you barge-in over assistant)
    MUTE_MIC_WHILE_AGENT_TALKING = (LIVE_ECHO_MODE == "room")
else:
    MUTE_MIC_WHILE_AGENT_TALKING = bool(_mute_override)

# ---------------------------------------------------------------------------
#  Animation mode
# ---------------------------------------------------------------------------
ANIMATION_MODE: str = anim_config.get("mode", "vector_db")

_ANIMATION_CLIPS: list[dict] = []
_ANIMATION_CLIP_NAMES: set[str] = set()

# FBX function name set + idle function names (loaded from CSV)
_FBX_FUNCTION_NAMES: set[str] = set()
_FBX_IDLE_FUNCTIONS: list[str] = []

if ANIMATION_MODE == "llm_select":
    _all = parse_actions_file()
    _ANIMATION_CLIPS = [c for c in _all if not c.get("phase")]
    _ANIMATION_CLIP_NAMES = {c["clip"] for c in _ANIMATION_CLIPS}
    log.info(
        "Animation mode=llm_select: loaded %d base clips (%d total in file)",
        len(_ANIMATION_CLIPS), len(_all),
    )
elif ANIMATION_MODE == "fbx_functions":
    import csv as _csv_mod
    _csv_path = Path(__file__).resolve().parent.parent / "character" / "animation_functions.csv"
    if _csv_path.exists():
        with open(_csv_path, encoding="utf-8") as _f:
            for _row in _csv_mod.DictReader(_f):
                fn = _row.get("function", "").strip()
                if fn:
                    _FBX_FUNCTION_NAMES.add(fn)
                    if _row.get("category", "").strip() == "idle":
                        _FBX_IDLE_FUNCTIONS.append(fn)
        # deduplicate idle list (CSV may have duplicate function names for variant animations)
        _FBX_IDLE_FUNCTIONS = list(dict.fromkeys(_FBX_IDLE_FUNCTIONS))
        log.info(
            "Animation mode=fbx_functions: loaded %d function names (%d idle) from %s",
            len(_FBX_FUNCTION_NAMES), len(_FBX_IDLE_FUNCTIONS), _csv_path,
        )
    else:
        log.warning("Animation mode=fbx_functions but CSV not found: %s", _csv_path)

app = FastAPI(title="Mocha Bridge")
log.info("bridge.live enabled=%s (WS /ws/live)", _live_mode_enabled())
log.info("Network: internal=%s  bridge=%s", INTERNAL_HOST, BRIDGE_INTERNAL_URL)
log.info(
    "Service URLs: stt=%s  tts=%s  memory=mem0(in-process)  animation=fbx_functions(csv)  llm=%s",
    config["stt_url"], config["tts_url"], config["llm_url"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(_auth_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

http = httpx.AsyncClient(timeout=120.0)
_ws_clients: list[WebSocket] = []
monitor_clients: list[WebSocket] = []

# Video playback confirmation. The video_player tool opens a video on the web
# client and then awaits the client's report of whether it ACTUALLY played (or
# whether every fallback candidate was refused at play time — oEmbed 200 does not
# guarantee the IFrame will play). Keyed by a short play_id the tool generates.
# This is what lets "done" mean "played" instead of "sent". Fire-and-forget safe:
# a missing/timed-out waiter just falls through to an optimistic result.
_play_waiters: dict[str, "asyncio.Future"] = {}


def register_play_waiter(play_id: str) -> "asyncio.Future":
    """Create + register a Future the web client will resolve via a
    ``video_play_result`` ws message. Caller awaits it (with a timeout)."""
    fut = asyncio.get_running_loop().create_future()
    _play_waiters[play_id] = fut
    return fut


def _resolve_play_waiter(play_id: str, result: dict) -> None:
    fut = _play_waiters.pop(play_id, None)
    if fut is not None and not fut.done():
        fut.set_result(result)


def discard_play_waiter(play_id: str) -> None:
    """Drop a waiter the caller is done with (e.g. on timeout) so it can't leak."""
    _play_waiters.pop(play_id, None)

# Per-user conversation histories and chat logs keyed by user_id.
# Legacy global lists retained for non-web channels (Telegram/Discord/CLI).
_user_histories: dict[str, list[dict]] = defaultdict(list)
_user_chat_logs: dict[str, list[dict]] = defaultdict(list)
# Backwards-compat alias — points at the "anonymous" bucket used when no
# authenticated user_id is available (e.g. old Telegram/Discord/CLI flows).
_ANON_USER_ID = "anonymous"
conversation_history = _user_histories[_ANON_USER_ID]
_chat_log = _user_chat_logs[_ANON_USER_ID]

# Single-operator identity. Every real surface (web / voice / telegram / cli)
# funnels into ONE memory + history + prompt namespace — Ika — so she learns one
# coherent picture of him over time and a Telegram reply lands in the same place
# the news was stored. Eval/test turns stay isolated via EVAL_ISOLATION. The value
# is sourced from config memory.mem0_user_id so the mem0 default agrees with it.
IKA_USER_ID = full_config.get("memory", {}).get("mem0_user_id", "ika")
# The autonomy composer reads this module-global thread directly. Point it at
# Ika's bucket — the same one _get_user_history/_append_history funnel into — so
# proactive turns see the live conversation, not the empty legacy "anonymous" one.
conversation_history = _user_histories[IKA_USER_ID]

MAX_HISTORY = full_config["memory"].get("short_term_limit", 20)

# Rolling condensed summary of everything trimmed out of the short-term window
# (per bucket). Injected as an "[Earlier this conversation]" system message so
# mid-length arcs survive both the window and bridge restarts. Persisted with
# the history itself (bridge/history_store.py).
_user_summaries: dict[str, str] = {}
_history_loaded: set[str] = set()


def _ensure_history_loaded(uid: str) -> None:
    """Lazy disk→RAM hydration, once per bucket. Mutates the existing list in
    place — module aliases (``conversation_history``) must keep pointing at it."""
    if uid in _history_loaded:
        return
    _history_loaded.add(uid)
    from bridge import history_store
    entries, summary = history_store.load_sync(uid)
    if entries:
        hist = _user_histories[uid]
        if not hist:
            hist.extend(entries[-MAX_HISTORY * 2:])
        log.info("history: restored %d entries for %s from disk", len(hist), uid)
    if summary:
        _user_summaries[uid] = summary


def _get_user_history(user_id: str | None) -> list[dict]:
    # Single operator: all real surfaces share Ika's one short-term history, so
    # the prompt and the autonomy composer read the same thread. Eval stays split.
    uid = (user_id or _ANON_USER_ID) if EVAL_ISOLATION.get() else IKA_USER_ID
    if not EVAL_ISOLATION.get():
        _ensure_history_loaded(uid)
    return _user_histories[uid]


def _get_user_summary(user_id: str | None) -> str:
    if EVAL_ISOLATION.get():
        return ""
    _ensure_history_loaded(IKA_USER_ID)
    return _user_summaries.get(IKA_USER_ID, "")


def _append_history(user_id: str | None, role: str, content: str) -> None:
    if EVAL_ISOLATION.get():
        uid = user_id or _ANON_USER_ID
        hist = _user_histories[uid]
        hist.append({"role": role, "content": content})
        if len(hist) > MAX_HISTORY * 3:
            del hist[: len(hist) - MAX_HISTORY * 2]
        return

    uid = IKA_USER_ID
    _ensure_history_loaded(uid)
    hist = _user_histories[uid]
    hist.append({"role": role, "content": content})
    # Bound the list; hand what falls out to the rolling summarizer instead of
    # silently forgetting it.
    if len(hist) > MAX_HISTORY * 3:
        dropped = hist[: len(hist) - MAX_HISTORY * 2]
        del hist[: len(hist) - MAX_HISTORY * 2]
        try:
            asyncio.get_running_loop().create_task(
                _summarize_history_span(uid, dropped))
        except RuntimeError:
            pass  # no loop (tests/CLI): the span is lost, same as before
    from bridge import history_store
    history_store.schedule_persist(uid, hist, _user_summaries.get(uid, ""))


async def _summarize_history_span(uid: str, dropped: list[dict]) -> None:
    """Fold trimmed-out turns into the bucket's rolling summary (fail-soft,
    off the turn path). Runs on the fast lane; logged as triggered_by='summary'."""
    if not dropped:
        return
    try:
        client = await active_fast_client()
        convo = "\n".join(
            f"{e.get('role', '?')}: {str(e.get('content', ''))[:400]}" for e in dropped)
        prev = _user_summaries.get(uid, "")
        sys_prompt = (
            "You maintain the condensed memory of an ongoing conversation between "
            "Ika (the user) and Mocha (the assistant). Merge PREVIOUS SUMMARY and "
            "NEW TURNS into ONE summary of at most 6 short sentences, third person "
            "('Ika asked…', 'Mocha found…'). Keep: concrete topics, decisions, "
            "promises, open threads, corrections, names. Drop: greetings, filler, "
            "anything already superseded. Never invent numbers — omit a number "
            "you're not sure of. Output ONLY the summary text."
        )
        user_block = f"PREVIOUS SUMMARY:\n{prev or '(none)'}\n\nNEW TURNS:\n{convo}"
        t0 = time.monotonic()
        res = await client.chat(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": user_block}],
            temperature=0.3, max_tokens=260, enable_thinking=False,
        )
        summary = (res.get("content") or "").strip()[:1800]
        if summary:
            _user_summaries[uid] = summary
            from bridge import history_store
            history_store.schedule_persist(uid, _user_histories[uid], summary)
        try:
            _sctx = CallContext(triggered_by="summary", user_id=uid,
                                conversation_id=str(_new_job_id()))
            asyncio.create_task(call_log.log_call(
                _sctx, model=client.model, temperature=0.3, max_tokens=260,
                stream=False, enable_thinking=False, tools_provided=False,
                messages=[{"role": "system", "content": "[history summary]"},
                          {"role": "user", "content": user_block}],
                response_content=summary or None,
                latency_ms=(time.monotonic() - t0) * 1000,
                prompt_tokens=(res.get("usage") or {}).get("prompt_tokens"),
                completion_tokens=(res.get("usage") or {}).get("completion_tokens"),
                total_tokens=(res.get("usage") or {}).get("total_tokens"),
            ))
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — memory upkeep must never break anything
        log.warning("history summary failed for %s: %s", uid, exc)


def _get_user_chat_log(user_id: str | None) -> list[dict]:
    return _user_chat_logs[user_id or _ANON_USER_ID]


def _extract_user_id(request: Request) -> str | None:
    """Extract user_id from Authorization: Bearer header or ?token= query param."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            return jwt_decode(auth.removeprefix("Bearer ").strip()).user_id
        except Exception:
            pass
    token = request.query_params.get("token", "")
    if token:
        try:
            return jwt_decode(token).user_id
        except Exception:
            pass
    return None


def _extract_user_id_ws(ws: WebSocket) -> str | None:
    """Extract user_id from WebSocket ?token= query param."""
    token = ws.query_params.get("token", "")
    if token:
        try:
            return jwt_decode(token).user_id
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
#  Single-active-Mocha presence
#
#  Multiple browser tabs/devices for the same user_id can hold WS connections
#  at once, but only ONE "owns" Mocha at a time. The active session gets the
#  3D model on screen + audio + mic; inactive sessions show panels normally
#  but pacify the avatar (frontend translates it off-screen + mutes).
#
#  Activity (typing, voice, explicit claim_active message) reassigns active.
#  Newly-connected sessions auto-claim only if no other session is active.
# ---------------------------------------------------------------------------

# user_id -> currently-active session_id
_active_session: dict[str, str] = {}
# (user_id, session_id) -> WebSocket — for fan-out of presence events
_user_sockets: dict[tuple[str, str], "WebSocket"] = {}


async def _send_presence(user_id: str, session_id: str, *, active: bool) -> None:
    """Send presence_active / presence_inactive to one specific socket."""
    sock = _user_sockets.get((user_id, session_id))
    if sock is None:
        log.info("presence: send skipped (no socket) uid=%s sid=%s active=%s",
                 user_id[:8], session_id[:8], active)
        return
    try:
        await sock.send_text(json.dumps({
            "type": "presence_active" if active else "presence_inactive",
        }))
        log.info("presence: SENT %s to uid=%s sid=%s",
                 "active" if active else "inactive",
                 user_id[:8], session_id[:8])
    except Exception as exc:
        # Socket dead; let the WS handler's finally clause clean it up.
        log.info("presence: send FAILED uid=%s sid=%s err=%s",
                 user_id[:8], session_id[:8], exc)


async def _claim_active(user_id: str | None, session_id: str | None) -> None:
    """Mark (user_id, session_id) as the active Mocha session for this user.

    If a different session was active, it gets a presence_inactive nudge.
    No-ops on missing user_id / session_id (legacy clients without the
    welcome-first frontend just skip the presence dance entirely).
    """
    if not user_id or not session_id:
        log.info("presence: claim skipped (uid=%s sid=%s)", user_id, session_id)
        return
    prev = _active_session.get(user_id)
    if prev == session_id:
        return
    _active_session[user_id] = session_id
    log.info("presence: claim_active uid=%s sid=%s prev=%s sockets_for_user=%d",
             user_id[:8], session_id[:8], (prev[:8] if prev else None),
             sum(1 for (u, _) in _user_sockets if u == user_id))
    if prev:
        await _send_presence(user_id, prev, active=False)
    await _send_presence(user_id, session_id, active=True)


def _register_session(user_id: str | None, session_id: str | None, ws: "WebSocket") -> bool:
    """Add a (user_id, session_id) socket to the registry.

    Returns True iff this is the only session for the user — caller should
    auto-claim it active in that case. False means another session is already
    active and this one starts inactive (passive observer).
    """
    if not user_id or not session_id:
        return False
    _user_sockets[(user_id, session_id)] = ws
    return user_id not in _active_session


def _unregister_session(user_id: str | None, session_id: str | None) -> None:
    if not user_id or not session_id:
        return
    _user_sockets.pop((user_id, session_id), None)
    if _active_session.get(user_id) == session_id:
        del _active_session[user_id]


async def _account_type_for(user_id: str | None) -> str:
    """Look up account_type for a user_id. Returns 'registered' for missing /
    unknown users (legacy non-web channels are unlimited)."""
    if not user_id:
        return "registered"
    try:
        row = await asyncio.to_thread(_auth_get_user_by_id, user_id)
    except Exception:
        return "registered"
    return (row or {}).get("account_type") or "registered"

# (Pass 1 / Pass 2 complexity-routing config removed in feature/single-pass-llama70b —
#  all calls now run full-context, single-pass.)


# When True (set per-task by /admin/eval), suppresses broadcasts and memory
# writes so the eval pipeline has no visible side effects outside its own
# response payload. Propagates through await via ContextVar.
EVAL_ISOLATION: ContextVar[bool] = ContextVar("eval_isolation", default=False)

# ---------------------------------------------------------------------------
#  Barge-in: cancellation for in-flight generation
# ---------------------------------------------------------------------------
_active_generation: Optional[asyncio.Task] = None
_generation_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
#  Pipeline timing (for debug overlay)
# ---------------------------------------------------------------------------
_pipeline_state: dict = {
    "phase": "idle",
    "llm_ms": 0,
    "tts_ms": 0,
    "anim_ms": 0,
    "last_input": "",
    "segment_count": 0,
}

# ---------------------------------------------------------------------------
#  Monitor: real-time thread status + job history for the dashboard
# ---------------------------------------------------------------------------
_thread_states: dict = {
    "uplink": {"status": "idle", "started_at": 0.0},
    "stt": {"status": "idle", "started_at": 0.0},
    "llm": {"status": "idle", "started_at": 0.0},
    "tts": {"status": "idle", "started_at": 0.0},
}

_job_counter: int = 0
_job_history: list[dict] = []
_MAX_JOB_HISTORY = 8

# Per-job absolute timeline tracking (job_id -> {start_mono, events: [...]})
_job_timelines: dict[int, dict] = {}


async def _broadcast_monitor(msg: dict):
    dead = []
    data = json.dumps(msg)
    for ws in monitor_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        monitor_clients.remove(ws)


async def _broadcast_chat_entry(entry: dict):
    """Push a completed chat-log entry to all connected monitor clients."""
    await _broadcast_monitor({"type": "chat_entry", "entry": entry})


async def _broadcast_agent_thought(source: str, kind: str, text: str,
                                    extra: dict | None = None) -> None:
    """Push a lightweight 'thinking stream' event — shown in the web UI's live
    thinking panel. Fire at LLM boundaries, tool calls, tool results, sub-agent
    replies, and stalls. Fail-silent: this is UX telemetry, never load-bearing.
    """
    try:
        msg: dict = {
            "type": "agent_thought",
            "source": source or "system",
            "kind": kind or "note",
            "text": (text or "").replace("\n", " ").strip()[:240],
            "at": time.time(),
        }
        if extra:
            msg.update(extra)
        await _broadcast_monitor(msg)
    except Exception:
        pass


async def _monitor_thread_start(thread: str, input_preview: str = "", job_id: int = 0,
                                  segment: int = 0, total_segments: int = 0):
    now = time.monotonic()
    _thread_states[thread]["status"] = "processing"
    _thread_states[thread]["started_at"] = now
    await _broadcast_monitor({
        "type": "thread_status",
        "thread": thread,
        "status": "processing",
        "input_preview": input_preview[:30],
        "job_id": job_id,
        "segment": segment,
        "total_segments": total_segments,
    })


async def _monitor_thread_end(thread: str, elapsed_ms: float = 0, input_preview: str = "",
                                job_id: int = 0, segment: int = 0, total_segments: int = 0):
    _thread_states[thread]["status"] = "idle"
    _thread_states[thread]["started_at"] = 0.0
    await _broadcast_monitor({
        "type": "thread_status",
        "thread": thread,
        "status": "idle",
        "elapsed_ms": round(elapsed_ms, 1),
        "input_preview": input_preview[:30],
        "job_id": job_id,
        "segment": segment,
        "total_segments": total_segments,
    })


def _new_job_id() -> int:
    global _job_counter
    _job_counter += 1
    return _job_counter


async def _monitor_job_phase(job_id: int, phase: str, action: str,
                               input_preview: str = "", segment: int = 0,
                               total_segments: int = 0, elapsed_ms: float = 0,
                               marginal_ms: float = 0):
    await _broadcast_monitor({
        "type": "job_event",
        "job_id": job_id,
        "phase": phase,
        "action": action,
        "input_preview": input_preview[:30],
        "segment": segment,
        "total_segments": total_segments,
        "elapsed_ms": round(elapsed_ms, 1),
        "marginal_ms": round(marginal_ms, 1),
        "timestamp": round(time.monotonic() * 1000),
    })

def _timeline_init(job_id: int):
    """Start a new absolute timeline for a job."""
    _job_timelines[job_id] = {"start_mono": time.monotonic(), "events": []}
    # Evict old timelines
    if len(_job_timelines) > _MAX_JOB_HISTORY * 2:
        oldest = sorted(_job_timelines.keys())[: len(_job_timelines) - _MAX_JOB_HISTORY]
        for k in oldest:
            _job_timelines.pop(k, None)


def _timeline_event(job_id: int, label: str, action: str, segment: int = -1, total: int = 0):
    """Record an event with absolute offset from job start (ms)."""
    tl = _job_timelines.get(job_id)
    if not tl:
        return None
    offset_ms = round((time.monotonic() - tl["start_mono"]) * 1000, 1)
    evt = {"label": label, "action": action, "offset_ms": offset_ms,
           "segment": segment, "total": total}
    tl["events"].append(evt)
    return evt


async def _broadcast_timeline_event(job_id: int, label: str, action: str,
                                      segment: int = -1, total: int = 0):
    """Record + broadcast a timeline event to monitor clients."""
    evt = _timeline_event(job_id, label, action, segment, total)
    if evt:
        await _broadcast_monitor({
            "type": "timeline_event",
            "job_id": job_id,
            **evt,
        })




# ---------------------------------------------------------------------------
#  Idle heartbeat
# ---------------------------------------------------------------------------
idle_config = full_config.get("idle", {})
IDLE_ENABLED = idle_config.get("enabled", True)
IDLE_INITIAL_DELAY = idle_config.get("initial_delay", 20)
IDLE_INTERVAL = idle_config.get("interval", 15)
IDLE_MAX_DURATION = idle_config.get("max_idle_duration", 300)

_last_interaction_time: float = time.monotonic()

# Autonomy state — shared with autonomy/engine.py and the self-mute tool.
_mocha_state: dict = {
    "mood": "curious",
    "last_topic_summary": "",
    "last_autonomous_spoke_at": 0.0,
    "autonomous_turns_today": 0,
    "muted_until_monotonic": 0.0,
    "last_hello_at": 0.0,
    # Updated on every tool execution so autonomy can back off mid-task.
    "last_tool_at_monotonic": 0.0,
}

IDLE_BEHAVIORS: list[dict] = [
    {"emotion": "neutral",  "action": "stretch arms above head and yawn"},
    {"emotion": "neutral",  "action": "look left and right slowly, scanning around"},
    {"emotion": "playful",  "action": "inspect fingernails with mild curiosity"},
    {"emotion": "neutral",  "action": "shift weight from one foot to the other"},
    {"emotion": "thinking", "action": "look up at the sky and tap chin idly"},
    {"emotion": "neutral",  "action": "cross arms and lean back casually"},
    {"emotion": "playful",  "action": "rock on heels, slightly bored"},
    {"emotion": "neutral",  "action": "tilt head and glance to the side"},
    {"emotion": "neutral",  "action": "gentle breathing, small shoulder rise and fall"},
    {"emotion": "playful",  "action": "hum silently and sway side to side a little"},
    {"emotion": "neutral",  "action": "rub back of neck and look down briefly"},
    {"emotion": "thinking", "action": "look around slowly as if noticing something"},
    {"emotion": "neutral",  "action": "fidget with hands behind back"},
    {"emotion": "playful",  "action": "bounce lightly on toes, restless energy"},
    {"emotion": "neutral",  "action": "roll shoulders and settle posture"},
]

_idle_last_index: int = -1


def _pick_idle_behavior() -> dict:
    global _idle_last_index
    candidates = list(range(len(IDLE_BEHAVIORS)))
    if _idle_last_index in candidates and len(candidates) > 1:
        candidates.remove(_idle_last_index)
    idx = random.choice(candidates)
    _idle_last_index = idx
    return IDLE_BEHAVIORS[idx]


def _touch_interaction(user_text: str | None = None):
    global _last_interaction_time, _last_user_interaction, _messages_since_warm
    _last_interaction_time = time.monotonic()
    _last_user_interaction = time.monotonic()
    _messages_since_warm += 1
    # Flip the presence machine to CONVERSING so the idle autonomy loop holds its
    # tongue while Ika is talking (it returns to IDLE after autonomy.idle_after_s).
    try:
        from autonomy import presence
        presence.note_user_activity()
    except Exception:
        pass
    # Record tail of the user message so autonomy can riff on the last topic.
    if user_text:
        topic = user_text.strip()
        if topic:
            _mocha_state["last_topic_summary"] = topic[-120:]


_diary_last_finalized_day: str = ""


async def _idle_heartbeat_loop():
    if not IDLE_ENABLED:
        return
    while True:
        await asyncio.sleep(5)

        # Autonomy engine gets a tick even when no web is connected — it may
        # still deliver via Telegram or queue. Fail-silent so a bad tick can't
        # kill the heartbeat.
        try:
            from autonomy.engine import decide_tick
            await decide_tick()
        except Exception as exc:
            log.warning("autonomy tick failed: %s", exc)

        # Task runtime: drop tasks left parked (awaiting an answer) past the TTL,
        # so a stale goal can't resurface when Ika comes back much later. Runs
        # off-turn (here, not in a turn) so it never drops a task he's resuming.
        # Cheap (tiny in-RAM dict); fail-silent.
        if TASK_RUNTIME_ENABLED:
            try:
                from bridge import task_store
                swept = task_store.sweep_timeouts(TASK_PARK_TTL_S)
                if swept:
                    log.info("task runtime: swept %d stale parked task(s)", swept)
            except Exception as exc:
                log.warning("task sweep failed: %s", exc)

        # Diary heartbeat — writes today's draft page when the user has been
        # idle long enough. Also handles local-tz midnight rollover by
        # finalising yesterday's draft once per day. Fail-silent.
        try:
            from bridge import diary_writer
            from memory import diary_store
            await diary_writer.tick(
                now_monotonic=time.monotonic(),
                last_interaction_monotonic=_last_interaction_time,
            )
            # Midnight rollover check — cheap: string compare.
            try:
                from bridge.channel_router import load_primary_user
                tz_name = (load_primary_user() or {}).get("timezone")
            except Exception:
                tz_name = None
            today_key = diary_store.today_key(tz_name)
            global _diary_last_finalized_day
            if _diary_last_finalized_day and _diary_last_finalized_day != today_key:
                # The day changed while we were running — finalise the day
                # that just ended (i.e. the previous key we had).
                prior = _diary_last_finalized_day
                _diary_last_finalized_day = today_key
                try:
                    await diary_store.finalize_page(prior)
                    from bridge import session_scratchpad
                    session_scratchpad.clear()
                    log.info("diary: day rolled %s → %s; finalised prior page",
                             prior, today_key)
                except Exception as exc:
                    log.warning("diary: finalize rollover failed: %s", exc)
            elif not _diary_last_finalized_day:
                _diary_last_finalized_day = today_key
        except Exception as exc:
            log.warning("diary tick failed: %s", exc)

        if not _ws_clients:
            continue
        elapsed = time.monotonic() - _last_interaction_time
        if elapsed < IDLE_INITIAL_DELAY:
            continue
        if IDLE_MAX_DURATION > 0 and elapsed > IDLE_MAX_DURATION:
            continue

        if ANIMATION_MODE == "fbx_functions" and _FBX_IDLE_FUNCTIONS:
            fn = random.choice(_FBX_IDLE_FUNCTIONS)
            msg = {
                "type": "idle_action",
                "emotion": "neutral",
                "action": fn,
                "gesture": fn,
            }
            log.info("Idle heartbeat (fbx_functions): %s", fn)
        else:
            behavior = _pick_idle_behavior()
            clip_name = await _resolve_action(behavior["action"], behavior["emotion"])
            msg = {
                "type": "idle_action",
                "emotion": behavior["emotion"],
                "action": behavior["action"],
                "gesture": clip_name,
            }
            log.info("Idle heartbeat: [%s] %s -> %s", behavior["emotion"], behavior["action"], clip_name)
        await _broadcast_clients(msg)

        jitter = IDLE_INTERVAL * 0.3
        wait = IDLE_INTERVAL + random.uniform(-jitter, jitter)
        await asyncio.sleep(max(wait, 5))


async def _handle_autonomy_hello(user_id: str | None = None):
    """Invoke the autonomy engine's reconnect-hello composer (fail-silent).

    For brand-new anon users (account_type='anon' AND display_name IS NULL)
    we flag `is_new_user=True` so autonomy fires a 'first_hello' utterance
    that bypasses the reconnect debounce and asks the user's name.
    """
    # Identity is fixed to Ika — there is no "brand-new, unnamed user" to greet
    # with a name question. Always treat a hello as a returning-user reconnect.
    try:
        from autonomy.engine import handle_client_hello
        await handle_client_hello(user_id=user_id, is_new_user=False)
    except Exception as exc:
        log.warning("autonomy hello failed: %s", exc)


# ---------------------------------------------------------------------------
#  Silent thinking gesture — broadcast during tool execution
# ---------------------------------------------------------------------------

def _fallback_action(user_text: str, emotion: str) -> str:
    t = (user_text or "").lower()

    if any(k in t for k in ("wave", "waving")):
        return "wave hand cheerfully"
    if any(k in t for k in ("jump", "hop", "bounce")):
        return "jump up and down"
    if any(k in t for k in ("dance", "music")):
        return "dance energetically"
    if any(k in t for k in ("sit", "seiza", "kneel")):
        return "sit down casually"
    if any(k in t for k in ("walk", "move around", "come closer", "step")):
        return "walk forward casually"
    if any(k in t for k in ("spin", "twirl", "turn around", "cartwheel")):
        return "spin around playfully"
    if any(k in t for k in ("stretch", "yawn")):
        return "stretch arms above head"

    e = (emotion or "neutral").lower()
    if e in ("thinking",):
        return "look up and tap chin thoughtfully"
    if e in ("excited",):
        return "cheer with a little bounce"
    if e in ("happy",):
        return "smile and wave slightly"
    if e in ("playful",):
        return "tilt head and lean forward playfully"
    if e in ("surprised",):
        return "step back briefly in surprise"
    if e in ("empathetic", "sad"):
        return "nod gently with a soft posture"

    return "shift weight and glance around casually"


def _normalize_segment(seg: dict) -> dict:
    seg.setdefault("emotion", "neutral")
    action = seg.pop("action", None) or seg.pop("gesture", None)
    seg["action"] = action
    seg.pop("gesture", None)
    return seg


def _current_time_message() -> dict:
    """Per-call system message giving Mocha the current local time/date.

    Sent right after the static system prompt (which is prefix-cached) so
    only this small message changes per turn. ~40 tokens. Without this the
    model hallucinates time-of-day claims like "you're up early".

    Renders in the user's timezone (from data/primary_user.json, captured
    from the browser on connect). Falls back to the server's local tz when
    no value has been learned yet.
    """
    from datetime import datetime
    try:
        from bridge.channel_router import load_primary_user
        primary = load_primary_user() or {}
    except Exception:
        primary = {}

    user_tz = primary.get("timezone")
    locale = primary.get("locale")
    loc_line = ""
    if primary.get("lat") is not None and primary.get("lng") is not None:
        loc_line = f" User approx location: {primary['lat']:.2f}, {primary['lng']:.2f}."

    tz_display = user_tz or "server-local"
    try:
        if user_tz:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(user_tz))
        else:
            now = datetime.now().astimezone()
    except Exception:
        now = datetime.now().astimezone()

    # Example: "2026-04-18 23:05 (11:05 PM, night) Saturday (PDT)"
    # The 12-hour + day-part reading is load-bearing for the FAST model: given
    # bare 24h "11:21" the 8B claimed "nearly midnight" at 11:21 AM — twice,
    # shipped (07-04; docs/audits/2026-07-05-llm-reasoning-audit.md). It was
    # parroting the prompt's own scripted example line, so the rules below must
    # stay descriptive — no quotable reply text.
    day_part = ("morning" if 5 <= now.hour < 12 else
                "afternoon" if 12 <= now.hour < 17 else
                "evening" if 17 <= now.hour < 21 else "night")
    stamp = now.strftime(f"%Y-%m-%d %H:%M (%-I:%M %p, {day_part}) %A (%Z)")
    return {
        "role": "system",
        "content": (
            f"Current local time: {stamp}. Timezone: {tz_display}."
            + (f" Locale: {locale}." if locale else "")
            + (loc_line)
            + "\nAlways read time-of-day, day-of-week, "
            f"and recency from this line — never guess. It is {day_part} right "
            f"now; any statement you make about the time of day must agree with "
            f"the parenthesized 12-hour reading above.\n\n"
            f"You MAY greet back in kind to a phatic greeting (\"hi\", \"hihi\"). "
            f"But you MUST NOT make any TIME-OF-DAY claim that contradicts the "
            f"actual time above. Specifically:\n"
            f"- Do NOT say Ika is up early or up late, or call him an early bird "
            f"or night owl, unless the actual time supports it.\n"
            f"- If his greeting names a different time of day than the actual "
            f"one (a \"good morning\" at night, a \"good night\" in the "
            f"morning), reciprocate the spirit and gently note the actual "
            f"time-of-day in your own words — never assert a time of day other "
            f"than the one above."
        ),
    }


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(t) if p.strip()]
    return parts or [t]


async def _prepend_segment(seg: dict, gen):
    """Async generator that prepends an already-peeked segment back into a stream."""
    yield {"_stream": "segment", "segment": seg}
    async for item in gen:
        yield item


# ---------------------------------------------------------------------------
#  Unified routing — LLM-driven two-pass routing
# ---------------------------------------------------------------------------

_tools_cfg = full_config.get("tools", {})
TOOLS_ENABLED: bool = bool(_tools_cfg.get("enabled", False))
_TOOL_MAX_ROUNDS: int = int(_tools_cfg.get("max_rounds", 5))
_TOOL_TIMEOUT: int = int(_tools_cfg.get("timeout", 30))

# Task runtime (bridge/task_store.py + task_templates.py + graph task_step).
# OFF by default: when disabled the conversational graph behaves exactly as before
# (classify emits no task route, task_step is never entered). Flip on per
# docs/task_runtime_design.md after live validation. Requires TOOLS_ENABLED.
_task_runtime_cfg = full_config.get("task_runtime", {})
TASK_RUNTIME_ENABLED: bool = bool(_task_runtime_cfg.get("enabled", False)) and TOOLS_ENABLED
TASK_PARK_TTL_S: int = int(_task_runtime_cfg.get("park_ttl_seconds", 600))

if TOOLS_ENABLED:
    from tools.registry import reload_custom_tools as _boot_reload
    _boot_reload()  # load custom tools on startup
    from tools.registry import TOOL_SCHEMAS
    from tools.executor import execute_tool
else:
    TOOL_SCHEMAS = []


def _extract_tool_calls(msg: dict) -> list[dict] | None:
    """Extract tool calls from Ollama response message.

    Handles both:
      1. Native ``tool_calls`` field (well-behaved models)
      2. Tool-call JSON in ``content`` (llama3.3 quirk)

    Returns a normalised list of ``{"function": {"name": ..., "arguments": ...}}`` dicts,
    or ``None`` if no tool calls detected.
    """
    # 1. Native tool_calls
    tc = msg.get("tool_calls")
    if tc:
        return tc
    # 2. Content-field fallback
    content = (msg.get("content") or "").strip()
    if not content or content[0] not in ("{", "["):
        return None
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "name" in parsed:
            return [{"function": {"name": parsed["name"],
                                  "arguments": parsed.get("parameters") or parsed.get("arguments") or {}}}]
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "name" in parsed[0]:
            return [{"function": {"name": tc["name"],
                                  "arguments": tc.get("parameters") or tc.get("arguments") or {}}}
                    for tc in parsed]
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
#  Inline-tag turn driver
#
#  Replaces the old JSON-segment pipeline. The LLM emits plaintext sprinkled
#  with inline tags (<reads>, <emotion>, <gesture>, <tool_call>). Bridge
#  parses the stream in real time, synthesises TTS per sentence chunk, fires
#  reads/gesture/emotion events, and handles tool calls inline.
# ---------------------------------------------------------------------------

# Numeric-literal detector for observability. Matches: 42, 3.14, $39.75, -1.29%, 12:34.
# Deliberately liberal — generates false positives for years/IDs/times; those
# are informational warnings, not errors, and show up in grepped logs.
_NUMERIC_LITERAL_RE = re.compile(
    r"""
    (?<!\w)                     # no word char before (avoid mid-word false positives like 'mp3')
    -?\$?\d+(?:[.,]\d+)?%?      # optional -, $, digits, optional decimal, optional %
    (?!\w)                      # no word char after
    """,
    re.VERBOSE,
)


_NUM_HANDLE_RE = re.compile(r"num:[a-zA-Z0-9]{8}")


def _redact_stale_numbers(text: str) -> str:
    """Replace every numeric literal OUTSIDE of a ``num:xxxxxxxx`` handle with
    a `[past #]` placeholder. Applied to conversation-history entries before
    Pass 2 fires so Mocha cannot pattern-match on a stale number from an
    earlier turn (e.g. yesterday's stock price) when replying to a fresh
    data query. Handles themselves pass through untouched.
    """
    if not text:
        return text
    # Mask handles so the number regex doesn't touch the digits inside them.
    stash: list[str] = []

    def _save(m: "re.Match") -> str:
        stash.append(m.group(0))
        return f"\x00H{len(stash) - 1}\x00"

    masked = _NUM_HANDLE_RE.sub(_save, text)
    redacted = _NUMERIC_LITERAL_RE.sub("[past #]", masked)
    for i, h in enumerate(stash):
        redacted = redacted.replace(f"\x00H{i}\x00", h)
    return redacted


def _check_unmapped_numeric_literals(text: str, job_id: int, chunk_idx: int) -> None:
    """Layer-4 observability: warn if the resolved chunk still contains bare
    numeric literals. After ``substitute_handles_in_text`` has done its job,
    any remaining numbers were NOT sourced from a tool — they came from the
    LLM's general knowledge (which for current-events / prices is a
    hallucination). Logs only; never blocks playback.
    """
    matches = [m.group(0) for m in _NUMERIC_LITERAL_RE.finditer(text)]
    if matches:
        log.warning(
            "[inline-turn] job=%s chunk_idx=%d unmapped numeric literal(s) %s in chunk: %r",
            job_id, chunk_idx, matches, text[:120],
        )


def _first_json_object(s: str) -> str | None:
    """Return the first balanced ``{...}`` slice in ``s`` (handles a jammed
    second ``<tool_call>`` or trailing prose after the closing brace)."""
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return s[start:i + 1]
    return None


_KWARG_RE = re.compile(
    r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^\s,]+)'
)


def _coerce_scalar(raw: str):
    v = raw.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d*\.\d+", v):
        return float(v)
    return v


def _parse_tool_args_str(args_str: str) -> dict:
    """Adapt an inline ``<tool_call>`` body to a dict for the executor.

    Models — especially the fast 3B, but also Qwen under a terse signature hint —
    are inconsistent about argument format. We accept all of these and normalise:

    1. A JSON object literal (the canonical form), even with trailing junk after
       the closing brace (e.g. a second ``<tool_call>`` jammed on without a
       closing tag).
    2. ``kwargs`` / function-call style — ``topic="quantum computing" max_results=5``
       — which fails ``json.loads`` and previously collapsed to
       ``{"request": ...}``, silently dropping every named param (this is exactly
       why ``get_news`` always errored with "no topic provided").
    3. A bare string → ``{"request": body}`` (single-string-arg tool schema).
    """
    s = (args_str or "").strip()
    if not s:
        return {}

    # 1) JSON object literal (whole string, then first balanced slice).
    if "{" in s:
        for candidate in (s if s.startswith("{") else None, _first_json_object(s)):
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    # 2) kwargs / function-call style: key=value pairs.
    if "=" in s:
        kw = {m.group(1): _coerce_scalar(m.group(2)) for m in _KWARG_RE.finditer(s)}
        if kw:
            return kw

    # 3) Bare single string.
    return {"request": s}


def _build_inline_messages(user_text: str, memories: list[dict],
                           user_id: str | None = None,
                           tools_available: bool | None = None,
                           reply_context: str | None = None,
                           intent_read: dict | None = None,
                           source: str | None = None) -> list[dict]:
    """Build the messages list for an inline-tag LLM call.

    History and memory-fragment numbers are passed through INTACT so
    cross-turn deduction works ("say that drop again", "which of the two?").
    Stale-as-current is prevented downstream instead: prompt staleness rules,
    the escalate protocol on the tool-less fast path, and the
    ``_ungrounded_live_claim`` backstop (novel numbers only).

    ``tools_available`` lets the caller build a CHAT-ONLY prompt (the fast
    model, which has no tools and escalates instead). Defaults to
    ``TOOLS_ENABLED``.
    """
    if tools_available is None:
        tools_available = TOOLS_ENABLED
    if tools_available:
        system_prompt = build_system_prompt(
            animation_mode=ANIMATION_MODE,
            tools_available=True,
            user_id=user_id,
        )
    else:
        # Fast chat path: lean persona-first prompt (~4x smaller). Avatar
        # surfaces get the minimal tag block; text channels get plain text.
        system_prompt = build_chat_prompt(
            tagged=_is_realtime_source(source or ""),
            user_id=user_id,
        )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    # Rolling summary of turns that fell out of the short-term window. Placed
    # BEFORE the history slice: it changes rarely (only when the window trims),
    # so the prefix cache still covers system prompt + summary on most turns.
    _cond = _get_user_summary(user_id)
    if _cond:
        messages.append({
            "role": "system",
            "content": ("[Earlier this conversation — condensed. Background "
                        "context, not the current topic; numbers here may be "
                        "stale.] " + _cond),
        })
    # History numbers are passed through INTACT (2026-07-02; they used to be
    # redacted to `[past #]`). The redaction protected against quoting stale
    # prices as current, but it destroyed cross-turn deduction: "say that drop
    # again" produced sentences with holes, and "which of the two?" got a
    # vibes-only answer because the comparison numbers were gone. The staleness
    # hazard is now covered downstream: the tool-less fast path escalates live
    # questions, `_ungrounded_live_claim` blocks NOVEL numbers (history numbers
    # are in its known-set), and the verifier grounds deep drafts.
    for entry in _get_user_history(user_id)[-MAX_HISTORY:]:
        messages.append({
            "role": entry["role"],
            "content": entry["content"],
        })
    if memories:
        mem_lines = [
            f"- ({m['role']}): {m['text']}"
            for m in memories
        ]
        messages.append({
            "role": "system",
            "content": (
                "[Memory fragments — older snippets retrieved by similarity to the "
                "current message. These are NOT the current topic. Use them only for "
                "continuity (knowing Ika's preferences, history, running jokes). Do "
                "NOT bring up subjects from these fragments unless Ika references them "
                "first. Numbers in these fragments are as-of when they were said — "
                "likely stale; never present one as current.]:\n"
            ) + "\n".join(mem_lines),
        })
    # Today so far — diary draft + fresh tool-call scratchpad. Gives Mocha
    # within-day/within-session recall of what's been happening, so she can
    # answer "what did we do earlier?" or "play that song again" without
    # losing the thread at a turn boundary.
    _today_block = _build_today_so_far_block(user_id=user_id)
    if _today_block:
        messages.append({"role": "system", "content": _today_block})
    _modals_line = get_open_modals_summary()
    if _modals_line:
        messages.append({
            "role": "system",
            "content": f"[Currently on screen]: {_modals_line}. You can reference and close these directly.",
        })
    # Mood — a soft inner-state line from the desk's combined P&L, shared with the
    # autonomy composer so she feels the same whether chatting or drifting.
    try:
        from bridge import pnl_mood
        _mood_msg = pnl_mood.mood_system_message()
        if _mood_msg:
            messages.append({"role": "system", "content": _mood_msg})
    except Exception:
        pass
    # Reply context — when Ika replies to a specific message Mocha sent (e.g. a
    # news item), the resolved article is injected here as SOURCE DATA so the
    # verifier won't strip her references to it, and so she can pick it back up.
    if reply_context:
        messages.append({"role": "system", "content": reply_context})
    # Read intent — interpret_node's resolved understanding of what Ika is asking,
    # injected as a directive so the synthesis answers THAT (not a misread of a
    # terse/reply message) and fetches data instead of deflecting.
    if intent_read and (intent_read.get("asking") or intent_read.get("refers_to")
                        or intent_read.get("kg_fact")):
        bits = []
        if intent_read.get("asking"):
            bits.append(f"- He wants: {intent_read['asking']}")
        if intent_read.get("refers_to"):
            bits.append(f"- Refers to: {intent_read['refers_to']}")
        if intent_read.get("note"):
            bits.append(f"- Note: {intent_read['note']}")
        # Knowledge-graph fact — a CITED relationship from the desk's shared
        # graph. This is ground truth: prefer it over any guess, and it carries
        # an evidence id so it survives the anti-confabulation verifier.
        if intent_read.get("kg_fact"):
            bits.append(f"- {intent_read['kg_fact']}")
        elif intent_read.get("kg_gap"):
            pair = intent_read["kg_gap"]
            bits.append(
                f"- The link between {pair[0]} and {pair[1]} isn't in the desk's "
                "knowledge graph yet. Say you're not certain of the connection and "
                "you'll look into it — do NOT invent one."
            )
        messages.append({"role": "system", "content": (
            "[Read of what Ika is asking — already resolved against the conversation; "
            "answer THIS, don't re-interpret a terse line as a new topic:\n"
            + "\n".join(bits) +
            "\nIf it needs fresh data, call the right tool; do NOT deflect with "
            "'nothing stands out'.]"
        )})
    messages.append(_current_time_message())
    messages.append({"role": "user", "content": user_text})
    return messages


_TODAY_BLOCK_CAP = 1200  # chars — hard cap so we don't balloon the prompt


def _build_today_so_far_block(user_id: str | None = None) -> str:
    """Compose the "today so far" system message from the diary draft (if any)
    plus the recent tool-activity scratchpad. Returns empty string when there's
    nothing to say — callers should skip injection in that case."""
    pieces: list[str] = []
    # Today's diary draft (summary only — the activity list lives in the
    # scratchpad block below, which is more up-to-the-second anyway).
    try:
        from memory import diary_store
        from bridge.channel_router import load_primary_user
        tz_name = (load_primary_user() or {}).get("timezone")
        today = diary_store.today_key(tz_name)
        # Use the sync read so we don't need to be async here.
        page = diary_store._read_page_sync(today, user_id=user_id)
        if page and page.get("summary"):
            pieces.append(
                "Diary draft for today (your own notes; keep it consistent):\n"
                + (page.get("summary") or "")[:700]
            )
    except Exception:
        pass
    # Recent tool activity — live, in-RAM.
    try:
        from bridge import session_scratchpad
        sp_block = session_scratchpad.format_for_prompt(max_entries=8, user_id=user_id)
        if sp_block:
            pieces.append(sp_block)
    except Exception:
        pass
    if not pieces:
        return ""
    body = "\n\n".join(pieces)
    if len(body) > _TODAY_BLOCK_CAP:
        body = body[: _TODAY_BLOCK_CAP - 1] + "…"
    return (
        "[Today so far — your live in-session memory. Use to answer \"what did "
        "we do earlier?\", \"play that again\", \"what was the last price?\". "
        "Handles in here (vid:, num:, img:) are still valid — re-use them.]\n\n"
        + body
    )


async def _run_inline_turn(
    user_text: str,
    memories: list[dict],
    job_id: int,
    *,
    source: str = "web",
    log_ctx: CallContext | None = None,
    user_id: str | None = None,
    reply_context: str | None = None,
):
    """Drive one complete user turn. Yields wire-format events.

    Events:
        {"type": "thinking_delta", "content": str}
        {"type": "emotion",       "id":   str}
        {"type": "gesture",       "name": str}
        {"type": "speech_chunk",  "chunk_idx": int, "text": str,
                                  "audio_base64": str|None,
                                  "viseme_b64": str|None,
                                  "viseme_fps": int, "viseme_frames": int}
        {"type": "tool_status",   "action": "call"|"result", "round": int, ...}
        {"type": "speech_end",    "total_chunks": int, "full_text": str}
    """
    # Orchestration now runs as a LangGraph StateGraph (bridge/graph.py). This
    # wrapper only sets per-turn context, drives the compiled graph in a task,
    # and relays the graph's streamed UI events through an asyncio.Queue. The
    # event contract is byte-identical to the old hand-rolled loop, so all 5
    # callers are untouched.
    from bridge.graph import MOCHA_GRAPH, SENTINEL

    # Single-operator identity: every real surface resolves to Ika (eval stays
    # isolated), so memory/history/prompt all share one namespace.
    if not EVAL_ISOLATION.get():
        user_id = IKA_USER_ID

    # Mark a conversational turn in flight so the idle autonomy loop stays silent
    # for its whole duration (even a long tool round), not just on the clock.
    try:
        from autonomy import presence
        presence.turn_started()
    except Exception:
        presence = None

    base_ctx = log_ctx or CallContext(
        triggered_by="chat_stream", source=source, conversation_id=str(job_id),
    )

    # Expose user_id to tools (e.g. schedule_cron tags jobs with the caller).
    if TOOLS_ENABLED:
        from tools.executor import TOOL_USER_ID as _TOOL_USER_ID
        _TOOL_USER_ID.set(user_id)

    # Note the interaction for the diary writer (per-day count).
    try:
        from bridge import diary_writer
        from bridge.channel_router import load_primary_user
        diary_writer.note_interaction((load_primary_user() or {}).get("timezone"))
    except Exception:
        pass

    emit: asyncio.Queue = asyncio.Queue()
    init = {
        "user_text": user_text,
        "memories": memories,
        "job_id": job_id,
        "source": source,
        "realtime": _is_realtime_source(source),
        "user_id": user_id,
        "reply_context": reply_context,
        "base_ctx": base_ctx,
        "emit": emit,
    }

    async def _run_graph():
        # recursion_limit is comfortably above (3 nodes × max tool rounds).
        try:
            await MOCHA_GRAPH.ainvoke(init, config={"recursion_limit": 50})
        finally:
            # Always unblock the consumer, even if a node raised.
            await emit.put(SENTINEL)

    graph_task = asyncio.create_task(_run_graph())
    try:
        while True:
            ev = await emit.get()
            if ev is SENTINEL:
                break
            yield ev
        await graph_task  # surface any node exception
    finally:
        # Barge-in / caller cancellation: tear down the in-flight graph so the
        # active LLM/tool call is cancelled. Memory/history writes live in the
        # caller AFTER this generator completes, so a cancelled turn persists
        # nothing — identical to the previous behaviour.
        if not graph_task.done():
            graph_task.cancel()
            try:
                await graph_task
            except (asyncio.CancelledError, Exception):
                pass
        # Turn over — start the silence clock so presence can drift back to IDLE.
        if presence is not None:
            try:
                presence.turn_ended()
            except Exception:
                pass


class ChannelRequest(BaseModel):
    text: str
    user_id: str = "unknown"       # external channel ID (e.g. Telegram chat_id)
    source: str = "unknown"
    app_user_id: str | None = None  # project mocha user_id (for per-user memory)
    # Set when Ika replies to one of Mocha's messages (Telegram reply-to): the
    # message_id of the message being replied to, plus its quoted text. Used to
    # resolve the exact article Mocha shared (see autonomy/news_ledger.py).
    reply_to_message_id: int | None = None
    quoted_text: str | None = None


class TtsRequest(BaseModel):
    text: str


# ---------------------------------------------------------------------------
#  Debug state broadcasting
# ---------------------------------------------------------------------------

async def _broadcast_debug_state(phase: str, **extra):
    _pipeline_state["phase"] = phase
    _pipeline_state.update(extra)
    msg = {"type": "debug_state", "phase": phase}
    msg.update(extra)
    await _broadcast_clients(msg)


# ---------------------------------------------------------------------------
#  Service calls
# ---------------------------------------------------------------------------

# Novelty gate for retrieved memories (2026-08-02): a fact Mocha just leaned on
# is "spent" for a while — surfacing the same fragment turn after turn is what
# made her recite his own history back at him. Keyed per user, TTL-purged,
# reset on restart (fine: the annoyance is within-session repetition).
_SURFACED_MEM: dict[str, dict[str, float]] = defaultdict(dict)
_MEM_SURFACE_TTL_S = 6 * 3600.0
_MEM_SURFACE_MAX = 200


def _mem_novelty_key(m: dict) -> str:
    import hashlib
    return hashlib.sha1((m.get("text") or "").strip().lower().encode("utf-8")).hexdigest()[:16]


async def _query_memories(text: str, user_id: str | None = None) -> list[dict]:
    """Semantic memory search via in-process mem0 store. Real turns all read from
    the single Ika namespace; eval/test turns keep their own isolated partition.

    Applies the novelty gate: fragments surfaced within the TTL are dropped
    (back-filled only if that leaves fewer than 2), and whatever is returned is
    recorded as surfaced. Fetches extra headroom so filtering rarely starves."""
    if not EVAL_ISOLATION.get():
        user_id = IKA_USER_ID
    try:
        from memory import mem0_store
        hits = await mem0_store.search(text, user_id=user_id, k=10)
    except Exception as e:
        log.warning(f"Memory query failed: {e}")
        return []
    if not hits:
        return []
    seen = _SURFACED_MEM[user_id or IKA_USER_ID]
    now = time.monotonic()
    for k_, ts_ in list(seen.items()):
        if now - ts_ > _MEM_SURFACE_TTL_S:
            del seen[k_]
    fresh = [m for m in hits if _mem_novelty_key(m) not in seen]
    out = fresh[:5]
    if len(out) < 2:   # everything relevant was recently used — allow the best back in
        spent = [m for m in hits if m not in fresh]
        out = (out + spent)[:2]
    while len(seen) > _MEM_SURFACE_MAX:
        seen.pop(next(iter(seen)))
    for m in out:
        seen[_mem_novelty_key(m)] = now
    return out


async def _store_memory(text: str, role: str, source: str | None = None,
                        user_id: str | None = None):
    """Fire-and-forget mem0 add. Offloaded to a thread; returns immediately
    so the calling turn doesn't eat mem0's fact-extraction LLM latency."""
    if EVAL_ISOLATION.get():
        return
    user_id = IKA_USER_ID  # one namespace for the single operator
    try:
        from memory import mem0_store
        extra = {"channel": source} if source else None
        await mem0_store.add(text, role=role, user_id=user_id, metadata=extra)
    except Exception as e:
        log.warning(f"Memory store failed: {e}")


async def _resolve_action(action_text: str, emotion: str = "") -> str | None:
    """Validate an LLM-proposed animation action against the local CSV roster.

    FBX-function mode reads names straight from character/animation_functions.csv
    (loaded at startup). llm_select mode validates against the base-clip table.
    The old vector_db fallback (HTTP call to the animation service) was removed
    — if the LLM produces a name that isn't in either table we just return None
    so the client can fall back to its default idle loop.
    """
    if not action_text:
        return None

    if ANIMATION_MODE == "fbx_functions":
        if action_text in _FBX_FUNCTION_NAMES:
            log.info("Animation (fbx_functions): '%s' -- valid function", action_text)
            return action_text
        log.debug("Animation (fbx_functions): '%s' -- not a valid function name", action_text)
        return None

    if ANIMATION_MODE == "llm_select" and action_text in _ANIMATION_CLIP_NAMES:
        log.info("Animation (llm_select): '%s' -- direct match", action_text)
        return action_text

    log.debug("Animation: '%s' -- no match in mode=%s", action_text, ANIMATION_MODE)
    return None


async def _generate_visemes(audio_bytes: bytes | None, text: str = "") -> dict | None:
    """Call STT /align endpoint to get phoneme timestamps, then map to viseme weights.

    Returns dict with 'viseme_b64', 'viseme_fps', 'viseme_frames', or None.
    """
    if not audio_bytes or not text:
        return None
    stt_url = config.get("stt_url", "")
    if not stt_url:
        return None
    try:
        resp = await http.post(
            f"{stt_url}/align",
            files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            data={"text": text},
            timeout=10.0,
        )
        if resp.status_code != 200:
            log.debug("Viseme alignment returned %d", resp.status_code)
            return None
        data = resp.json()
        viseme_b64 = data.get("viseme_b64")
        if not viseme_b64:
            return None
        return {
            "viseme_b64": viseme_b64,
            "viseme_fps": data.get("viseme_fps", 30),
            "viseme_frames": data.get("viseme_frames", 0),
        }
    except Exception as e:
        log.debug("Viseme generation unavailable: %s", e)
        return None


def _sanitize_for_tts(text: str) -> str:
    """Calm down F5-TTS prosody peaks before synthesis.

    F5 reads the gen_text literally and was trained on real speech, so
    exclamation marks → big pitch + energy spike (often shouty) and ALL
    CAPS → louder + sharper attack. Both sound off in Mocha's voice.

    This mutates ONLY the text sent to TTS. The chat bubble + history are
    built upstream from the original text, so the visual `!` emphasis is
    preserved while the audio stays calm.
    """
    if not text:
        return text
    # Emoji never reach the synthesizer — F5 tries to *read* them ("face with
    # tears of joy"). Display text upstream keeps them; only the audio is scrubbed.
    text = re.sub(r'[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]', '', text)
    # Collapse `!` (any run, optionally followed by other punctuation) into
    # a soft period. "wow!" → "wow." / "really?!" → "really?."  / "!!!" → "."
    text = re.sub(r'!+', '.', text)
    # Treat 2+ consecutive caps as shouting → Title-case it. Skips
    # standalone single letters ("I", "A") and acronyms of length 1.
    text = re.sub(
        r'\b[A-Z]{2,}\b',
        lambda m: m.group(0).capitalize(),
        text,
    )
    return text


async def _synthesize(text: str, user_id: str | None = None) -> Optional[bytes]:
    payload: dict = {"text": _sanitize_for_tts(text)}
    voice_path = _user_active_voice_path(user_id)
    if voice_path:
        payload["ref_audio_path"] = voice_path
        # Send the ref text alongside so F5-TTS skips its in-process Whisper
        # step (which currently breaks under torchcodec lib mismatch). The
        # sidecar is created on first use and reused forever after.
        ref_text = await _voice_ref_text(voice_path)
        if ref_text:
            payload["ref_text"] = ref_text
    try:
        resp = await http.post(
            f"{config['tts_url']}/synthesize",
            json=payload,
        )
        if resp.status_code == 200:
            return resp.content
        log.warning("TTS HTTP %s: %s", resp.status_code, (resp.text or "")[:500])
    except Exception as e:
        log.warning(f"TTS failed: {e}")
    return None


async def _transcribe(audio_bytes: bytes, job_id: int = 0) -> str:
    await _monitor_thread_start("stt", input_preview="(audio)", job_id=job_id)
    t0 = time.monotonic()
    try:
        files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
        resp = await http.post(f"{config['stt_url']}/transcribe", files=files)
        ms = (time.monotonic() - t0) * 1000
        if resp.status_code == 200:
            text = resp.json().get("text", "")
            await _monitor_thread_end("stt", elapsed_ms=ms, input_preview=text, job_id=job_id)
            await _monitor_job_phase(job_id, "stt", "end", input_preview=text, elapsed_ms=ms)
            return text
    except Exception as e:
        log.warning(f"STT failed: {e}")
    ms = (time.monotonic() - t0) * 1000
    await _monitor_thread_end("stt", elapsed_ms=ms, job_id=job_id)
    return ""


# ---------------------------------------------------------------------------
#  Concurrent segment processing (TTS + animation in parallel)
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup():
    # NOTE: this decorator is load-bearing — without it nothing in the
    # boot sequence below actually runs (AgentLoop, heartbeat, channels,
    # Shiro). Lost briefly in a refactor and discovered because the cron
    # scheduler reported "not running".
    asyncio.create_task(_init_call_log())
    asyncio.create_task(_idle_heartbeat_loop())
    asyncio.create_task(_llm_warmup())
    asyncio.create_task(_cache_rewarm_loop())
    asyncio.create_task(_start_channels())
    asyncio.create_task(_start_agent_loop())
    # Warm the held-ticker cache so the very first turn is portfolio-aware.
    asyncio.create_task(_refresh_held_tickers())


_agent_loop_instance = None


def get_agent_loop():
    """Return the running AgentLoop singleton (or None if disabled/not started)."""
    return _agent_loop_instance


@app.on_event("shutdown")
async def _on_shutdown():
    if _agent_loop_instance:
        await _agent_loop_instance.stop()
    await call_log.close_pool()
    await llm_client.close()


async def _init_call_log():
    cfg = full_config.get("call_log", {})
    if not cfg.get("enabled"):
        log.info("Call log: disabled in config")
        return
    try:
        await call_log.init_pool(
            cfg["dsn"],
            min_size=cfg.get("pool_min", 1),
            max_size=cfg.get("pool_max", 5),
        )
        await call_log.ensure_schema()
        log.info("Call log: PostgreSQL pool ready")
    except Exception as exc:
        log.error("Call log: init failed (logging disabled): %s", exc)


async def _start_channels():
    """Boot enabled messaging channels (Telegram, Discord, CLI)."""
    await asyncio.sleep(1)
    channels_cfg = full_config.get("channels", {})
    bridge_url = BRIDGE_INTERNAL_URL

    from channels.base import registry

    tg_cfg = channels_cfg.get("telegram", {})
    if tg_cfg.get("enabled", True):
        try:
            from channels.telegram import TelegramChannel
            from auth.db import get_all_telegram_users
            # Single operator: pin the bot to Ika's known chat-id so a stranger
            # who finds the bot can't be mistaken for Ika or write to his memory.
            # Unknown (fresh install) → None = open, so Ika's first message can
            # register; a restart then pins it.
            from bridge.channel_router import load_primary_user
            _pri = load_primary_user() or {}
            _allowed = [str(_pri["telegram_user_id"])] if _pri.get("telegram_user_id") else None
            tg_users = get_all_telegram_users()
            for tgu in tg_users:
                ch = TelegramChannel(
                    bot_token=tgu["telegram_bot_token"],
                    bridge_url=bridge_url,
                    app_user_id=tgu["user_id"],
                    allowed_users=_allowed,
                )
                registry.register_user_bot(tgu["user_id"], ch)
            if tg_users:
                log.info("Loaded %d per-user Telegram bot(s)", len(tg_users))
        except Exception as exc:
            log.error("Failed to init per-user Telegram channels: %s", exc)

    dc_cfg = channels_cfg.get("discord", {})
    if dc_cfg.get("enabled") and dc_cfg.get("bot_token"):
        try:
            from channels.discord_channel import DiscordChannel
            ch = DiscordChannel(
                bot_token=dc_cfg["bot_token"],
                bridge_url=bridge_url,
                allowed_guilds=dc_cfg.get("allowed_guilds"),
                allowed_channels=dc_cfg.get("allowed_channels"),
            )
            registry.register(ch)
        except Exception as exc:
            log.error("Failed to init Discord channel: %s", exc)

    cli_cfg = channels_cfg.get("cli", {})
    if cli_cfg.get("enabled"):
        try:
            from channels.cli import CLIChannel
            ch = CLIChannel(bridge_url=bridge_url)
            registry.register(ch)
        except Exception as exc:
            log.error("Failed to init CLI channel: %s", exc)

    await registry.start_all()


async def _start_agent_loop():
    """Start the proactive agent scheduler if enabled."""
    global _agent_loop_instance
    await asyncio.sleep(3)
    agent_cfg = full_config.get("agent_loop", {})
    if not agent_cfg.get("enabled"):
        return
    try:
        from agent.loop import AgentLoop
        from channels.base import registry as ch_registry
        _agent_loop_instance = AgentLoop(agent_cfg, bridge_url=BRIDGE_INTERNAL_URL, channel_registry=ch_registry)
        await _agent_loop_instance.start()
        log.info("Agent loop started")
    except Exception as exc:
        log.error("Failed to start agent loop: %s", exc)


async def _warm_prefix_cache(reason: str = "startup"):
    """Send a prefill request with the full system prompt (+ recent memories)
    so vLLM's prefix cache has the KV blocks ready before the first real
    user interaction.  max_tokens=1 means we only pay for one output token."""
    jid = _new_job_id()
    _timeline_init(jid)
    await _broadcast_timeline_event(jid, "cache_warm", "start")

    # Warm what the FAST path actually sends: the lean chat prompt (per
    # surface). The full tool prompt lives on the deep client (:8000), which
    # has its own traffic — warming it here on the fast model wastes the warm.
    system_prompt = build_chat_prompt(tagged=False, user_id=IKA_USER_ID)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    try:
        # Also prefill the tagged (avatar-surface) variant's prefix.
        await llm_client.chat(
            [{"role": "system", "content": build_chat_prompt(tagged=True, user_id=IKA_USER_ID)},
             {"role": "user", "content": "hi"}],
            temperature=0, max_tokens=1)
    except Exception:
        pass

    # Fetch recent memories so the memory prefix is also cached.
    try:
        from memory import mem0_store
        mems = await mem0_store.search("recent context", k=5)
        if mems:
            mem_lines = [f"- ({m['role']}): {m['text']}" for m in mems]
            messages.append({
                "role": "system",
                "content": "[Memory fragments — older snippets retrieved by similarity to the current message. These are NOT the current topic. Use them only for continuity (knowing Ika's preferences, history, running jokes). Do NOT bring up subjects from these fragments unless Ika references them first.]:\n" + "\n".join(mem_lines),
            })
    except Exception as exc:
        log.debug("Cache warm: memory fetch failed (non-fatal): %s", exc)

    messages.append({"role": "user", "content": "hi"})

    try:
        _t0 = time.monotonic()
        result = await llm_client.chat(messages, temperature=0, max_tokens=1)
        _lat = (time.monotonic() - _t0) * 1000
        _usage = result.get("usage", {})
        asyncio.create_task(call_log.log_call(
            CallContext(triggered_by="cache_warm"),
            model=llm_client.model, temperature=0.0, max_tokens=1,
            stream=False, tools_provided=False, messages=messages,
            response_content=result.get("content"),
            finish_reason=result.get("finish_reason"),
            latency_ms=_lat,
            prompt_tokens=_usage.get("prompt_tokens"),
            completion_tokens=_usage.get("completion_tokens"),
            total_tokens=_usage.get("total_tokens"),
        ))
        log.info("Prefix cache warm (%s) OK — %d messages, system prompt %d chars",
                 reason, len(messages), len(system_prompt))
    except Exception as exc:
        log.warning("Prefix cache warm (%s) failed: %s", reason, exc)

    await _broadcast_timeline_event(jid, "cache_warm", "end")


_CACHE_REWARM_INTERVAL = 180  # seconds (3 minutes)
_last_user_interaction: float = 0.0
_last_cache_warm: float = 0.0  # monotonic time of last warm
_messages_since_warm: int = 0   # new messages since last cache warm


async def _llm_warmup():
    """Initial startup warmup: wait for vLLM to be ready, then warm prefix cache."""
    global _last_cache_warm, _messages_since_warm
    await asyncio.sleep(3)
    log.info("LLM warm-up: sending prefill to seed prefix cache…")
    await _warm_prefix_cache("startup")
    _last_cache_warm = time.monotonic()
    _messages_since_warm = 0


async def _cache_rewarm_loop():
    """Periodically re-warm the prefix cache when idle so that fresh memories
    are included in the cached prefix.  Only fires after new messages have
    been exchanged AND the user has been idle for CACHE_REWARM_INTERVAL."""
    global _last_cache_warm, _messages_since_warm
    while True:
        await asyncio.sleep(_CACHE_REWARM_INTERVAL)
        if _messages_since_warm == 0:
            continue  # no new memories to cache
        idle_seconds = time.monotonic() - _last_user_interaction
        if _last_user_interaction > 0 and idle_seconds < _CACHE_REWARM_INTERVAL:
            continue  # user still active, real requests keep cache warm
        log.debug("Idle for %.0fs, %d new messages — re-warming prefix cache…",
                  idle_seconds, _messages_since_warm)
        await _warm_prefix_cache("idle-rewarm")
        _last_cache_warm = time.monotonic()
        _messages_since_warm = 0


@app.get("/", include_in_schema=False)
async def root_redirect():
    """Voice-chat web UI removed; send browsers to the pipeline monitor."""
    return RedirectResponse(url="/monitor", status_code=302)


@app.get("/health")
async def health():
    checks = {}
    _svcs = [("tts", config["tts_url"])]
    if _STT_ENABLED:
        _svcs.insert(0, ("stt", config["stt_url"]))
    for name, url in _svcs:
        try:
            r = await http.get(f"{url}/health", timeout=5.0)
            checks[name] = "ok" if r.status_code == 200 else f"error ({r.status_code})"
        except Exception as e:
            checks[name] = f"down ({e})"
    # Memory is in-process; report ok if the module imports and builds its store.
    try:
        from memory import mem0_store
        mem0_store.get_store()
        checks["memory"] = "ok (mem0 in-process)"
    except Exception as exc:
        checks["memory"] = f"down ({exc})"
    # Animation is CSV-driven (fbx_functions); no service to ping.
    checks["animation"] = f"ok ({ANIMATION_MODE})"
    # LLM health via the dedicated client
    checks["llm"] = "ok" if await llm_client.health() else "down"

    all_ok = all(v.startswith("ok") for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "services": checks,
        "model": llm_config.get("model", ""),
    }


@app.get("/debug/state")
async def debug_state():
    return {**_pipeline_state}


_SENTENCE_END_CHARS = frozenset(".!?")




# ---------------------------------------------------------------------------
#  GET /api/conversation — chat history for the dashboard
# ---------------------------------------------------------------------------

@app.get("/api/conversation")
async def api_conversation(request: Request, limit: int = 0, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    log_data = _get_user_chat_log(user_id)
    entries = log_data[-limit:] if limit > 0 else log_data
    return JSONResponse({"exchanges": entries})


# ---------------------------------------------------------------------------
#  GET /api/cron_jobs — list scheduled jobs with human-readable cadence
# ---------------------------------------------------------------------------

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DOW_NUM_TO_NAME = {
    "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def _cron_to_human(expr: str) -> str:
    """Best-effort human-readable cron description.

    Supports common patterns:
      - ``0 8 * * *``   → "Daily at 08:00"
      - ``0 8 * * 1-5`` → "Weekdays at 08:00"
      - ``0 9 * * 1``   → "Mondays at 09:00"
      - ``*/15 * * * *``→ "Every 15 minutes"
      - ``0 */2 * * *`` → "Every 2 hours, on the hour"
      - ``0 0 1 * *``   → "On the 1st of every month at 00:00"

    Falls back to the raw expression when it's something exotic.
    """
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return expr or ""
    minute, hour, dom, month, dow = parts

    # Every N minutes
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(minute[2:])
            return f"Every {n} minute{'s' if n != 1 else ''}"
        except ValueError:
            pass
    if minute == "*" and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return "Every minute"

    # Every N hours, on the hour
    if minute == "0" and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(hour[2:])
            return f"Every {n} hour{'s' if n != 1 else ''}, on the hour"
        except ValueError:
            pass

    # Specific minute:hour patterns
    try:
        mm = int(minute)
        hh = int(hour)
        time_str = f"{hh:02d}:{mm:02d}"
    except ValueError:
        return expr

    if dom == "*" and month == "*":
        if dow == "*":
            return f"Daily at {time_str}"
        if dow == "1-5":
            return f"Weekdays at {time_str}"
        if dow == "0,6" or dow == "6,0":
            return f"Weekends at {time_str}"
        # Single day or comma list
        names = []
        for tok in dow.split(","):
            name = _DOW_NUM_TO_NAME.get(tok.lower())
            if not name:
                return expr
            names.append(name + "s" if not name.endswith("s") else name)
        return f"{', '.join(names)} at {time_str}"

    if dow == "*" and month == "*":
        # Day-of-month patterns
        try:
            d = int(dom)
            suffix = "th"
            if d % 10 == 1 and d != 11:
                suffix = "st"
            elif d % 10 == 2 and d != 12:
                suffix = "nd"
            elif d % 10 == 3 and d != 13:
                suffix = "rd"
            return f"On the {d}{suffix} of every month at {time_str}"
        except ValueError:
            return expr

    return expr


@app.get("/api/diary/pages")
async def api_diary_pages():
    """List all diary pages (newest first, summary only). Used by the frontend
    book-flip modal to populate the page index."""
    try:
        from memory import diary_store
        pages = await diary_store.recent_pages(n=120)
        return JSONResponse({"pages": pages})
    except Exception as exc:
        log.warning("/api/diary/pages failed: %s", exc)
        return JSONResponse({"pages": [], "error": str(exc)}, status_code=500)


@app.get("/api/diary/page/{date}")
async def api_diary_page(date: str):
    """Return the full diary page for a given date (including activities)."""
    try:
        from memory import diary_store
        page = await diary_store.get_page(date)
        if not page:
            return JSONResponse({"error": "not_found", "date": date}, status_code=404)
        return JSONResponse(page)
    except Exception as exc:
        log.warning("/api/diary/page/%s failed: %s", date, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/diary/page/{date}")
async def api_diary_delete(date: str):
    """Delete a diary page (file + Chroma entry). Irreversible."""
    try:
        from memory import diary_store
        ok = await diary_store.delete_page(date)
        return JSONResponse({"deleted": ok, "date": date})
    except Exception as exc:
        log.warning("/api/diary/page/%s delete failed: %s", date, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/diary/write-now")
async def api_diary_write_now():
    """Force-write today's draft (useful for the dashboard button + manual smoke)."""
    try:
        from bridge import diary_writer
        date = await diary_writer.write_draft(reason="manual")
        return JSONResponse({"written": bool(date), "date": date})
    except Exception as exc:
        log.warning("/api/diary/write-now failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/cron_jobs")
async def api_cron_jobs():
    """Return the current cron job list with human-readable cadence.

    Used by the UI cron modal. Read-only; editing is done via the existing
    schedule_cron / cancel_cron_job tools.
    """
    al = get_agent_loop()
    if not al:
        return JSONResponse({"jobs": []})
    jobs = al.list_jobs()
    for j in jobs:
        j["cron_human"] = _cron_to_human(j.get("cron", ""))
    return JSONResponse({"jobs": jobs})


@app.get("/api/stock-chart")
async def api_stock_chart(symbol: str = "SPY", period: str = "1mo", interval: str = "1d"):
    """Return OHLCV data for the frontend candlestick chart.

    Runs yfinance in a thread pool to avoid blocking the event loop.
    """
    import asyncio

    def _fetch():
        import yfinance as yf
        ticker = yf.Ticker(symbol.upper())
        hist = ticker.history(period=period, interval=interval)
        if hist.empty:
            return []
        data = []
        for date, row in hist.iterrows():
            ts = int(date.timestamp())
            data.append({
                "time": ts,
                "open": round(row["Open"], 2),
                "high": round(row["High"], 2),
                "low": round(row["Low"], 2),
                "close": round(row["Close"], 2),
                "volume": int(row.get("Volume", 0)),
            })
        return data

    try:
        data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
        return JSONResponse({"symbol": symbol.upper(), "period": period, "interval": interval, "data": data})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
#  POST /api/tts — on-demand TTS proxy for the dashboard play button
# ---------------------------------------------------------------------------

@app.post("/api/tts")
async def api_tts(req: TtsRequest, request: Request, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    audio = await _synthesize(req.text, user_id=user_id)
    if audio is None:
        return JSONResponse({"error": "TTS synthesis failed"}, status_code=502)
    return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")


# ---------------------------------------------------------------------------
#  User character / profile API  (all require JWT)
# ---------------------------------------------------------------------------

_USERS_DATA_DIR = ROOT / "data" / "users"
_DEFAULT_CHAR_DIR = ROOT / "character"
_DEFAULT_VOICE_PATH = ROOT / "audio" / "reference_voice.wav"
_MAX_VOICE_MB = 5
_VOICE_EXTS = (".wav", ".mp3", ".ogg", ".m4a")

_MAX_VRM_MB = 50
_MAX_BG_MB = 10


def _user_dir(user_id: str) -> Path:
    d = _USERS_DATA_DIR / user_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
#  Per-user asset library (VRMs + reference voices)
# ---------------------------------------------------------------------------
#
# Layout:
#   data/users/{uid}/
#     vrms/<name>.vrm          ← multi-file VRM library
#     voices/<name>.wav        ← multi-file voice library
#     active.json              ← {"vrm": <name>, "voice": <name>}
#
# Lazy migration: first time a user touches the library, we move the legacy
# single-file character.vrm into vrms/Mocha.vrm and seed voices/default.wav
# from the global audio/reference_voice.wav. Costs nothing for users who
# never visit the Avatar tab.

import json as _lib_json


def _safe_name(name: str) -> str:
    """Sanitize a filename — strip path components, collapse spaces, cap len."""
    base = Path(name).name  # drop any directory parts
    base = base.replace("\x00", "").strip()
    if not base or base in (".", ".."):
        return ""
    return base[:128]


def _user_lib_dir(user_id: str, kind: str) -> Path:
    """kind = 'vrms' | 'voices'."""
    if kind not in ("vrms", "voices"):
        raise ValueError(f"unknown library kind: {kind}")
    d = _user_dir(user_id) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_active_path(user_id: str) -> Path:
    return _user_dir(user_id) / "active.json"


def _read_active(user_id: str) -> dict:
    p = _user_active_path(user_id)
    if not p.exists():
        return {}
    try:
        return _lib_json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_active(user_id: str, active: dict) -> None:
    _user_active_path(user_id).write_text(
        _lib_json.dumps(active, indent=2), encoding="utf-8"
    )


def _ensure_user_library(user_id: str) -> None:
    """Lazy-migrate a user from the old single-file layout to the library
    layout. Idempotent — safe to call on every library read."""
    ud = _user_dir(user_id)
    vrms_dir = ud / "vrms"
    voices_dir = ud / "voices"
    active = _read_active(user_id)
    changed = False

    # ── VRMs ─────────────────────────────────────────────────────────────
    if not vrms_dir.exists():
        vrms_dir.mkdir(parents=True, exist_ok=True)
        legacy = ud / "character.vrm"
        target = vrms_dir / "Mocha.vrm"
        try:
            if legacy.exists():
                legacy.rename(target)
            else:
                global_vrm = _DEFAULT_CHAR_DIR / "Mocha.vrm"
                if global_vrm.exists():
                    import shutil as _sh
                    _sh.copy(global_vrm, target)
        except Exception as exc:
            log.warning("VRM library seed failed for %s: %s", user_id[:8], exc)
        if "vrm" not in active and target.exists():
            active["vrm"] = "Mocha.vrm"
            changed = True

    # ── Voices ───────────────────────────────────────────────────────────
    if not voices_dir.exists():
        voices_dir.mkdir(parents=True, exist_ok=True)
        target = voices_dir / "default.wav"
        try:
            if _DEFAULT_VOICE_PATH.exists():
                import shutil as _sh
                _sh.copy(_DEFAULT_VOICE_PATH, target)
        except Exception as exc:
            log.warning("voice library seed failed for %s: %s", user_id[:8], exc)
        if "voice" not in active and target.exists():
            active["voice"] = "default.wav"
            changed = True

    if changed:
        _write_active(user_id, active)


def _list_assets(user_id: str, kind: str) -> list[dict]:
    """Return [{name, size, uploaded_at}] sorted by name."""
    d = _user_lib_dir(user_id, kind)
    out = []
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "size": st.st_size,
                "uploaded_at": st.st_mtime,
            })
        except Exception:
            pass
    return out


async def _voice_ref_text(voice_path: str | None) -> str | None:
    """Return the reference text for a per-user voice file.

    Reads a sidecar `<voice>.txt` next to the WAV. If missing, transcribes
    the voice via the STT service and writes the sidecar (best-effort —
    a failure here just means TTS will have to transcribe in-process,
    which currently breaks under broken torchcodec). Caching by sidecar
    means we only call STT once per voice, ever.
    """
    if not voice_path:
        return None
    p = Path(voice_path)
    if not p.is_file():
        return None
    sidecar = p.with_suffix(p.suffix + ".txt")
    if sidecar.is_file():
        try:
            text = sidecar.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception as exc:
            log.debug("voice sidecar read failed for %s: %s", p.name, exc)

    # Lazy transcribe via STT — Faster-Whisper, no torchcodec dependency.
    try:
        with p.open("rb") as fh:
            files = {"file": (p.name, fh, "audio/wav")}
            resp = await http.post(f"{config['stt_url']}/transcribe", files=files)
        if resp.status_code != 200:
            log.warning("voice transcribe HTTP %s for %s", resp.status_code, p.name)
            return None
        text = (resp.json().get("text") or "").strip()
        if not text:
            return None
        try:
            sidecar.write_text(text, encoding="utf-8")
        except Exception as exc:
            log.debug("voice sidecar write failed for %s: %s", p.name, exc)
        log.info("Transcribed voice %s -> %s", p.name, text[:80])
        return text
    except Exception as exc:
        log.warning("voice transcribe failed for %s: %s", p.name, exc)
        return None


def _user_active_voice_path(user_id: str | None) -> str | None:
    """Return absolute path to the user's active voice file, or None.

    Used by the bridge when calling TTS /synthesize so the per-user voice
    follows the user. Falls back to None (TTS uses global default) for
    anonymous / non-web channels and when the user has no active voice set.
    """
    if not user_id:
        return None
    try:
        _ensure_user_library(user_id)
        active = _read_active(user_id)
        name = active.get("voice")
        if not name:
            return None
        p = _user_lib_dir(user_id, "voices") / name
        return str(p) if p.is_file() else None
    except Exception as exc:
        log.debug("active voice lookup failed for %s: %s", (user_id or "?")[:8], exc)
        return None


@app.get("/api/user/character")
async def user_character(request: Request, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    ud = _user_dir(user_id)
    soul = (ud / "soul.md").read_text(encoding="utf-8") if (ud / "soul.md").exists() \
        else (_DEFAULT_CHAR_DIR / "soul.md").read_text(encoding="utf-8")
    behaviors = (ud / "behaviors.yaml").read_text(encoding="utf-8") if (ud / "behaviors.yaml").exists() \
        else (_DEFAULT_CHAR_DIR / "behaviors.yaml").read_text(encoding="utf-8")
    return JSONResponse({"soul": soul, "behaviors": behaviors})


@app.put("/api/user/soul")
async def user_update_soul(request: Request, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    body = await request.json()
    content = body.get("content", "")
    (_user_dir(user_id) / "soul.md").write_text(content, encoding="utf-8")
    return JSONResponse({"status": "ok"})


@app.put("/api/user/behaviors")
async def user_update_behaviors(request: Request, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    body = await request.json()
    content = body.get("content", "")
    (_user_dir(user_id) / "behaviors.yaml").write_text(content, encoding="utf-8")
    return JSONResponse({"status": "ok"})


@app.post("/api/user/upload/vrm")
async def user_upload_vrm(request: Request, file: UploadFile = File(...), token: str = ""):
    """Upload a VRM into the user's library. Saved to data/users/{uid}/vrms/.

    The first uploaded VRM auto-becomes active if no active selection exists.
    Filename is derived from the upload (sanitized); duplicate names overwrite.
    """
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    data = await file.read()
    if len(data) > _MAX_VRM_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"VRM file exceeds {_MAX_VRM_MB}MB limit")

    raw_name = _safe_name(file.filename or "uploaded.vrm")
    if not raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not raw_name.lower().endswith(".vrm"):
        raw_name += ".vrm"

    _ensure_user_library(user_id)
    dest = _user_lib_dir(user_id, "vrms") / raw_name
    dest.write_bytes(data)

    # Auto-activate if no active VRM yet (first upload).
    active = _read_active(user_id)
    if not active.get("vrm"):
        active["vrm"] = raw_name
        _write_active(user_id, active)

    return JSONResponse({"status": "ok", "name": raw_name, "size": len(data)})


@app.post("/api/user/upload/voice")
async def user_upload_voice(request: Request, file: UploadFile = File(...), token: str = ""):
    """Upload a reference voice clip into the user's library.

    Saved to data/users/{uid}/voices/. Auto-activates on first upload if no
    active voice exists. Per-user voice is passed to TTS via ref_audio_path
    so no service restart is needed.
    """
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    data = await file.read()
    if len(data) > _MAX_VOICE_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Voice file exceeds {_MAX_VOICE_MB}MB limit")

    raw_name = _safe_name(file.filename or "voice.wav")
    if not raw_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    ext = Path(raw_name).suffix.lower()
    if ext not in _VOICE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Voice file must be one of: {', '.join(_VOICE_EXTS)}",
        )

    _ensure_user_library(user_id)
    dest = _user_lib_dir(user_id, "voices") / raw_name
    dest.write_bytes(data)

    # Eagerly transcribe the new clip so TTS doesn't have to run Whisper
    # ASR in-process (which breaks under torchcodec lib mismatch). The
    # sidecar persists across TTS restarts. Best-effort — a failure here
    # falls back to lazy transcription on first synthesis.
    try:
        await _voice_ref_text(str(dest))
    except Exception as exc:
        log.debug("eager voice transcribe failed for %s: %s", raw_name, exc)

    active = _read_active(user_id)
    if not active.get("voice"):
        active["voice"] = raw_name
        _write_active(user_id, active)

    return JSONResponse({"status": "ok", "name": raw_name, "size": len(data)})


@app.get("/api/user/library")
async def user_library(request: Request, token: str = ""):
    """Return the user's full library state — VRM list, voice list, actives."""
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    _ensure_user_library(user_id)
    active = _read_active(user_id)
    return JSONResponse({
        "vrms": _list_assets(user_id, "vrms"),
        "voices": _list_assets(user_id, "voices"),
        "active_vrm": active.get("vrm"),
        "active_voice": active.get("voice"),
    })


@app.post("/api/user/active")
async def user_set_active(request: Request, token: str = ""):
    """Switch the active asset for a kind. Body: {kind, name}."""
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    body = await request.json()
    kind = body.get("kind")
    name = _safe_name(body.get("name") or "")
    if kind not in ("vrm", "voice") or not name:
        raise HTTPException(status_code=400, detail="kind must be vrm|voice and name required")

    _ensure_user_library(user_id)
    target = _user_lib_dir(user_id, kind + "s") / name
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"{kind} '{name}' not found in library")

    active = _read_active(user_id)
    active[kind] = name
    _write_active(user_id, active)
    return JSONResponse({"status": "ok", "kind": kind, "active": name})


@app.get("/api/user/voice-file")
async def user_voice_file(request: Request, name: str = "", token: str = ""):
    """Serve a single voice file from the user's voices/ library.

    Used by the Avatar tab to play back a clip in an <audio> preview.
    """
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    name = _safe_name(name)
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    p = _user_lib_dir(user_id, "voices") / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="voice not found")
    # Generic audio media type — browsers sniff actual format from content.
    return FileResponse(p, media_type="audio/wav", filename=name)


@app.get("/api/character-default")
async def character_default(kind: str = ""):
    """Serve the GLOBAL default soul.md or behaviors.yaml (no auth — these
    are the public starter content). Used by the Avatar tab's 'Reset to
    default' buttons to repopulate the editor without overwriting the
    user's saved file (user must click Save afterwards)."""
    if kind == "soul":
        p = _DEFAULT_CHAR_DIR / "soul.md"
        media = "text/markdown; charset=utf-8"
    elif kind == "behaviors":
        p = _DEFAULT_CHAR_DIR / "behaviors.yaml"
        media = "text/yaml; charset=utf-8"
    else:
        raise HTTPException(status_code=400, detail="kind must be soul|behaviors")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="default not found")
    return FileResponse(p, media_type=media, filename=p.name)


@app.delete("/api/user/asset")
async def user_delete_asset(request: Request, kind: str = "", name: str = "", token: str = ""):
    """Remove a library asset. Refuses to delete the currently-active one
    (caller must switch active first)."""
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if kind not in ("vrm", "voice"):
        raise HTTPException(status_code=400, detail="kind must be vrm|voice")
    name = _safe_name(name)
    if not name:
        raise HTTPException(status_code=400, detail="name required")

    _ensure_user_library(user_id)
    active = _read_active(user_id)
    if active.get(kind) == name:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete the active {kind}. Switch active first.",
        )

    target = _user_lib_dir(user_id, kind + "s") / name
    if target.is_file():
        target.unlink()
    return JSONResponse({"status": "ok", "kind": kind, "deleted": name})


@app.post("/api/user/upload/background")
async def user_upload_background(request: Request, file: UploadFile = File(...), token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    data = await file.read()
    if len(data) > _MAX_BG_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Background exceeds {_MAX_BG_MB}MB limit")
    ext = Path(file.filename or "bg.jpg").suffix or ".jpg"
    dest = _user_dir(user_id) / f"background{ext}"
    # Remove old background files with different extensions
    for old in _user_dir(user_id).glob("background.*"):
        old.unlink(missing_ok=True)
    dest.write_bytes(data)
    return JSONResponse({"status": "ok", "size": len(data)})


@app.get("/api/user/vrm")
async def user_vrm(request: Request, token: str = ""):
    """Serve the user's currently-active VRM from their library.

    Resolution order:
      1) data/users/{uid}/vrms/<active>      (library, current)
      2) data/users/{uid}/character.vrm      (legacy single-slot, pre-library)
      3) character/Mocha.vrm                 (global default)
    """
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if user_id:
        try:
            _ensure_user_library(user_id)
            active = _read_active(user_id).get("vrm")
            if active:
                lib_path = _user_lib_dir(user_id, "vrms") / active
                if lib_path.is_file():
                    return FileResponse(
                        lib_path, media_type="model/gltf-binary", filename=active,
                    )
        except Exception as exc:
            log.debug("active VRM lookup failed: %s", exc)
        legacy = _user_dir(user_id) / "character.vrm"
        if legacy.exists():
            return FileResponse(legacy, media_type="model/gltf-binary", filename="character.vrm")
    fallback = _DEFAULT_CHAR_DIR / "Mocha.vrm"
    if fallback.exists():
        return FileResponse(fallback, media_type="model/gltf-binary", filename="Mocha.vrm")
    raise HTTPException(status_code=404, detail="No VRM model found")


@app.get("/api/user/background")
async def user_background(request: Request, token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    if user_id:
        ud = _user_dir(user_id)
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            bg = ud / f"background{ext}"
            if bg.exists():
                return FileResponse(bg)
    raise HTTPException(status_code=404, detail="No background set")


# ---------------------------------------------------------------------------
#  Telegram bot management — per-user bot registration
# ---------------------------------------------------------------------------

@app.get("/api/user/telegram")
async def get_user_telegram(request: Request):
    from auth.deps import get_current_user as _gcu
    from auth.models import CurrentUser as _CU
    user = _gcu(authorization=request.headers.get("authorization", ""))
    from auth.db import get_user_setting
    token = get_user_setting(user.user_id, "telegram_bot_token") or ""
    return JSONResponse({"configured": bool(token)})


@app.put("/api/user/telegram")
async def set_user_telegram(request: Request):
    from auth.deps import get_current_user as _gcu
    user = _gcu(authorization=request.headers.get("authorization", ""))
    body = await request.json()
    new_token = (body.get("bot_token") or "").strip()
    if not new_token:
        raise HTTPException(status_code=400, detail="bot_token required")

    from auth.db import update_user_setting
    from channels.base import registry
    from channels.telegram import TelegramChannel

    # Stop the old bot if one is running for this user.
    await registry.stop_user_bot(user.user_id)

    # Persist the new token.
    update_user_setting(user.user_id, "telegram_bot_token", new_token)

    # Start the new bot immediately.
    ch = TelegramChannel(
        bot_token=new_token,
        bridge_url=BRIDGE_INTERNAL_URL,
        app_user_id=user.user_id,
    )
    registry.register_user_bot(user.user_id, ch)
    try:
        await ch.start()
    except Exception as exc:
        await registry.stop_user_bot(user.user_id)
        from auth.db import delete_user_setting
        delete_user_setting(user.user_id, "telegram_bot_token")
        raise HTTPException(status_code=400, detail=f"Bot failed to start: {exc}")

    return JSONResponse({"status": "ok", "message": "Telegram bot registered and started."})


@app.delete("/api/user/telegram")
async def delete_user_telegram(request: Request):
    from auth.deps import get_current_user as _gcu
    user = _gcu(authorization=request.headers.get("authorization", ""))
    from auth.db import delete_user_setting
    from channels.base import registry
    await registry.stop_user_bot(user.user_id)
    delete_user_setting(user.user_id, "telegram_bot_token")
    delete_user_setting(user.user_id, "telegram_chat_id")
    return JSONResponse({"status": "ok", "message": "Telegram bot removed."})


# ---------------------------------------------------------------------------
#  GET /chat/stream — SSE streaming chat hub (dashboard + unified log)
#  Yields each segment as a server-sent event in real-time as the LLM produces it.
#  Supports an inline ReAct-style tool loop when tools.enabled is true.
# ---------------------------------------------------------------------------

@app.get("/chat/stream")
async def chat_stream(request: Request, text: str, client_id: str = "",
                      token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    _touch_interaction(user_text=text)
    jid = _new_job_id()
    _timeline_init(jid)
    await _broadcast_debug_state("thinking", last_input=text)
    mem_task = asyncio.create_task(_query_memories(text, user_id=user_id))
    collected_chunks: list[dict] = []
    tool_events: list[dict] = []

    async def generate():
        nonlocal collected_chunks, tool_events

        try:
            await _broadcast_timeline_event(jid, "llm", "start")

            memories = await mem_task
            _cs_ctx = CallContext(triggered_by="chat_stream", source="web",
                                 user_id=user_id, conversation_id=str(jid))

            total_chunks = 0
            full_text = ""
            async for ev in _run_inline_turn(text, memories, job_id=jid,
                                              source="web", log_ctx=_cs_ctx,
                                              user_id=user_id):
                etype = ev.get("type")
                if etype == "thinking_delta":
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "reads_debug":
                    # Mocha's emotional read of the user. Forwarded to the
                    # frontend so a debug overlay can display it; not piped
                    # to TTS.
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "emotion":
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "gesture":
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "viseme_update":
                    # Late lip-sync upgrade for an already-delivered chunk.
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "speech_chunk":
                    if not collected_chunks:
                        await _broadcast_debug_state(
                            "speaking", tts_ms=0,
                        )
                    collected_chunks.append(ev)
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "tool_status":
                    tool_events.append(ev)
                    yield f"data: {json.dumps(ev)}\n\n"
                elif etype == "speech_end":
                    total_chunks = ev.get("total_chunks", 0)
                    full_text = ev.get("full_text", "")
                    yield f"data: {json.dumps(ev)}\n\n"

            await _broadcast_timeline_event(jid, "llm", "end")
        except Exception as exc:
            log.exception("chat_stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return

        # Store history + memories + chat log entry
        if full_text:
            _append_history(user_id, "user", text)
            _append_history(user_id, "assistant", full_text)
            asyncio.create_task(_store_memory(text, "user", user_id=user_id))
            asyncio.create_task(_store_memory(full_text, "assistant", user_id=user_id))
            entry = {
                "user_text": text,
                "assistant_text": full_text,
                "source": "dashboard",
                "timestamp": time.time(),
                "_client_id": client_id,
                "chunks": [
                    {"text": c.get("text"), "chunk_idx": c.get("chunk_idx"),
                     "audio_base64": c.get("audio_base64")}
                    for c in collected_chunks
                ],
                "tool_events": tool_events or None,
            }
            _get_user_chat_log(user_id).append(entry)
            await _broadcast_chat_entry(entry)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        await _broadcast_debug_state("idle")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/admin/reload-tools")
async def reload_tools():
    """Hot-reload custom tools from tools/custom/."""
    global TOOL_SCHEMAS
    import tools.registry as _reg
    _reg.reload_custom_tools()
    TOOL_SCHEMAS = _reg.TOOL_SCHEMAS
    return JSONResponse({"status": "ok", "tools": _reg.TOOL_NAMES})


# ---------------------------------------------------------------------------
#  POST /admin/eval — run a prompt through the real Mocha pipeline in isolation
# ---------------------------------------------------------------------------

class EvalRequest(BaseModel):
    text: str
    mode: str = "tool_loop"
    history: list[dict] | None = None
    include_memory: bool = False
    include_audio: bool = False
    # Override the turn source to exercise realtime (e.g. "web") vs non-realtime
    # (e.g. "telegram") paths — notably to test the verifier. Default "eval" is
    # non-realtime, so a plain eval runs the verifier.
    source: str = "eval"


@app.post("/admin/eval")
async def admin_eval(req: EvalRequest):
    """Run a prompt through the real inline-tag pipeline in an isolated scope.

    Suppresses broadcasts and memory writes. Captures every tool call, LLM call,
    and handle minted during the run. Returns a JSON blob rich enough for a
    caller (human or LLM) to judge correctness.
    """
    import uuid as _uuid
    from tools.executor import EVAL_CAPTURE, EVAL_TOOL_ROUND
    from tools.handle_registry import EVAL_HANDLES
    from character.context import build_system_prompt as _build_sp

    eval_id = str(_uuid.uuid4())
    jid = _new_job_id()
    conversation_id = f"eval-{eval_id}"

    _preview_snap = {
        "variables": dict(_active_preview.get("variables") or {}),
        "css": _active_preview.get("css") or "",
        "background_image_url": _active_preview.get("background_image_url") or "",
        "background_overlay_rgba": _active_preview.get("background_overlay_rgba") or "",
        "html_decor": _active_preview.get("html_decor") or "",
        "html_mods": list(_active_preview.get("html_mods") or []),
    }
    _modals_snap = {k: dict(v) for k, v in _OPEN_MODALS.items()}

    _iso_tok = EVAL_ISOLATION.set(True)
    _tool_calls: list[dict] = []
    _handles: list[str] = []
    _cap_tok = EVAL_CAPTURE.set(_tool_calls)
    _hand_tok = EVAL_HANDLES.set(_handles)
    _round_tok = EVAL_TOOL_ROUND.set(0)

    errors: list[str] = []
    chunks: list[dict] = []
    emotions: list[str] = []
    gestures: list[str] = []
    full_text = ""

    t0 = time.monotonic()
    try:
        memories: list[dict] = []
        if req.include_memory:
            try:
                memories = await _query_memories(req.text)
            except Exception as exc:
                errors.append(f"memory query failed: {exc}")

        _ctx = CallContext(triggered_by="admin_eval", source="eval", user_id="eval",
                           conversation_id=conversation_id)
        try:
            async for ev in _run_inline_turn(req.text, memories, job_id=jid,
                                              source=req.source, log_ctx=_ctx):
                etype = ev.get("type")
                if etype == "emotion":
                    emotions.append(ev.get("id", ""))
                elif etype == "gesture":
                    gestures.append(ev.get("name", ""))
                elif etype == "speech_chunk":
                    chunks.append(ev)
                elif etype == "speech_end":
                    full_text = ev.get("full_text") or ""
        except Exception as exc:
            errors.append(f"pipeline failed: {exc}")
    finally:
        _active_preview.update(_preview_snap)
        _OPEN_MODALS.clear()
        _OPEN_MODALS.update(_modals_snap)
        EVAL_TOOL_ROUND.reset(_round_tok)
        EVAL_HANDLES.reset(_hand_tok)
        EVAL_CAPTURE.reset(_cap_tok)
        EVAL_ISOLATION.reset(_iso_tok)
    total_latency_ms = (time.monotonic() - t0) * 1000

    llm_calls: list[dict] = []
    try:
        pool = call_log._pool
        if pool is not None:
            async with pool.acquire() as conn:
                await asyncio.sleep(0.25)
                rows = await conn.fetch(
                    """
                    SELECT triggered_by, tool_round, pass_number,
                           prompt_tokens, completion_tokens, total_tokens,
                           latency_ms, finish_reason, error,
                           (response_tool_calls IS NOT NULL AND jsonb_array_length(response_tool_calls) > 0) AS has_tool_call
                    FROM llm_call_log
                    WHERE conversation_id = $1
                    ORDER BY id
                    """,
                    conversation_id,
                )
                llm_calls = [dict(r) for r in rows]
    except Exception as exc:
        errors.append(f"pg join failed: {exc}")

    if not full_text:
        full_text = " ".join(c.get("text", "") for c in chunks)

    try:
        system_prompt_bytes = len(_build_sp(animation_mode=ANIMATION_MODE,
                                            tools_available=TOOLS_ENABLED))
    except Exception:
        system_prompt_bytes = 0

    return JSONResponse({
        "status": "ok" if not errors else "partial",
        "eval_id": eval_id,
        "conversation_id": conversation_id,
        "prompt": req.text,
        "chunks": [
            {"text": c.get("text"), "chunk_idx": c.get("chunk_idx")}
            for c in chunks
        ],
        "emotions": emotions,
        "gestures": gestures,
        "full_text": full_text,
        "tool_calls": _tool_calls,
        "llm_calls": llm_calls,
        "handles_issued": _handles,
        "system_prompt_bytes": system_prompt_bytes,
        "total_latency_ms": round(total_latency_ms, 1),
        "errors": errors,
    })


# ---------------------------------------------------------------------------
#  Theme toolbox — live preview + screenshot capture (used by Hana + theme_*)
# ---------------------------------------------------------------------------
import uuid

_screenshot_waiters: dict[str, asyncio.Future] = {}
# Server-side mirror of the last broadcast preview so theme_apply can serialize it.
_active_preview: dict = {
    "variables": {},
    "css": "",
    "background_image_url": "",
    "background_overlay_rgba": "",
    "html_decor": "",
    "html_mods": [],
}


# Track which modal-class windows are currently on-screen. Mocha's system
# prompt gets a "Currently on screen: ..." line built from this so she knows
# what can be closed / referenced.
_OPEN_MODALS: dict[str, dict] = {}


def _set_open_modal(kind: str, info: dict) -> None:
    """Record that a modal of this kind is open. `kind` is e.g. 'video_player' or 'presentation'."""
    prev = _OPEN_MODALS.get(kind) or {}
    prev.update(info or {})
    _OPEN_MODALS[kind] = prev


def _clear_open_modal(kind: str) -> None:
    _OPEN_MODALS.pop(kind, None)


def get_open_modals_summary() -> str:
    """Return a short human line like
        '1 video_player ("Lofi Study Loop"); 1 presentation ("Tesla Q4")'
    or empty string when nothing is open. Used by character/context when
    building Mocha's system prompt."""
    if not _OPEN_MODALS:
        return ""
    bits = []
    for kind, info in _OPEN_MODALS.items():
        title = (info.get("title") or "").strip()
        if title:
            bits.append(f"{kind} (\"{title}\")")
        else:
            bits.append(kind)
    return "; ".join(bits)


def get_active_preview() -> dict:
    return {
        "variables": dict(_active_preview.get("variables") or {}),
        "css": _active_preview.get("css") or "",
        "background_image_url": _active_preview.get("background_image_url") or "",
        "background_overlay_rgba": _active_preview.get("background_overlay_rgba") or "",
        "html_decor": _active_preview.get("html_decor") or "",
        "html_mods": list(_active_preview.get("html_mods") or []),
    }


def _reset_active_preview() -> None:
    _active_preview["variables"] = {}
    _active_preview["css"] = ""
    _active_preview["background_image_url"] = ""
    _active_preview["background_overlay_rgba"] = ""
    _active_preview["html_decor"] = ""
    _active_preview["html_mods"] = []


@app.post("/admin/theme/preview")
async def admin_theme_preview(payload: dict):
    """Push a live theme preview to all connected web clients.

    Body: {"variables": {...}, "css": "...", "background_image_url": "...",
    "background_overlay_rgba": "r,g,b,a", "html_decor": "...", "html_mods": [...]}.
    Never touches disk. Ambient music is handled separately by video_player.
    """
    variables = payload.get("variables") or {}
    css = payload.get("css") or ""
    background_image_url = (payload.get("background_image_url") or "").strip()
    background_overlay_rgba = (payload.get("background_overlay_rgba") or "").strip()
    html_decor = payload.get("html_decor") or ""
    html_mods = payload.get("html_mods") or []
    if not isinstance(variables, dict):
        raise HTTPException(status_code=400, detail="variables must be an object")
    if not isinstance(css, str):
        raise HTTPException(status_code=400, detail="css must be a string")
    if not isinstance(html_decor, str):
        raise HTTPException(status_code=400, detail="html_decor must be a string")
    if not isinstance(html_mods, list):
        raise HTTPException(status_code=400, detail="html_mods must be a list")

    _active_preview["variables"] = dict(variables)
    _active_preview["css"] = css
    _active_preview["background_image_url"] = background_image_url
    _active_preview["background_overlay_rgba"] = background_overlay_rgba
    _active_preview["html_decor"] = html_decor
    _active_preview["html_mods"] = list(html_mods)

    await _broadcast_clients({
        "type": "ui_command",
        "action": "apply_theme_preview",
        "variables": variables,
        "css": css,
        "background_image_url": background_image_url,
        "background_overlay_rgba": background_overlay_rgba,
        "html_decor": html_decor,
        "html_mods": html_mods,
    })
    return JSONResponse({
        "status": "preview_sent",
        "clients": len(_ws_clients),
        "variables_count": len(variables),
        "css_bytes": len(css),
        "background_image_url": background_image_url or None,
        "html_decor_bytes": len(html_decor),
        "html_mods_count": len(html_mods),
    })


@app.post("/admin/theme/clear-preview")
async def admin_theme_clear_preview():
    """Remove the live preview from all connected web clients."""
    _reset_active_preview()
    await _broadcast_clients({
        "type": "ui_command",
        "action": "clear_theme_preview",
    })
    return JSONResponse({"status": "preview_cleared", "clients": len(_ws_clients)})


async def request_screenshot(timeout: float = 8.0) -> str:
    """Ask any connected web client to html2canvas its current viewport, then
    await the base64 PNG reply. Returns "" if no client is available or the
    deadline expires.
    """
    if not _ws_clients:
        return ""

    req_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _screenshot_waiters[req_id] = fut

    await _broadcast_clients({
        "type": "ui_command",
        "action": "capture_screenshot",
        "request_id": req_id,
    })

    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("Screenshot %s timed out after %.1fs", req_id, timeout)
        return ""
    finally:
        _screenshot_waiters.pop(req_id, None)


@app.post("/admin/theme/screenshot-reply")
async def admin_theme_screenshot_reply(payload: dict):
    """Receive a base64 PNG from the web client in response to a capture request."""
    req_id = (payload.get("request_id") or "").strip()
    image_b64 = payload.get("image_b64") or ""
    error = payload.get("error") or ""
    if not req_id:
        raise HTTPException(status_code=400, detail="request_id required")
    fut = _screenshot_waiters.get(req_id)
    if fut is None or fut.done():
        return JSONResponse({"status": "ignored", "reason": "no pending waiter"})
    if error:
        fut.set_exception(RuntimeError(f"capture failed: {error}"))
    else:
        fut.set_result(image_b64)
    return JSONResponse({"status": "ok"})


@app.post("/admin/theme/reload-stylesheets")
async def admin_theme_reload_stylesheets():
    """Ask all connected web clients to bust the <link> cache by appending ?v=<ts>."""
    await _broadcast_clients({
        "type": "ui_command",
        "action": "reload_stylesheets",
        "v": int(time.time()),
    })
    return JSONResponse({"status": "reloaded", "clients": len(_ws_clients)})


@app.post("/admin/clear-memory")
async def admin_clear_memory(request: Request):
    """Wipe Mocha's recent context for the caller.

    Query param: ?window=1h|3h|1d|1w|all (default 1h).

    All windows wipe the in-process short-term ring buffers — that's what
    Mocha's prompt actually reads from, so this is the right semantic for
    "she forgets what we just talked about" at any window.

    `all` additionally wipes the long-term mem0 facts and the user's diary.
    The PG llm_call_log is never touched (it's a write-only audit trail).
    """
    window = (request.query_params.get("window") or "1h").lower()
    if window not in ("1h", "3h", "1d", "1w", "all"):
        raise HTTPException(status_code=400, detail="window must be one of 1h, 3h, 1d, 1w, all")

    uid = _extract_user_id(request) or _ANON_USER_ID

    try:
        # 1) Always: drop in-process short-term context — and its on-disk copy,
        # or the "cleared" history would resurrect on the next restart.
        _user_histories[uid].clear()
        _user_chat_logs[uid].clear()
        _user_summaries.pop(uid, None)
        from bridge import history_store
        history_store.schedule_persist(uid, [], "")
        log.info("clear-memory window=%s uid=%s (short-term wiped)", window, uid[:8])

        # 2) "all" → also wipe long-term layers for this user.
        if window == "all":
            from memory import mem0_store, diary_store
            await mem0_store.clear(uid)
            await diary_store.delete_all_pages(uid)
            log.info("clear-memory window=all uid=%s (mem0 + diary wiped)", uid[:8])

        # Notify monitor clients so the UI can show a status pill.
        await _broadcast_monitor({"type": "memory_cleared", "window": window})
        return JSONResponse({"status": "cleared", "window": window, "user_id": uid})
    except Exception as exc:
        log.error("Failed to clear memory (window=%s): %s", window, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/tts/restart")
async def admin_tts_restart():
    """In-place reload of the TTS model (clears ref-audio cache). Only works
    if the TTS process is already alive and serving HTTP. For cases where the
    process itself has died, use ``/admin/services/restart`` instead."""
    try:
        resp = await http.post(f"{config['tts_url']}/reload", timeout=30.0)
        resp.raise_for_status()
        log.info("TTS model reloaded via admin endpoint")
        return JSONResponse({"status": "reloaded"})
    except Exception as exc:
        log.error("TTS reload failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


_ALLOWED_SERVICE_RESTART = {"stt", "tts", "all"}


@app.post("/admin/services/restart")
async def admin_services_restart(service: str = "tts"):
    """Process-level restart via ``./start.sh`` so it works even when the
    target service is dead (no HTTP to poke). Runs as fire-and-forget
    subprocesses so the bridge keeps serving this HTTP call.

    Query string: ``?service=stt`` | ``?service=tts`` | ``?service=all``.
    """
    service = (service or "").strip().lower()
    if service not in _ALLOWED_SERVICE_RESTART:
        raise HTTPException(
            status_code=400,
            detail=f"service must be one of: {sorted(_ALLOWED_SERVICE_RESTART)}",
        )

    script = ROOT / "start.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail=f"start.sh not found at {script}")

    # ``./start.sh <svc>`` is idempotent: if already running it no-ops, otherwise
    # starts. For a forced restart we stop first, but only for that specific
    # service (not `stop` which would kill bridge too). We rely on start.sh's
    # stale-pid handling to replace the process cleanly.
    async def _run(args: list[str]) -> tuple[int, str]:
        # stdout/stderr to DEVNULL — start.sh forks uvicorn in the background
        # with its own log redirect, so inheriting the pipe would make
        # communicate() hang waiting for the daemonized child's EOF.
        proc = await asyncio.create_subprocess_exec(
            str(script), *args,
            cwd=str(ROOT),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (-1, "start.sh timed out after 10s")
        return (proc.returncode or 0, "")

    services = ["stt", "tts"] if service == "all" else [service]
    results: list[dict] = []

    # Kill any existing pid for each target (start.sh's start_service already
    # handles stale pidfiles on port, but we want to guarantee a fresh process
    # every click so model caches reload too).
    for svc in services:
        pid_file = ROOT / ".pids" / f"{svc}.pid"
        if pid_file.exists():
            try:
                import signal as _signal
                pid = int(pid_file.read_text().strip())
                os.kill(pid, _signal.SIGTERM)
                log.info("[/admin/services/restart] sent SIGTERM to %s pid=%d", svc, pid)
            except Exception as exc:
                log.debug("[/admin/services/restart] stop %s: %s", svc, exc)
        # Always then fire start — covers the "process already dead" case too.
        rc, out = await _run([svc])
        ok = rc == 0
        tail = (out or "").strip().splitlines()[-3:]
        results.append({"service": svc, "ok": ok, "returncode": rc,
                        "output_tail": "\n".join(tail)})
        log.info("[/admin/services/restart] %s rc=%s", svc, rc)

    return JSONResponse({"services": results})


@app.post("/admin/tts/upload-voice")
async def admin_tts_upload_voice(file: UploadFile = File(...)):
    """Upload a new reference voice (.wav or .ogg), backup the old one, and reload TTS."""
    audio_dir = ROOT / "audio"
    ref_path = audio_dir / "reference_voice.wav"
    backup_path = audio_dir / "reference_voice_backup.wav"

    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in (".wav", ".ogg"):
        raise HTTPException(status_code=400, detail="Only .wav and .ogg files are accepted")

    content = await file.read()
    if len(content) < 1000:
        raise HTTPException(status_code=400, detail="File too small to be valid audio")

    tmp_path = audio_dir / f"_upload_tmp{ext}"
    try:
        tmp_path.write_bytes(content)

        # Convert .ogg → .wav via ffmpeg
        if ext == ".ogg":
            wav_tmp = audio_dir / "_upload_tmp.wav"
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/ffmpeg", "-y", "-i", str(tmp_path),
                "-ar", "48000", "-ac", "1", "-sample_fmt", "s16",
                str(wav_tmp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"ffmpeg conversion failed: {stderr.decode()[-200:]}"
                )
            tmp_path.unlink(missing_ok=True)
            tmp_path = wav_tmp

        # Backup current reference voice
        if ref_path.is_file():
            shutil.copy2(str(ref_path), str(backup_path))
            log.info("Backed up reference voice to %s", backup_path)

        # Move new file into place
        shutil.move(str(tmp_path), str(ref_path))
        log.info("New reference voice saved: %s (%d bytes)", ref_path, len(content))

        # Transcribe the new clip via STT and update reference_text
        # (F5-TTS built-in Whisper depends on broken TorchCodec, so we must provide text)
        try:
            stt_url = config.get("stt_url", f"http://{INTERNAL_HOST}:{full_config['stt']['port']}")
            with open(str(ref_path), "rb") as af:
                resp_stt = await http.post(
                    f"{stt_url}/transcribe",
                    files={"file": ("reference_voice.wav", af, "audio/wav")},
                    timeout=30.0,
                )
            resp_stt.raise_for_status()
            new_ref_text = resp_stt.json().get("text", "").strip()
            if new_ref_text:
                _persist_config("tts.reference_text", new_ref_text)
                log.info("Updated reference_text via STT: %s", new_ref_text)
            else:
                log.warning("STT returned empty transcription for new reference voice")
        except Exception as exc:
            log.warning("Failed to transcribe new reference voice via STT: %s", exc)

        # Reload TTS
        try:
            resp = await http.post(f"{config['tts_url']}/reload", timeout=30.0)
            resp.raise_for_status()
            log.info("TTS reloaded after voice upload")
        except Exception as exc:
            log.warning("TTS reload after upload failed: %s (voice file saved OK)", exc)
            return JSONResponse({
                "status": "saved_but_reload_failed",
                "detail": str(exc),
                "backup_exists": backup_path.is_file(),
            })

        return JSONResponse({
            "status": "ok",
            "backup_exists": backup_path.is_file(),
        })

    except HTTPException:
        raise
    except Exception as exc:
        log.error("Voice upload failed: %s", exc)
        for p in [audio_dir / "_upload_tmp.wav", audio_dir / "_upload_tmp.ogg"]:
            p.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/tts/rollback-voice")
async def admin_tts_rollback_voice():
    """Restore reference_voice_backup.wav as the active reference voice and reload TTS."""
    audio_dir = ROOT / "audio"
    ref_path = audio_dir / "reference_voice.wav"
    backup_path = audio_dir / "reference_voice_backup.wav"

    if not backup_path.is_file():
        raise HTTPException(status_code=404, detail="No backup file exists")

    shutil.move(str(backup_path), str(ref_path))
    log.info("Rolled back reference voice from backup")

    # Transcribe the restored clip via STT and update reference_text
    try:
        stt_url = config.get("stt_url", f"http://{INTERNAL_HOST}:{full_config['stt']['port']}")
        with open(str(ref_path), "rb") as af:
            resp_stt = await http.post(
                f"{stt_url}/transcribe",
                files={"file": ("reference_voice.wav", af, "audio/wav")},
                timeout=30.0,
            )
        resp_stt.raise_for_status()
        new_ref_text = resp_stt.json().get("text", "").strip()
        if new_ref_text:
            _persist_config("tts.reference_text", new_ref_text)
            log.info("Updated reference_text via STT after rollback: %s", new_ref_text)
    except Exception as exc:
        log.warning("Failed to transcribe reference voice after rollback: %s", exc)

    try:
        resp = await http.post(f"{config['tts_url']}/reload", timeout=30.0)
        resp.raise_for_status()
        log.info("TTS reloaded after voice rollback")
    except Exception as exc:
        log.warning("TTS reload after rollback failed: %s", exc)
        return JSONResponse({
            "status": "rolled_back_but_reload_failed",
            "detail": str(exc),
            "backup_exists": False,
        })

    return JSONResponse({"status": "ok", "backup_exists": False})


@app.get("/admin/tts/voice-status")
async def admin_tts_voice_status():
    """Check whether a backup reference voice exists (for rollback button state)."""
    audio_dir = ROOT / "audio"
    return JSONResponse({
        "backup_exists": (audio_dir / "reference_voice_backup.wav").is_file(),
        "reference_exists": (audio_dir / "reference_voice.wav").is_file(),
    })


# ---------------------------------------------------------------------------
#  POST /channel — text-only intake for messaging channels (Telegram, Discord, CLI)
# ---------------------------------------------------------------------------

def _resolve_reply_context(reply_to_message_id: int | None,
                           quoted_text: str | None) -> str | None:
    """Build the system block injected when Ika replies to a Mocha message.

    Resolves the exact shared article by message_id (preferred — see
    autonomy/news_ledger.py), falling back to the quoted text for messages that
    predate the ledger. Returns None when there's nothing to anchor on."""
    if not reply_to_message_id and not quoted_text:
        return None
    rec = None
    if reply_to_message_id is not None:
        try:
            from autonomy import news_ledger
            rec = news_ledger.get(reply_to_message_id)
        except Exception:
            rec = None
    if rec:
        meta = []
        if rec.get("source"):
            meta.append(rec["source"])
        if rec.get("published_at"):
            meta.append(f"published {str(rec['published_at'])[:10]}")
        meta_line = f" ({', '.join(meta)})" if meta else ""
        take = (rec.get("take") or "").strip()
        url = rec.get("url") or ""
        lines = [
            "[Ika is replying to a news item YOU shared with him earlier. This is "
            "the source material — treat it as known, don't re-verify it:",
            f"- headline: \"{rec.get('headline', '')}\"{meta_line}",
        ]
        if take:
            lines.append(f"- your take when you sent it: \"{take}\"")
        if url:
            lines.append(f"- link: {url}")
        lines.append(
            "Pick the thread back up and answer what he's asking. If he wants more "
            "like it, call recall_news to find related articles you've shared. "
            "Don't recite the headline verbatim or read the URL aloud.]"
        )
        return "\n".join(lines)
    if quoted_text:
        return (
            "[Ika is replying to an earlier message of yours, which said: "
            f"\"{quoted_text.strip()[:400]}\". Pick that thread back up and answer "
            "his reply in context. If it was about news you shared, recall_news can "
            "find related items.]"
        )
    return None


@app.post("/channel")
async def channel_intake(req: ChannelRequest):
    """Text-only pipeline for external channels.

    Runs memory + LLM (with optional tool loop) but skips TTS —
    channels only need the text response.
    """
    _touch_interaction(user_text=req.text)
    jid = _new_job_id()
    log.info("Channel [%s/%s]: %s", req.source, req.user_id, req.text[:80])

    # Single-operator identity: all channels funnel into Ika's one namespace
    # (the raw chat_id / app_user_id are still used below for outbound routing).
    # Kick memory search off FIRST so it overlaps the side-writes below instead
    # of running serially after them.
    ch_user_id = IKA_USER_ID
    mem_task = asyncio.create_task(_query_memories(req.text, user_id=ch_user_id))

    # Learn / remember the primary user for each channel so the channel router
    # can DM them proactively (reminders, cron findings, etc.). File write —
    # off the event loop.
    if req.user_id and req.user_id != "agent":
        try:
            from bridge.channel_router import save_primary_user
            await asyncio.to_thread(
                save_primary_user, req.source, req.user_id, app_user_id=req.app_user_id)
        except Exception as exc:
            log.warning("save_primary_user failed: %s", exc)

    # Persist telegram_chat_id in user settings for proactive delivery.
    # psycopg2 connect+write — blocking, so run it in a thread.
    if req.source == "telegram" and req.app_user_id and req.user_id not in ("unknown", "agent"):
        try:
            from auth.db import update_user_setting
            await asyncio.to_thread(
                update_user_setting, req.app_user_id, "telegram_chat_id", req.user_id)
        except Exception as exc:
            log.warning("Failed to save telegram_chat_id: %s", exc)

    await _broadcast_debug_state("thinking", last_input=req.text)
    memories = await mem_task

    # Reply handling — if Ika replied to one of Mocha's messages, resolve the
    # article she shared so the graph can pick that thread back up.
    reply_context = _resolve_reply_context(req.reply_to_message_id, req.quoted_text)

    _ch_ctx = CallContext(triggered_by="channel", source=req.source,
                          user_id=req.user_id, conversation_id=str(jid))

    # Drive the inline-tag turn; collect full_text. For text-only channels we
    # don't care about audio chunks — they still get generated (for dashboard
    # mirroring) but we just track the spoken text to return via HTTP.
    full_text_parts: list[str] = []
    chunks: list[dict] = []
    async for ev in _run_inline_turn(req.text, memories, job_id=jid,
                                      source=req.source, log_ctx=_ch_ctx,
                                      user_id=ch_user_id, reply_context=reply_context):
        etype = ev.get("type")
        if etype == "speech_chunk":
            chunks.append(ev)
        elif etype == "speech_end":
            full_text_parts.append(ev.get("full_text") or "")

    full_text = full_text_parts[-1] if full_text_parts else " ".join(c.get("text", "") for c in chunks)

    if full_text:
        _append_history(ch_user_id, "user", req.text)
        _append_history(ch_user_id, "assistant", full_text)
        asyncio.create_task(_store_memory(req.text, "user", user_id=ch_user_id))
        asyncio.create_task(_store_memory(full_text, "assistant", user_id=ch_user_id))

    await _broadcast_debug_state("idle")

    _ch_resp = {
        "user_text": req.text,
        "assistant_text": full_text,
        "source": req.source,
        "timestamp": time.time(),
        "chunks": [
            {"text": c.get("text"), "chunk_idx": c.get("chunk_idx")}
            for c in chunks
        ],
    }
    _get_user_chat_log(ch_user_id).append(_ch_resp)
    asyncio.create_task(_broadcast_chat_entry(_ch_resp))
    return JSONResponse(_ch_resp)


# ---------------------------------------------------------------------------
#  POST /voice
# ---------------------------------------------------------------------------

@app.post("/voice")
async def voice(request: Request, file: UploadFile = File(...), token: str = ""):
    user_id = _extract_user_id(request) or (jwt_decode(token).user_id if token else None)
    _touch_interaction()
    jid = _new_job_id()
    audio_bytes = await file.read()
    user_text = await _transcribe(audio_bytes, job_id=jid)

    if not user_text.strip():
        return JSONResponse({"error": "Could not transcribe audio", "text": ""})

    await _broadcast_debug_state("thinking", last_input=user_text)
    memories = await _query_memories(user_text, user_id=user_id)
    _voice_ctx = CallContext(triggered_by="voice", user_id=user_id,
                             conversation_id=str(jid))

    chunks: list[dict] = []
    first_emotion = "neutral"
    first_gesture = ""
    full_text = ""
    async for ev in _run_inline_turn(user_text, memories, job_id=jid,
                                      source="voice", log_ctx=_voice_ctx,
                                      user_id=user_id):
        etype = ev.get("type")
        if etype == "emotion" and not chunks:
            first_emotion = ev.get("id", "neutral")
        elif etype == "gesture" and not chunks:
            first_gesture = ev.get("name", "") or ""
        elif etype == "speech_chunk":
            chunks.append(ev)
        elif etype == "speech_end":
            full_text = ev.get("full_text") or " ".join(c.get("text", "") for c in chunks)

    if full_text:
        _append_history(user_id, "user", user_text)
        _append_history(user_id, "assistant", full_text)
        asyncio.create_task(_store_memory(user_text, "user", user_id=user_id))
        asyncio.create_task(_store_memory(full_text, "assistant", user_id=user_id))

    _voice_entry = {
        "user_text": user_text,
        "assistant_text": full_text,
        "source": "voice",
        "timestamp": time.time(),
        "chunks": [
            {"text": c.get("text"), "chunk_idx": c.get("chunk_idx")}
            for c in chunks
        ],
    }
    _get_user_chat_log(user_id).append(_voice_entry)
    asyncio.create_task(_broadcast_chat_entry(_voice_entry))
    await _broadcast_debug_state("idle")

    # Concatenate chunk audio into a single WAV for the /voice response.
    first_audio_b64 = next((c.get("audio_base64") for c in chunks if c.get("audio_base64")), None)
    if first_audio_b64:
        # For simplicity return the first chunk's audio as the primary response.
        # Callers that want all chunks should use /chat/stream or /ws/live.
        audio_bytes_out = base64.b64decode(first_audio_b64)
        return StreamingResponse(
            io.BytesIO(audio_bytes_out),
            media_type="audio/wav",
            headers={
                "X-User-Text": user_text,
                "X-Assistant-Text": full_text,
                "X-Emotion": first_emotion,
                "X-Gesture": first_gesture,
                "X-Chunk-Count": str(len(chunks)),
            },
        )

    return JSONResponse({
        "user_text": user_text,
        "assistant_text": full_text,
        "chunks": [
            {"text": c.get("text"), "chunk_idx": c.get("chunk_idx")}
            for c in chunks
        ],
    })


# ---------------------------------------------------------------------------
#  WebSocket helpers
# ---------------------------------------------------------------------------

async def _broadcast_clients(message: dict):
    if EVAL_ISOLATION.get():
        return
    dead = []
    data = json.dumps(message)
    if message.get("type") == "ui_command":
        log.info("[broadcast] ui_command action=%s  → %d client(s)",
                 message.get("action"), len(_ws_clients))
    for ws in _ws_clients:
        try:
            await ws.send_text(data)
        except Exception as exc:
            log.warning("[broadcast] send failed: %s", exc)
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


async def _cancel_optional_task(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _ws_text_turn(
    ws: WebSocket,
    text: str,
    *,
    interrupted: bool = False,
    job_id_ref: list | None = None,
    conv_ctrl: "ConversationController | None" = None,
    user_id: str | None = None,
) -> None:
    """Drive one live turn using the inline-tag pipeline.

    Consumes events from ``_run_inline_turn`` and forwards them to the
    WebSocket client as speech_segment / emotion / gesture / speech_end.
    """
    _touch_interaction(user_text=text)

    # Quota gate: anonymous users get a daily token cap (config.yaml → quota).
    # Registered users (and legacy non-web channels with no user_id) are
    # unlimited. On cap-hit, refuse in Mocha's voice + push the user toward
    # Settings → Account → Sign Up.
    if user_id:
        _acct = await _account_type_for(user_id)
        _allowed, _used, _cap = await _auth_quota.check_quota(user_id, _acct)
        if (not _allowed) and _cap is not None:
            _refusal = _auth_quota.quota_refusal_message()
            try:
                await ws.send_text(json.dumps({
                    "type": "quota_exceeded",
                    "message": _refusal,
                    "used": _used,
                    "cap": _cap,
                }))
            except Exception:
                pass
            # Reflect the cap event in conversation history so context stays
            # honest on the next turn (after upgrade or tomorrow's reset).
            _append_history(user_id, "user", text)
            _append_history(user_id, "assistant", _refusal)
            await _broadcast_debug_state("idle")
            return

    jid = _new_job_id()
    if job_id_ref is not None:
        job_id_ref[0] = jid
    _timeline_init(jid)

    mem_task = asyncio.create_task(_query_memories(text, user_id=user_id))
    await ws.send_text(json.dumps({"type": "interrupt"}))
    await _broadcast_debug_state("thinking", last_input=text)
    await _broadcast_timeline_event(jid, "llm", "start")

    memories = await mem_task
    if interrupted:
        memories = list(memories) if memories else []
        memories.insert(
            0,
            {
                "role": "system",
                "text": (
                    "Interruption: the user interrupted you mid-speech. "
                    "Start with ONE very short reaction sentence (1-4 words) "
                    "like 'Oh?' or 'Huh?' acknowledging the interruption, "
                    "then answer the new user message normally."
                ),
            },
        )

    chunks: list[dict] = []
    current_emotion = "neutral"
    current_gesture = ""
    full_text = ""
    was_cancelled = False
    _ctx = CallContext(triggered_by="ws_live", source="ws_live",
                       user_id=user_id, conversation_id=str(jid))

    try:
        async for ev in _run_inline_turn(text, memories, job_id=jid,
                                          source="ws_live", log_ctx=_ctx,
                                          user_id=user_id):
            etype = ev.get("type")
            if etype == "emotion":
                current_emotion = ev.get("id", "neutral")
            elif etype == "gesture":
                current_gesture = ev.get("name", "") or ""
            elif etype == "speech_chunk":
                chunks.append(ev)
                response = {
                    "type": "speech_segment",
                    "job_id": jid,
                    "index": ev.get("chunk_idx", 0),
                    "total": -1,
                    "text": ev.get("text", ""),
                    "emotion": current_emotion,
                    "gesture": current_gesture,
                }
                if ev.get("audio_base64"):
                    response["audio_base64"] = ev["audio_base64"]
                    if conv_ctrl is not None:
                        conv_ctrl.extend_mute(ev["audio_base64"])
                if ev.get("viseme_b64"):
                    response["viseme_b64"] = ev["viseme_b64"]
                    response["viseme_fps"] = ev.get("viseme_fps", 30)
                    response["viseme_frames"] = ev.get("viseme_frames", 0)
                await ws.send_text(json.dumps(response))
            elif etype == "viseme_update":
                # Late lip-sync upgrade; `index` mirrors speech_segment naming,
                # chunk_idx kept for symmetry with the SSE path.
                await ws.send_text(json.dumps({
                    "type": "viseme_update", "job_id": jid,
                    "index": ev.get("chunk_idx", 0),
                    "chunk_idx": ev.get("chunk_idx", 0),
                    "viseme_b64": ev.get("viseme_b64"),
                    "viseme_fps": ev.get("viseme_fps", 30),
                    "viseme_frames": ev.get("viseme_frames", 0),
                }))
            elif etype == "speech_end":
                full_text = ev.get("full_text") or ""
                if chunks:
                    await ws.send_text(json.dumps({
                        "type": "speech_end", "job_id": jid, "total": len(chunks),
                    }))
                    if conv_ctrl is not None:
                        conv_ctrl.mark_segments_sent()
    except asyncio.CancelledError:
        log.info("[live] Job %d: cancelled", jid)
        was_cancelled = True
    except Exception as exc:
        log.exception("[live] Job %d: error (%s)", jid, exc)
        was_cancelled = True
    finally:
        await _broadcast_timeline_event(jid, "llm", "end")

    if full_text and not was_cancelled:
        _append_history(user_id, "user", text)
        _append_history(user_id, "assistant", full_text)
        asyncio.create_task(_store_memory(text, "user", user_id=user_id))
        asyncio.create_task(_store_memory(full_text, "assistant", user_id=user_id))
        _live_entry = {
            "user_text": text,
            "assistant_text": full_text,
            "source": "live",
            "timestamp": time.time(),
            "chunks": [{"text": c.get("text"), "chunk_idx": c.get("chunk_idx")} for c in chunks],
        }
        _get_user_chat_log(user_id).append(_live_entry)
        asyncio.create_task(_broadcast_chat_entry(_live_entry))

    await _broadcast_debug_state("idle")


# ---------------------------------------------------------------------------
#  Whisper hallucination filter
# ---------------------------------------------------------------------------

_WHISPER_HALLUCINATIONS = [
    "subtitles by",
    "amara.org",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "like and subscribe",
    "translated by",
    "transcribed by",
    "copyright",
    "all rights reserved",
    "the following is a",
    "mosub",
    "viewer discretion",
    "subs by",
]


def _is_whisper_hallucination(text: str) -> bool:
    """Return True if *text* matches a known Whisper hallucination pattern."""
    lower = text.lower().strip()
    if len(lower) < 3:
        return True  # too short to be real speech
    for phrase in _WHISPER_HALLUCINATIONS:
        if phrase in lower:
            return True
    return False


# ---------------------------------------------------------------------------
#  ConversationController — state machine for /ws/live pipeline
# ---------------------------------------------------------------------------

class ConversationController:
    """Single-threaded conversation state machine for the live voice pipeline.

    Owns the conversation turn and coordinates VAD/STT → LLM → TTS → browser.
    Implements speculative LLM execution with cancellation: if the user keeps
    talking after a VAD final, the in-flight LLM call is cancelled and
    resubmitted with the combined text.

    States: IDLE → LISTENING → PROCESSING → SPEAKING → IDLE
    """

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"

    def __init__(self, ws: WebSocket, vad: VadUtteranceSegmenter, job_id_ref: list,
                 user_id: str | None = None):
        self.state = self.IDLE
        self._ws = ws
        self._vad = vad
        self._job_id_ref = job_id_ref
        self._user_id = user_id
        self._active_gen: Optional[asyncio.Task] = None
        self._gen_id: int = 0
        self._pending_text: str = ""      # accumulated text across finals
        self._mute_until: float = 0.0     # server-side mute deadline (monotonic)
        self._lock = asyncio.Lock()       # serialize all state transitions
        # Event-based mute tracking (driven by segment_play_start/end)
        self._playing_segments: set = set()    # (job_id, seg_idx) pairs currently playing
        self._all_segments_sent: bool = False  # True after speech_end sent for current job

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    async def on_vad_segment(self, tag: str, pcm: bytes) -> None:
        """Called from the WebSocket feed loop for each VAD emission."""
        async with self._lock:
            await self._handle_segment(tag, pcm)

    async def on_text_input(self, text: str) -> None:
        """Called when user sends typed text (bypasses mic/VAD)."""
        async with self._lock:
            await self._interrupt()
            self._pending_text = text
            await self._submit_to_llm(text)

    @staticmethod
    def _wav_duration_s(audio_b64: str) -> float:
        """Parse actual duration from WAV header instead of assuming 24 kHz."""
        raw = base64.b64decode(audio_b64[:200])  # header is in first ~80 bytes
        if len(raw) < 44 or raw[:4] != b"RIFF":
            return len(audio_b64) * 3 // 4 / (24000 * 2)  # fallback
        sample_rate = int.from_bytes(raw[24:28], "little")
        channels = int.from_bytes(raw[22:24], "little")
        bps = int.from_bytes(raw[34:36], "little")
        total_raw = len(audio_b64) * 3 // 4
        data_len = max(0, total_raw - 44)
        return data_len / max(1, sample_rate * channels * (bps // 8))

    def extend_mute(self, audio_b64: str, grace_s: float = 0.8) -> None:
        """Called by _send_seg when a speech_segment with audio is sent.

        Parses the actual WAV sample rate from the header and extends the
        server-side mute deadline so the VAD doesn't pick up TTS echo.
        Also sends mic_mute:true on first segment.
        """
        if not MUTE_MIC_WHILE_AGENT_TALKING:
            return
        was_speaking = (self.state == self.SPEAKING)
        duration_s = self._wav_duration_s(audio_b64)
        new_deadline = time.monotonic() + duration_s + grace_s
        self._mute_until = max(self._mute_until, new_deadline)
        self.state = self.SPEAKING
        if not was_speaking:
            self._all_segments_sent = False
            self._playing_segments.clear()
            asyncio.create_task(
                self._ws.send_text(json.dumps({"type": "mic_mute", "muted": True}))
            )

    def mark_segments_sent(self) -> None:
        """Called after speech_end is sent."""
        self._all_segments_sent = True

    async def on_playback_event(self, event_type: str, job_id: int, seg_idx: int) -> None:
        """Called when client reports segment_play_start or segment_play_end."""
        if not MUTE_MIC_WHILE_AGENT_TALKING:
            return
        if event_type == "segment_play_start":
            self._playing_segments.add((job_id, seg_idx))
        elif event_type == "segment_play_end":
            self._playing_segments.discard((job_id, seg_idx))
            if not self._playing_segments and self._all_segments_sent:
                self._mute_until = 0.0
                self.state = self.IDLE
                self._vad.flush()
                await self._ws.send_text(json.dumps({"type": "mic_mute", "muted": False}))
                await _broadcast_debug_state("idle")

    # ------------------------------------------------------------------ #
    #  Internal state transitions                                        #
    # ------------------------------------------------------------------ #

    async def _handle_segment(self, tag: str, pcm: bytes) -> None:
        # While SPEAKING, drop utterances if segments are playing or mute timer active.
        if self.state == self.SPEAKING:
            if self._playing_segments or time.monotonic() < self._mute_until:
                log.debug("ConvCtrl: utterance dropped (SPEAKING, mute active)")
                return
            # Mute fully expired and no segments playing — transition to IDLE
            self.state = self.IDLE
            self._vad.flush()
            await _broadcast_debug_state("idle")

        if tag == "interim":
            await self._handle_interim(pcm)
        else:
            await self._handle_final(pcm)

    async def _handle_interim(self, pcm: bytes) -> None:
        """Transcribe for visual feedback only — no LLM call."""
        if self.state == self.IDLE:
            self.state = self.LISTENING
        wav = pcm16_mono_to_wav(pcm, sample_rate=self._vad.sample_rate)
        text = await _transcribe(wav, job_id=_new_job_id())
        if text and text.strip() and not _is_whisper_hallucination(text.strip()):
            preview = (self._pending_text + " " + text.strip()).strip()
            await self._ws.send_text(json.dumps({"type": "stt_interim", "text": preview}))

    async def _handle_final(self, pcm: bytes) -> None:
        """Transcribe and decide: submit, resubmit (cancel+combine), or barge-in."""
        wav = pcm16_mono_to_wav(pcm, sample_rate=self._vad.sample_rate)
        await _broadcast_debug_state("listening")
        text = await _transcribe(wav, job_id=_new_job_id())
        if not text or not text.strip():
            if self.state == self.LISTENING:
                self.state = self.IDLE
                await _broadcast_debug_state("idle")
            return
        text = text.strip()

        if _is_whisper_hallucination(text):
            log.debug("ConvCtrl: dropping Whisper hallucination: %r", text)
            if self.state == self.LISTENING:
                self.state = self.IDLE
                await _broadcast_debug_state("idle")
            return

        log.info("ConvCtrl [%s]: final STT %r", self.state, text)

        if self.state == self.PROCESSING:
            # User kept talking while LLM was processing a partial sentence.
            # Cancel the speculative LLM call and resubmit with combined text.
            log.info("ConvCtrl: user still talking — cancel LLM and resubmit")
            if self._active_gen and not self._active_gen.done():
                self._active_gen.cancel()
            self._pending_text = (self._pending_text + " " + text).strip()
            await self._submit_to_llm(self._pending_text)
            return

        if self.state == self.SPEAKING:
            # Barge-in: user interrupts Mocha mid-speech
            log.info("ConvCtrl: barge-in detected")
            await self._interrupt()
            self._pending_text = text
            await self._submit_to_llm(text)
            return

        # IDLE or LISTENING: accumulate and submit (speculative execution)
        self._pending_text = (self._pending_text + " " + text).strip()
        await self._submit_to_llm(self._pending_text)

    async def _submit_to_llm(self, text: str) -> None:
        """Start a new LLM→TTS generation for the given text."""
        self.state = self.PROCESSING
        self._gen_id += 1
        gen_id = self._gen_id
        was_interrupted = False

        # If there's a stale generation somehow, cancel it
        if self._active_gen and not self._active_gen.done():
            self._active_gen.cancel()
            was_interrupted = True

        await self._ws.send_text(json.dumps({"type": "stt_result", "text": text}))

        self._active_gen = asyncio.create_task(
            self._run_generation(text, gen_id, was_interrupted)
        )

    async def _run_generation(self, text: str, gen_id: int, interrupted: bool) -> None:
        """Wrapper around _ws_text_turn that tracks state."""
        _was_cancelled = False
        try:
            await _ws_text_turn(
                self._ws, text,
                interrupted=interrupted,
                job_id_ref=self._job_id_ref,
                conv_ctrl=self,
                user_id=self._user_id,
            )
        except asyncio.CancelledError:
            _was_cancelled = True
            log.info("ConvCtrl: generation %d cancelled", gen_id)
        except Exception as exc:
            _was_cancelled = True
            log.error("ConvCtrl: generation %d failed: %s", gen_id, exc)
        finally:
            # Only clean up if we're still the active generation
            if self._gen_id == gen_id:
                if not _was_cancelled:
                    self._pending_text = ""
                # Don't reset to IDLE if segments are still playing
                if self.state in (self.SPEAKING, self.PROCESSING):
                    if not self._playing_segments and not (time.monotonic() < self._mute_until):
                        self.state = self.IDLE
                        await _broadcast_debug_state("idle")

    async def _interrupt(self) -> None:
        """Cancel current generation, flush VAD, reset mute, unmute mic."""
        if self._active_gen and not self._active_gen.done():
            self._active_gen.cancel()
        self._mute_until = 0.0
        self._playing_segments.clear()
        self._all_segments_sent = False
        self._vad.flush()  # Discard echo audio buffered during TTS playback
        await self._ws.send_text(json.dumps({"type": "interrupt"}))
        if MUTE_MIC_WHILE_AGENT_TALKING:
            await self._ws.send_text(json.dumps({"type": "mic_mute", "muted": False}))
        self.state = self.LISTENING


# ---------------------------------------------------------------------------
#  WS /ws/live — phone-call mode: continuous PCM + VAD utterances → STT → LLM
# ---------------------------------------------------------------------------

@app.websocket("/ws/live")
async def live_ws(ws: WebSocket):
    """Stream PCM16 mono 16 kHz in arbitrary-size binary frames (the web client
    sends 512-sample / 1024-byte ScriptProcessor buffers; the VAD segmenter
    rebuffers internally, so frame size is not a protocol contract).

    Server runs Silero VAD (with webrtcvad fallback), packages utterances on
    silence, runs Whisper per utterance, then the inline-tag pipeline.

    The ConversationController state machine serializes all processing and
    handles speculative LLM execution, barge-in, and echo suppression.
    """
    await ws.accept()
    if not _live_mode_enabled():
        await ws.close(code=4403)
        return
    _ws_user_id = _extract_user_id_ws(ws)
    _ws_clients.append(ws)
    # Set in client_hello once the browser tells us its persistent session_id.
    # Used by the single-active-Mocha presence registry.
    _ws_session_id: str | None = None
    log.info("Live client connected. Total: %d", len(_ws_clients))

    job_id_ref: list = [0]
    vad = VadUtteranceSegmenter(
        sample_rate=int(live_config.get("sample_rate", 16000)),
        frame_ms=int(live_config.get("frame_ms", 20)),
        aggressiveness=int(live_config.get("vad_aggressiveness", 2)),
        silence_ms_interim=_cfg_vad_interim_ms,
        silence_ms_final=_cfg_vad_final_ms,
        min_speech_ms=int(live_config.get("min_speech_ms", 200)),
        max_utterance_ms=int(live_config.get("max_utterance_ms", 15000)),
        silero_threshold=float(live_config.get("silero_threshold", 0.4)),
    )

    ctrl = ConversationController(ws, vad, job_id_ref, user_id=_ws_user_id)
    ws._vad_segmenter = vad  # expose for runtime config updates

    _uplink_idle_task: Optional[asyncio.Task] = None
    _uplink_lit = False
    _logged_first_pcm = False

    async def _notify_uplink_pcm(nbytes: int) -> None:
        """Monitor: light 'uplink' while client streams PCM on /ws/live."""
        nonlocal _uplink_lit, _uplink_idle_task, _logged_first_pcm
        if not _logged_first_pcm:
            _logged_first_pcm = True
            log.info("Live /ws/live: first PCM chunk received (%d bytes) — uplink active", nbytes)
        if not _uplink_lit:
            _uplink_lit = True
            await _monitor_thread_start("uplink", input_preview=f"{nbytes}B→srv", job_id=0)
        if _uplink_idle_task and not _uplink_idle_task.done():
            _uplink_idle_task.cancel()

        async def _uplink_idle():
            try:
                await asyncio.sleep(0.22)
                nonlocal _uplink_lit
                _uplink_lit = False
                await _monitor_thread_end("uplink", input_preview="")
            except asyncio.CancelledError:
                raise

        _uplink_idle_task = asyncio.create_task(_uplink_idle())

    try:
        while True:
            raw = await ws.receive()

            if raw.get("type") == "websocket.disconnect":
                break

            if "text" in raw:
                msg = json.loads(raw["text"])
                mtype = msg.get("type", "")

                if mtype == "user_input":
                    text = msg.get("text", "")
                    if text:
                        # Typing counts as activity → this device claims Mocha.
                        await _claim_active(_ws_user_id, _ws_session_id)
                        await ctrl.on_text_input(text)

                elif mtype == "claim_active":
                    # Explicit "bring Mocha here" from the frontend (e.g. user
                    # clicked the away pill on the inactive device).
                    await _claim_active(_ws_user_id, _ws_session_id)

                elif mtype in ("segment_play_start", "segment_play_end"):
                    seg_idx = msg.get("index", 0)
                    seg_total = msg.get("total", 0)
                    jid = int(msg.get("job_id") or 0) or job_id_ref[0]
                    act = "start" if mtype == "segment_play_start" else "end"
                    # Feed playback events to ConversationController for mute coordination
                    await ctrl.on_playback_event(mtype, jid, seg_idx)
                    asyncio.create_task(
                        _broadcast_timeline_event(
                            jid, "ws_play", act,
                            segment=seg_idx, total=seg_total,
                        )
                    )

                elif mtype == "video_play_result":
                    # The web player reports whether a video actually played, or
                    # whether every candidate was refused at play time. Resolves
                    # the Future the video_player tool is awaiting (see
                    # register_play_waiter) so "done" reflects real playback.
                    pid = (msg.get("play_id") or "").strip()
                    if pid:
                        _resolve_play_waiter(pid, {
                            "ok": bool(msg.get("ok")),
                            "video_id": msg.get("video_id"),
                            "title": msg.get("title"),
                        })

                elif mtype == "client_hello":
                    sid = (msg.get("session_id") or "").strip()
                    _ws_session_id = sid or None
                    is_only_session = _register_session(_ws_user_id, sid, ws)
                    log.info(
                        "client_hello uid=%s sid=%s only_session=%s active_users=%d total_sockets=%d",
                        (_ws_user_id[:8] if _ws_user_id else "?"),
                        (sid[:8] if sid else "?"),
                        is_only_session,
                        len(_active_session),
                        len(_user_sockets),
                    )
                    if is_only_session:
                        # First/only session for this user → auto-claim active.
                        await _claim_active(_ws_user_id, sid)
                    elif _ws_user_id and sid:
                        # Another session is already active; this one starts
                        # passive. Frontend will hide the 3D model + mute.
                        await _send_presence(_ws_user_id, sid, active=False)
                    asyncio.create_task(_handle_autonomy_hello(user_id=_ws_user_id))

                elif mtype == "user_context":
                    # Browser-reported tz/locale/coords. Debounced write to
                    # data/primary_user.json so Telegram/Discord fall back to
                    # the same cached value when no web client is connected.
                    try:
                        from bridge.channel_router import save_primary_user_context
                        save_primary_user_context(
                            tz=msg.get("tz"),
                            locale=msg.get("locale"),
                            lat=msg.get("lat"),
                            lng=msg.get("lng"),
                        )
                    except Exception as exc:
                        log.warning("user_context save failed: %s", exc)

                elif mtype == "user_active":
                    # Treat as an interaction: user is present and engaged.
                    _touch_interaction()

                elif mtype == "user_idle":
                    # Frontend reports the user hasn't touched keyboard/mouse in a while.
                    # We don't reset _last_interaction_time here; autonomy tick reads
                    # elapsed time directly. This event is informational (logged only).
                    pass

                elif mtype == "page_visible":
                    # Tab refocused; treat as a soft touch but don't trigger hello again.
                    _touch_interaction()

                elif mtype == "page_hidden":
                    # Honor the frontend decision; autonomy won't force speech
                    # on a hidden tab. Also poke the diary writer — tab hidden
                    # is a good "pause of the day" moment to capture.
                    try:
                        from bridge import diary_writer
                        asyncio.create_task(diary_writer.on_page_hidden())
                    except Exception as exc:
                        log.debug("diary on_page_hidden enqueue failed: %s", exc)

                elif mtype == "page_closing":
                    # Last-chance diary write when the user closes the tab.
                    try:
                        from bridge import diary_writer
                        asyncio.create_task(diary_writer.on_page_hidden())
                    except Exception as exc:
                        log.debug("diary page_closing enqueue failed: %s", exc)

                elif mtype == "mocha_mute":
                    duration = int(msg.get("duration_s", 600) or 600)
                    duration = max(30, min(duration, 7200))
                    _mocha_state["muted_until_monotonic"] = time.monotonic() + duration
                    log.info("mocha_mute for %ds", duration)

            elif "bytes" in raw:
                b = raw["bytes"]
                if not b:
                    continue
                await _notify_uplink_pcm(len(b))
                for tag, utt in vad.feed(b):
                    # Real speech detected on this device → it claims Mocha.
                    # (Cheap: _claim_active no-ops if we're already active.)
                    await _claim_active(_ws_user_id, _ws_session_id)
                    await ctrl.on_vad_segment(tag, utt)

    except (WebSocketDisconnect, RuntimeError):
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if _uplink_idle_task and not _uplink_idle_task.done():
            _uplink_idle_task.cancel()
        if _uplink_lit:
            try:
                await _monitor_thread_end("uplink", input_preview="")
            except Exception:
                pass
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        # Drop this socket from the presence registry. If it was the active
        # session, _active_session is cleared too; the next interaction from
        # any remaining session will claim Mocha automatically.
        _unregister_session(_ws_user_id, _ws_session_id)
        log.info("Live client disconnected. Total: %d", len(_ws_clients))


# ---------------------------------------------------------------------------
#  WS /ws/voice-stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/voice-stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
    vs_user_id = _extract_user_id_ws(ws)
    log.info("Voice stream connected.")

    try:
        while True:
            audio_bytes = await ws.receive_bytes()
            # VAD-ish behavior: don't create a job/timeline for silence.
            # We'll only allocate job_id after STT produces non-empty text.
            user_text = await _transcribe(audio_bytes, job_id=0)
            if not user_text.strip():
                await ws.send_json({"type": "silence"})
                continue

            jid = _new_job_id()
            _timeline_init(jid)

            await ws.send_json({"type": "user_text", "text": user_text})
            _touch_interaction(user_text=user_text)

            memories = await _query_memories(user_text, user_id=vs_user_id)
            await _broadcast_timeline_event(jid, "llm", "start")
            _vs_ctx = CallContext(triggered_by="ws_voice_stream", user_id=vs_user_id,
                                  conversation_id=str(jid))

            collected_chunks: list[dict] = []
            full_text = ""
            async for ev in _run_inline_turn(user_text, memories, job_id=jid,
                                              source="voice-stream", log_ctx=_vs_ctx,
                                              user_id=vs_user_id):
                etype = ev.get("type")
                if etype == "emotion":
                    await ws.send_json({"type": "emotion", "id": ev.get("id", "")})
                elif etype == "gesture":
                    await ws.send_json({"type": "gesture", "name": ev.get("name", "")})
                elif etype == "speech_chunk":
                    collected_chunks.append(ev)
                    await ws.send_json({
                        "type": "assistant_text",
                        "index": ev.get("chunk_idx", 0),
                        "text": ev.get("text", ""),
                    })
                    if ev.get("audio_base64"):
                        await ws.send_bytes(base64.b64decode(ev["audio_base64"]))
                elif etype == "speech_end":
                    full_text = ev.get("full_text") or ""
            await _broadcast_timeline_event(jid, "llm", "end")

            if full_text:
                _append_history(vs_user_id, "user", user_text)
                _append_history(vs_user_id, "assistant", full_text)
                asyncio.create_task(_store_memory(user_text, "user", user_id=vs_user_id))
                asyncio.create_task(_store_memory(full_text, "assistant", user_id=vs_user_id))
                _vs_entry = {
                    "user_text": user_text,
                    "assistant_text": full_text,
                    "source": "voice-stream",
                    "timestamp": time.time(),
                    "chunks": [
                        {"text": c.get("text"), "chunk_idx": c.get("chunk_idx")}
                        for c in collected_chunks
                    ],
                }
                _get_user_chat_log(vs_user_id).append(_vs_entry)
                asyncio.create_task(_broadcast_chat_entry(_vs_entry))

    except (WebSocketDisconnect, RuntimeError):
        log.info("Voice stream disconnected.")


# ---------------------------------------------------------------------------
#  WS /ws/monitor — real-time pipeline dashboard
# ---------------------------------------------------------------------------

@app.websocket("/ws/monitor")
async def monitor_ws(ws: WebSocket):
    await ws.accept()
    monitor_clients.append(ws)
    log.info("Monitor client connected. Total: %d", len(monitor_clients))

    # Send current thread states on connect
    for thread, state in _thread_states.items():
        await ws.send_text(json.dumps({
            "type": "thread_status",
            "thread": thread,
            "status": state["status"],
        }))
    # Send current echo mode so the dashboard toggle initialises correctly
    await ws.send_text(json.dumps({"type": "echo_mode", "mode": LIVE_ECHO_MODE}))
    # Send current config values for Config tab
    await ws.send_text(json.dumps({
        "type": "config_state",
        "vad_final_ms": _cfg_vad_final_ms,
        "vad_interim_ms": _cfg_vad_interim_ms,
        "llm_temperature": getattr(llm_client, "default_temperature", 0.8),
        "history_size": MAX_HISTORY,
    }))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            await _handle_monitor_command(msg)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if ws in monitor_clients:
            monitor_clients.remove(ws)
        log.info("Monitor client disconnected. Total: %d", len(monitor_clients))


def _persist_config(dotpath: str, value):
    """Write a single config value to config.yaml in-place, preserving comments/formatting."""
    import re as _re
    cfg_path = ROOT / "config.yaml"
    key = dotpath.rsplit(".", 1)[-1]
    # Format the replacement value
    if isinstance(value, bool):
        yaml_val = "true" if value else "false"
    elif isinstance(value, str):
        yaml_val = f'"{value}"' if value else '""'
    elif isinstance(value, float):
        yaml_val = str(value)
    else:
        yaml_val = str(value)
    text = cfg_path.read_text()
    # Match the key at any indentation, replace only the value portion
    pattern = _re.compile(rf'^(\s*{_re.escape(key)}\s*:\s*)(.+)$', _re.MULTILINE)
    new_text, count = pattern.subn(rf'\g<1>{yaml_val}', text, count=1)
    if count:
        cfg_path.write_text(new_text)
    else:
        log.warning("_persist_config: key %r not found in config.yaml", dotpath)


async def _handle_monitor_command(msg: dict) -> None:
    """Handle config commands sent from the dashboard monitor."""
    global LIVE_ECHO_MODE, MUTE_MIC_WHILE_AGENT_TALKING
    global _cfg_vad_final_ms, _cfg_vad_interim_ms
    global MAX_HISTORY

    if msg.get("type") != "config_update":
        return

    key = msg.get("key")
    value = msg.get("value")

    if key == "echo_mode" and value in ("room", "headphone"):
        LIVE_ECHO_MODE = value
        MUTE_MIC_WHILE_AGENT_TALKING = (value == "room")
        _persist_config("bridge.live.echo_mode", value)
        log.info("Echo mode changed via dashboard: %s (mute_mic=%s)", value, MUTE_MIC_WHILE_AGENT_TALKING)
        await _broadcast_clients({"type": "echo_mode", "mode": value})
        for mc in monitor_clients:
            try:
                await mc.send_text(json.dumps({"type": "echo_mode", "mode": value}))
            except Exception:
                pass

    elif key == "vad_final_ms":
        _cfg_vad_final_ms = int(value)
        _persist_config("bridge.live.silence_ms_final", _cfg_vad_final_ms)
        # Update any active segmenters
        for ws in list(_ws_clients):
            seg = getattr(ws, "_vad_segmenter", None)
            if seg and hasattr(seg, "update_timings"):
                seg.update_timings(silence_ms_final=_cfg_vad_final_ms)
        log.info("VAD final silence changed via dashboard: %dms", _cfg_vad_final_ms)

    elif key == "vad_interim_ms":
        _cfg_vad_interim_ms = int(value)
        _persist_config("bridge.live.silence_ms_interim", _cfg_vad_interim_ms)
        for ws in list(_ws_clients):
            seg = getattr(ws, "_vad_segmenter", None)
            if seg and hasattr(seg, "update_timings"):
                seg.update_timings(silence_ms_interim=_cfg_vad_interim_ms)
        log.info("VAD interim silence changed via dashboard: %dms", _cfg_vad_interim_ms)

    elif key == "llm_temperature":
        llm_client.default_temperature = float(value)
        _persist_config("llm.temperature", float(value))
        log.info("LLM temperature changed via dashboard: %.2f", llm_client.default_temperature)

    elif key in ("history_size", "pass2_history"):
        # pass2_history kept as alias for backward compat with the dashboard
        # frontend until that's renamed to history_size.
        MAX_HISTORY = max(1, int(value))
        _persist_config("memory.short_term_limit", MAX_HISTORY)
        log.info("History size changed via dashboard: %d", MAX_HISTORY)


@app.get("/monitor", include_in_schema=False)
async def monitor_page():
    html = STATIC_DIR / "monitor.html"
    if html.is_file():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(html.read_text())
    return JSONResponse({"error": "Monitor page not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config["host"], port=config["port"])
