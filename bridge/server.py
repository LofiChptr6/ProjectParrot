"""
Bridge / Orchestrator — Connects STT, LLM, TTS, Memory, and Unity.

Data flow:
  Mic -> [STT] -> text -> [Memory query + LLM] -> response -> [TTS] -> audio -> [Unity]
                                                            -> [Memory store]

Multi-segment pipeline:
  The LLM returns a "segments" array where each entry is one sentence with its
  own emotion and action.  The bridge resolves animations and synthesises TTS
  for ALL segments concurrently (asyncio.gather), then streams finished segments
  to Unity in order so playback starts immediately.

Barge-in:
  When the user sends new input while a response is still being processed, the
  bridge cancels the in-flight generation task and sends an "interrupt" message
  to Unity so it can stop playback.  The new input is then processed normally.

Debug state:
  The bridge broadcasts "debug_state" messages to Unity at each pipeline stage
  with timing info so the overlay can show what's happening.

Exposes:
  GET  /chat/stream          -- SSE streaming chat (dashboard + unified hub)
  POST /voice               -- audio in, text + audio out
  POST /channel             -- text channels (Telegram/Discord/CLI); optional tools
  WS   /ws/unity            -- WebSocket for Unity (push-to-talk binary WAV + text)
  WS   /ws/live             -- phone-call mode: stream PCM16; VAD → STT → LLM
  WS   /ws/voice-stream     -- WebSocket: chunked audio → STT → LLM (no Unity)
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
import random
import re
import shutil
import struct
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from animation.ingest import parse_actions_file, describe_clip
from .audio_utils import pcm16_mono_to_wav
from .llm_client import LLMClient
from .vad_segmenter import VadUtteranceSegmenter
from .call_log import CallContext
from . import call_log
from character.context import build_system_prompt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bridge")

ROOT = Path(__file__).resolve().parent.parent
full_config = yaml.safe_load((ROOT / "config.yaml").read_text())
config = full_config["bridge"]
llm_config = full_config["llm"]
anim_config = full_config.get("animation", {})
live_config = config.get("live", {})

# Runtime-adjustable config (updated via dashboard Config tab)
_cfg_vad_final_ms = int(live_config.get("silence_ms_final", 900))
_cfg_vad_interim_ms = int(live_config.get("silence_ms_interim", 350))

# ---------------------------------------------------------------------------
#  Network addresses — derived from the central "network" block so every URL
#  stays consistent when you move from LAN to WAN or back.
# ---------------------------------------------------------------------------
_net = full_config.get("network", {})
INTERNAL_HOST = _net.get("internal_host", "127.0.0.1")
EXTERNAL_HOST = _net.get("external_host", INTERNAL_HOST)

def _resolve_url(cfg_key: str, service_section: str, default_port: int) -> str:
    """Return explicit bridge.<cfg_key> if set, else derive from internal_host."""
    explicit = config.get(cfg_key)
    if explicit:
        return explicit.rstrip("/")
    port = full_config.get(service_section, {}).get("port", default_port)
    return f"http://{INTERNAL_HOST}:{port}"

config["stt_url"]       = _resolve_url("stt_url",       "stt",       8001)
config["tts_url"]       = _resolve_url("tts_url",       "tts",       8002)
config["memory_url"]    = _resolve_url("memory_url",    "memory",    8003)
config["animation_url"] = _resolve_url("animation_url", "animation", 8004)
# gesture_url removed — FBX function-based animation replaces EMAGE gesture service

# LLM client (OpenAI-compatible API — vLLM, etc.)
llm_client = LLMClient(llm_config)
# Expose URL for logging / health display
config["llm_url"] = llm_client._base_url.rsplit("/v1", 1)[0]

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

app = FastAPI(title="Parrot Bridge")
log.info("bridge.live enabled=%s (WS /ws/live)", _live_mode_enabled())
log.info(
    "Network: internal=%s  external=%s  bridge=%s",
    INTERNAL_HOST, EXTERNAL_HOST, BRIDGE_INTERNAL_URL,
)
log.info(
    "Service URLs: stt=%s  tts=%s  memory=%s  animation=%s  llm=%s",
    config["stt_url"], config["tts_url"], config["memory_url"],
    config["animation_url"], config["llm_url"],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

http = httpx.AsyncClient(timeout=120.0)
unity_clients: list[WebSocket] = []
monitor_clients: list[WebSocket] = []
conversation_history: list[dict] = []
_chat_log: list[dict] = []
MAX_HISTORY = full_config["memory"].get("short_term_limit", 20)

_cr_config = llm_config.get("complexity_routing", {})
COMPLEXITY_ROUTING_ENABLED: bool = bool(_cr_config.get("enabled", False))
COMPLEXITY_SHORT_HISTORY: int    = int(_cr_config.get("short_history", 4))
PASS1_TOOLS: bool                = bool(_cr_config.get("pass1_tools", True))

LLM_FAILURE_MESSAGE = "Sorry, I'm having trouble thinking right now."
_LLM_FALLBACK = {"text": LLM_FAILURE_MESSAGE, "emotion": "sad", "action": None}

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


# Fallback stalling phrases — fired ONLY when the LLM returns a tool call
# with no accompanying speech segment. Chosen by category using cheap keyword
# matching over the user message + the first tool name, so the cue sounds
# like Mocha reacted to the actual request rather than reading a stock line.
_STALL_DESIGN = [
    "Ooh, designing.",
    "Alright, let me sketch that.",
    "Okay, pulling colors together.",
    "Give me a sec to vibe it out.",
    "Cooking something up.",
    "Let me see what fits.",
]
_STALL_RESEARCH = [
    "One sec, checking.",
    "Ooh, let me pull that up.",
    "Hmm, looking into it.",
    "Gimme a moment, digging.",
    "Asking Nori.",
    "On it — checking now.",
]
_STALL_SCHEDULE = [
    "Setting that up.",
    "One sec, wiring the schedule.",
    "Okay, putting that on the cron.",
]
_STALL_GENERIC = [
    "One sec.",
    "Hmm, let me check.",
    "On it.",
    "Hang on, looking.",
    "Okay, digging in.",
    "Ooh, let me grab that.",
]

_STALL_THEME_KWS = ("theme", "color", "colour", "style", "vibe", "redesign",
                     "palette", "mood", "background", "restyle")


# Safety net: if Mocha produces speech like "one sec" / "let me find that" with
# NO tool_call attached, auto-inject an ask_nori call instead of letting the
# promise dangle. Both streaming paths share this heuristic.
_STALL_PATTERNS = (
    "check", "look into", "look it up", "looking into", "give me a",
    "one moment", "hold on", "hang on", "pull up", "pulling up",
    "find out", "find that", "find it", "let me find", "let me pull",
    "let me check", "let me grab", "let me look", "let me get",
    "let me see", "let me know", "one sec", "just a sec", "just a moment",
    "gimme a sec", "search", "dig into", "digging",
)
_SKIP_PATTERNS = (
    "hana", "theme", "palette", "preview", "critique",
    "screenshot", "propose", "restyle", "revert",
)
_RESEARCH_MARKERS = (
    "?", "what ", "when ", "how ", "where ", "who ", "which ",
    "tell me", "show me", "find ", "search ", "look up", "look into",
)
# Content nouns — user message contains one of these, we auto-fire ask_nori
# even without a question mark. "cowboy bebop ost" isn't researchy by the
# marker list but "ost" nails it as a music-fetch request.
_CONTENT_MARKERS = (
    # Music / audio
    "ost", "soundtrack", "song", "album", "track", "music",
    "lofi", "lo-fi", "jazz", "classical", "ambient", "synthwave",
    "remix", "cover", "loop", "playlist",
    # Video
    "video", "clip", "movie", "anime", "episode", "trailer", "watch",
    # Action verb — "play X", "put on X" are always content-fetch asks
    "play", "put on", "queue",
    # Data
    "stock", "price", "quote", "news", "headline", "weather",
    "forecast", "stream", "radio", "podcast",
)
_AFFIRMATIONS = {
    "yeah", "yes", "yup", "yep", "ok", "okay", "sure", "cool",
    "nice", "great", "thanks", "thank you", "no", "nope", "nah",
    "not really",
}


def _is_short_affirmation(ut: str, ut_low: str) -> bool:
    if len(ut) >= 24:
        return False
    return any(
        ut_low == w or ut_low.startswith(w + " ")
        or ut_low.startswith(w + "!") or ut_low.startswith(w + ",")
        or ut_low.startswith(w + ".")
        for w in _AFFIRMATIONS
    )


def _should_auto_route_to_nori(user_text: str, mocha_text: str) -> tuple[bool, str]:
    """Decide whether to synthesize an ask_nori tool call when the LLM
    produced speech but no tool call. Returns (fire, reason-for-logging).
    """
    ut = (user_text or "").strip()
    ut_low = ut.lower()
    mt_low = (mocha_text or "").lower()

    mocha_stalled = any(p in mt_low for p in _STALL_PATTERNS)
    if not mocha_stalled:
        return False, "no-stall-pattern"
    if _is_short_affirmation(ut, ut_low):
        return False, "user-said-affirmation"
    if any(p in mt_low for p in _SKIP_PATTERNS):
        return False, "mocha-pointed-elsewhere"

    # Researchy = explicit question form OR find/show/tell directive.
    is_researchy = len(ut) >= 20 and any(m in ut_low for m in _RESEARCH_MARKERS)
    # Content request = short-form noun ask ("cowboy bebop ost", "lofi music").
    is_content = any(m in ut_low.split() or m in ut_low for m in _CONTENT_MARKERS)
    if not (is_researchy or is_content):
        return False, "not-researchy-or-content"

    return True, "researchy" if is_researchy else "content-noun"


def _pick_stall_phrase(user_msg: str, tool_name: str) -> str:
    """Pick a contextual stall based on intent (user message) + the tool the
    LLM is about to call. Never uses the same canned "Let me look into that"
    line every time.
    """
    u = (user_msg or "").lower()
    t = (tool_name or "").lower()
    if t.startswith("theme_") or t == "ask_hana" or any(k in u for k in _STALL_THEME_KWS):
        return random.choice(_STALL_DESIGN)
    if t in ("schedule_cron", "cancel_cron_job", "list_cron_jobs"):
        return random.choice(_STALL_SCHEDULE)
    if t == "ask_nori" or any(k in u for k in ("stock", "news", "weather", "price", "find", "look up", "search", "what's")):
        return random.choice(_STALL_RESEARCH)
    return random.choice(_STALL_GENERIC)


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
    # Record tail of the user message so autonomy can riff on the last topic.
    if user_text:
        topic = user_text.strip()
        if topic:
            _mocha_state["last_topic_summary"] = topic[-120:]


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

        if not unity_clients:
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
        await _broadcast_to_unity(msg)

        jitter = IDLE_INTERVAL * 0.3
        wait = IDLE_INTERVAL + random.uniform(-jitter, jitter)
        await asyncio.sleep(max(wait, 5))


async def _handle_autonomy_hello():
    """Invoke the autonomy engine's reconnect-hello composer (fail-silent)."""
    try:
        from autonomy.engine import handle_client_hello
        await handle_client_hello()
    except Exception as exc:
        log.warning("autonomy hello failed: %s", exc)


# ---------------------------------------------------------------------------
#  Silent thinking gesture — broadcast during tool execution
# ---------------------------------------------------------------------------

async def _broadcast_thinking_gesture() -> None:
    """Send a silent idle_action with a thinking gesture to Unity.

    Called between tool rounds (after the first) so the character keeps
    animating while searches/tools are running — no speech, no TTS.
    """
    if not unity_clients:
        return
    if ANIMATION_MODE == "fbx_functions":
        # Use a known function name directly — no vector DB lookup needed
        fn = random.choice(["idle_waiting", "idle_look_around", "idle_breathe"])
        msg = {
            "type": "idle_action",
            "emotion": "thinking",
            "action": fn,
            "gesture": fn,
        }
    else:
        thinking_phrases = [
            "think carefully while looking to the side",
            "tap chin thoughtfully",
            "look up and think",
            "furrow brow and consider",
        ]
        action = random.choice(thinking_phrases)
        clip_name = await _resolve_action(action, "thinking")
        msg = {
            "type": "idle_action",
            "emotion": "thinking",
            "action": action,
            "gesture": clip_name or "",
        }
    await _broadcast_to_unity(msg)


# ---------------------------------------------------------------------------
#  Helpers
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


def _llm_reply_ok(reply: str) -> bool:
    return reply != LLM_FAILURE_MESSAGE


def _normalize_segment(seg: dict) -> dict:
    seg.setdefault("emotion", "neutral")
    action = seg.pop("action", None) or seg.pop("gesture", None)
    seg["action"] = action
    seg.pop("gesture", None)
    return seg


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(t) if p.strip()]
    return parts or [t]


def _enforce_one_sentence_per_segment(segments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for seg in segments:
        sentences = _split_sentences(seg.get("text", ""))
        if len(sentences) <= 1:
            out.append(seg)
            continue
        for s in sentences:
            out.append({**seg, "text": s})
    return out


async def _llm_repair_segments(user_text: str, draft_segments: list[dict]) -> list[dict]:
    draft_text = _segments_full_text(draft_segments)
    repair_system = (
        "You are repairing an assistant reply into JSON for a 3D character.\n"
        "Return VALID JSON ONLY. No markdown, no code fences, no extra text.\n"
        'Output format:\n'
        '{"segments": [{"text": "one sentence", "emotion": "emotion_id", "action": "physical action"}, ...]}\n'
        "\n"
        "Rules:\n"
        "- Each segment.text is exactly ONE spoken sentence; casual, natural wording.\n"
        "- End each segment with . ? or ! so TTS and transcripts read clearly.\n"
        "- Split at real sentence boundaries; use as many segments as needed — no padding.\n"
        "- Choose emotion and action per sentence; vary them naturally across segments.\n"
        "- Preserve meaning and order; do not add new facts.\n"
    )
    repair_user = (
        f"User message:\n{user_text}\n\n"
        f"Assistant draft to repair:\n{draft_text}\n"
    )

    try:
        messages = [
            {"role": "system", "content": repair_system},
            {"role": "user", "content": repair_user},
        ]
        _t0 = time.monotonic()
        result = await llm_client.chat(
            messages, temperature=0.3,
            max_tokens=min(2048, llm_config.get("max_tokens", 4096)),
        )
        _lat = (time.monotonic() - _t0) * 1000
        _usage = result.get("usage", {})
        asyncio.create_task(call_log.log_call(
            CallContext(triggered_by="repair"),
            model=llm_client.model, temperature=0.3,
            max_tokens=min(2048, llm_config.get("max_tokens", 4096)),
            stream=False, tools_provided=False, messages=messages,
            response_content=result.get("content"),
            response_tool_calls=result.get("tool_calls"),
            finish_reason=result.get("finish_reason"),
            error=result.get("_error"), latency_ms=_lat,
            prompt_tokens=_usage.get("prompt_tokens"),
            completion_tokens=_usage.get("completion_tokens"),
            total_tokens=_usage.get("total_tokens"),
        ))
        content = result.get("content") or ""
        repaired = _parse_llm_response(content)
        repaired = _enforce_one_sentence_per_segment(repaired)
        return repaired or draft_segments
    except Exception:
        return draft_segments


async def _prepend_segment(seg: dict, gen):
    """Async generator that prepends an already-peeked segment back into a stream."""
    yield {"_stream": "segment", "segment": seg}
    async for item in gen:
        yield item


# ---------------------------------------------------------------------------
#  Unified routing — LLM-driven two-pass routing
# ---------------------------------------------------------------------------

async def _unified_route(
    user_text: str,
    memories: list[dict],
    job_id: int = 0,
    log_ctx: CallContext | None = None,
):
    """Unified routing: Pass 1 with short context → LLM decides → route.

    Yields the same stream events as ``_llm_chat_stream()``:
      - ``{"_stream": "segment", "segment": {...}}``
      - ``{"_stream": "tool_calls", "tool_calls": [...], "message": {...}}``
      - ``{"_stream": "text", "text": "..."}``

    Routing is entirely LLM-driven:
      Option A (fast): LLM answers directly → yield segments (1 LLM call)
      Option B (tools): LLM returns stalling segment + tool_calls →
          yield stalling segment first (for TTS), then yield tool_calls
      Option C (complex): LLM returns segment with needs_context=True →
          yield stalling segment, then start Pass 2 with full history
    """
    stream_tools = TOOL_SCHEMAS if TOOLS_ENABLED else None
    _base_ctx = log_ctx or CallContext(triggered_by="chat_stream", conversation_id=str(job_id))

    if not COMPLEXITY_ROUTING_ENABLED:
        # Single-pass mode: full history, no routing instructions.
        # Let the model decide whether to think (enable_thinking=None).
        async for item in _llm_chat_stream(
            user_text, memories, job_id=job_id,
            history_limit=MAX_HISTORY,
            unified_routing=False,
            tools=stream_tools,
            log_ctx=_base_ctx,
        ):
            yield item
        return

    # --- Two-pass mode ---
    # Pass 1: short history + routing instructions, optional tool schemas, NO thinking.
    # Thinking is disabled (enable_thinking=False) so Pass 1 is fast — the model
    # either answers directly, calls tools immediately (if pass1_tools), or signals
    # needs_context for escalation to Pass 2.
    _p1_tools = stream_tools if PASS1_TOOLS else None
    _p1_ctx = dataclasses.replace(_base_ctx, pass_number=1)
    gen1 = _llm_chat_stream(
        user_text, memories, job_id=job_id,
        history_limit=COMPLEXITY_SHORT_HISTORY,
        unified_routing=True,
        tools=_p1_tools,
        max_tokens=512,
        enable_thinking=False,
        log_ctx=_p1_ctx,
    )

    # Collect segments from Pass 1 until stream ends
    collected_segments: list[dict] = []

    async for item in gen1:
        st = item.get("_stream")

        # Pass 1 has thinking disabled, but Pass 2 thinking deltas flow
        # through the outer caller (_llm_chat_stream → this generator → /chat/stream).
        # In case thinking leaks anyway, pass it through.
        if st in ("thinking_delta", "thinking_done"):
            yield item
            continue

        if st == "segment":
            collected_segments.append(item["segment"])
            continue

        # tool_calls: flush any collected stalling segments first (so they
        # get TTS'd and sent to client BEFORE the tool loop starts), then
        # yield the tool_calls event.
        if st == "tool_calls":
            for seg in collected_segments:
                seg["_is_thinking"] = True
                yield {"_stream": "segment", "segment": seg}
            collected_segments.clear()
            yield item
            continue

        # "text", "tool_status", and other events — pass through
        if st in ("text", "tool_status"):
            yield item
            continue

    # --- Route based on what Pass 1 produced ---

    # Check if any collected segment requests escalation (tools or complex)
    needs_context = any(s.get("needs_context") for s in collected_segments)

    if needs_context:
        # Option B/C: yield stalling segments for TTS, then run Pass 2
        # with full history + tool schemas
        for seg in collected_segments:
            seg["_is_thinking"] = True
            yield {"_stream": "segment", "segment": seg}

        log.info("[routing] Job %d: needs_context — escalating to Pass 2 (thinking enabled)", job_id)
        _p2_ctx = dataclasses.replace(_base_ctx, pass_number=2)
        async for item in _llm_chat_stream(
            user_text, memories, job_id=job_id,
            history_limit=MAX_HISTORY,
            unified_routing=False,
            tools=stream_tools,
            enable_thinking=True,
            log_ctx=_p2_ctx,
        ):
            yield item
        return

    # Option A: fast path — yield all collected segments directly
    for seg in collected_segments:
        yield {"_stream": "segment", "segment": seg}


# ---------------------------------------------------------------------------
#  Tool-calling support — helpers for ReAct loop inside /chat/stream
# ---------------------------------------------------------------------------
_tools_cfg = full_config.get("tools", {})
TOOLS_ENABLED: bool = bool(_tools_cfg.get("enabled", False))
_TOOL_MAX_ROUNDS: int = int(_tools_cfg.get("max_rounds", 5))
_TOOL_TIMEOUT: int = int(_tools_cfg.get("timeout", 30))

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


async def _llm_tool_round(messages: list[dict], job_id: int = 0,
                          log_ctx: CallContext | None = None) -> dict:
    """Single non-streaming LLM call with ``tools`` param.  Returns normalised dict."""
    _t0 = time.monotonic()
    result = await llm_client.chat(messages, tools=TOOL_SCHEMAS, enable_thinking=False)
    _lat = (time.monotonic() - _t0) * 1000
    _usage = result.get("usage", {})
    ctx = log_ctx or CallContext(triggered_by="tool_round", conversation_id=str(job_id))
    asyncio.create_task(call_log.log_call(
        ctx, model=llm_client.model,
        temperature=llm_client.default_temperature,
        max_tokens=llm_client.default_max_tokens,
        stream=False, enable_thinking=False, tools_provided=True,
        messages=messages,
        response_content=result.get("content"),
        response_tool_calls=result.get("tool_calls"),
        finish_reason=result.get("finish_reason"),
        error=result.get("_error"), latency_ms=_lat,
        prompt_tokens=_usage.get("prompt_tokens"),
        completion_tokens=_usage.get("completion_tokens"),
        total_tokens=_usage.get("total_tokens"),
    ))
    return result


async def normalize_llm_segments(
    user_text: str,
    segments: list[dict],
    *,
    job_id: int = 0,
) -> list[dict]:
    """Shared post-LLM pipeline: repair/split, one sentence per segment, echo drop, action fallback.

    Used by ``_llm_chat`` and by Unity streaming (per completed JSON segment object)
    so Chat and WebSocket share the same TTS-sized units.
    """
    _ = job_id  # reserved for future metrics
    if not segments:
        return [dict(_LLM_FALLBACK)]
    segments = [_normalize_segment(dict(s)) for s in segments if s.get("text")]
    if not segments:
        return [dict(_LLM_FALLBACK)]

    # Only invoke the expensive repair LLM when JSON was truly malformed
    # (raw-text fallback from _parse_llm_response).  For normal valid-JSON
    # segments the fast regex splitter is sufficient.
    stall_prefix: list[dict] = []
    if any(s.get("_raw_fallback") for s in segments):
        log.info("normalize: malformed JSON detected — running repair LLM")
        stall_prefix = [{
            "text": "Hmm, one sec.",
            "emotion": "thinking",
            "action": "look up and tap chin thoughtfully",
            "_is_thinking": True,
        }]
        segments = await _llm_repair_segments(user_text, segments)

    segments = _enforce_one_sentence_per_segment(segments)
    segments = _drop_echo_segments(user_text, segments)
    if not segments:
        return stall_prefix or [dict(_LLM_FALLBACK)]
    for seg in segments:
        if not seg.get("action"):
            seg["action"] = _fallback_action(user_text, seg.get("emotion", "neutral"))
        seg.pop("_raw_fallback", None)
    return stall_prefix + segments


def _parse_llm_response(raw: str) -> list[dict]:
    raw = raw.strip()
    # Strip <think>...</think> blocks (Qwen3/DeepSeek reasoning)
    close_idx = raw.find("</think>")
    if close_idx >= 0:
        raw = raw[close_idx + len("</think>"):].strip()
    elif raw.startswith("<think>"):
        # Orphan <think> with no closing tag — model hit max_tokens mid-thought.
        # Never speak reasoning text; return empty and let the caller stay silent.
        log.warning("_parse_llm_response: orphan <think> (no close tag) — dropping (len=%d)", len(raw))
        return []
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)

        if isinstance(parsed, dict) and "segments" in parsed:
            segments = parsed["segments"]
            if isinstance(segments, list) and segments:
                return [_normalize_segment(s) for s in segments if s.get("text")]

        if isinstance(parsed, dict) and "text" in parsed:
            return [_normalize_segment(parsed)]

    except (json.JSONDecodeError, TypeError):
        pass

    return [{"text": raw, "emotion": "neutral", "action": None, "_raw_fallback": True}]


class _StreamingSegmentParser:
    """Parse ``<think>...</think>`` + ``{"segments": [{...}, ...]}`` from a token stream.

    Emits (in order):

    - ``{"kind": "thinking", "content": "..."}`` when a ``<think>`` block closes.
    - ``{"kind": "text", "text": "..."}`` when a segment's ``"text"`` string is complete.
    - ``{"kind": "segment", "segment": {...}}`` when a segment object closes.

    :meth:`feed` returns only **new** events since the last call (buffer is rescanned
    deterministically; an internal cursor deduplicates).
    """

    _THINK_OPEN = "<think>"
    _THINK_CLOSE = "</think>"

    def __init__(self):
        self._buf = ""
        self._in_array = False
        self._array_start = -1
        self._emitted = 0
        self._saw_segment_event = False
        self._thinking_done = False
        self._thinking_emitted = 0  # chars of thinking content already sent as deltas

    def feed(self, chunk: str) -> list[dict]:
        self._buf += chunk
        events: list[dict] = []

        # --- Incremental thinking deltas (DeepSeek-style live streaming) ---
        if not self._thinking_done:
            events.extend(self._emit_thinking_deltas())

        # --- Segment parsing (only meaningful after thinking is done) ---
        all_ev = self._events_from_buffer()
        new = all_ev[self._emitted :]
        self._emitted = len(all_ev)
        for e in new:
            if e.get("kind") == "segment":
                self._saw_segment_event = True
        events.extend(new)
        return events

    def _emit_thinking_deltas(self) -> list[dict]:
        """Emit new thinking tokens as ``thinking_delta`` events.

        Returns ``thinking_done`` when ``</think>`` is found, and sets
        ``_thinking_done = True`` so segment parsing can begin.
        """
        buf = self._buf
        open_idx = buf.find(self._THINK_OPEN)
        if open_idx < 0:
            return []

        content_start = open_idx + len(self._THINK_OPEN)
        close_idx = buf.find(self._THINK_CLOSE)
        events: list[dict] = []

        if close_idx >= 0:
            # Full block found — emit remaining content + done
            full_content = buf[content_start:close_idx]
            if len(full_content) > self._thinking_emitted:
                events.append({"kind": "thinking_delta", "content": full_content[self._thinking_emitted:]})
            events.append({"kind": "thinking_done"})
            self._thinking_done = True
        else:
            # Still streaming inside <think> — emit new tokens
            current = buf[content_start:]
            if len(current) > self._thinking_emitted:
                events.append({"kind": "thinking_delta", "content": current[self._thinking_emitted:]})
                self._thinking_emitted = len(current)

        return events

    def finish(self) -> list[dict]:
        if not self._saw_segment_event:
            # Strip thinking block before fallback parse
            buf = self._buf
            close = buf.find(self._THINK_CLOSE)
            if close >= 0:
                buf = buf[close + len(self._THINK_CLOSE):].strip()
            return _parse_llm_response(buf)
        return []

    @staticmethod
    def _text_field_value_if_complete(buf: str, obj_open: int) -> Optional[str]:
        if obj_open < 0 or obj_open >= len(buf):
            return None
        key = buf.find('"text"', obj_open)
        if key < 0:
            return None
        colon = buf.find(":", key + 6)
        if colon < 0:
            return None
        i = colon + 1
        while i < len(buf) and buf[i] in " \t\n\r":
            i += 1
        if i >= len(buf) or buf[i] != '"':
            return None
        i += 1
        out: list[str] = []
        while i < len(buf):
            c = buf[i]
            if c == "\\":
                if i + 1 >= len(buf):
                    return None
                out.append(c)
                out.append(buf[i + 1])
                i += 2
                continue
            if c == '"':
                return "".join(out)
            out.append(c)
            i += 1
        return None

    def _events_from_buffer(self) -> list[dict]:
        buf = self._buf
        events: list[dict] = []

        # Thinking deltas are handled by _emit_thinking_deltas() in feed().
        # Don't parse segments until thinking block is closed.
        if not self._thinking_done:
            return events

        if not self._in_array:
            idx = buf.find('"segments"')
            if idx < 0:
                return events
            bracket = buf.find("[", idx + 10)
            if bracket < 0:
                return events
            self._in_array = True
            self._array_start = bracket + 1

        pos = self._array_start
        depth = 0
        obj_start = -1
        in_str = False
        esc = False
        text_emitted_for_obj = False

        while pos < len(buf):
            c = buf[pos]
            if esc:
                esc = False
                pos += 1
                continue
            if in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                pos += 1
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                if depth == 0:
                    obj_start = pos
                    text_emitted_for_obj = False
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    try:
                        obj = json.loads(buf[obj_start : pos + 1])
                        if obj.get("text"):
                            events.append(
                                {"kind": "segment", "segment": _normalize_segment(obj)}
                            )
                    except json.JSONDecodeError:
                        pass
                    obj_start = -1
                    text_emitted_for_obj = False
            elif c == "]" and depth == 0:
                break
            else:
                if depth == 1 and obj_start >= 0 and not text_emitted_for_obj:
                    txt = self._text_field_value_if_complete(buf, obj_start)
                    if txt is not None:
                        events.append({"kind": "text", "text": txt})
                        text_emitted_for_obj = True
            pos += 1

        if depth == 1 and obj_start >= 0 and not text_emitted_for_obj:
            txt = self._text_field_value_if_complete(buf, obj_start)
            if txt is not None:
                events.append({"kind": "text", "text": txt})
                text_emitted_for_obj = True

        return events


class ChannelRequest(BaseModel):
    text: str
    user_id: str = "unknown"
    source: str = "unknown"


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
    await _broadcast_to_unity(msg)


# ---------------------------------------------------------------------------
#  Service calls
# ---------------------------------------------------------------------------

async def _query_memories(text: str) -> list[dict]:
    try:
        resp = await http.post(
            f"{config['memory_url']}/query",
            json={"text": text, "n_results": 5},
        )
        if resp.status_code == 200:
            return resp.json().get("memories", [])
    except Exception as e:
        log.warning(f"Memory query failed: {e}")
    return []


async def _store_memory(text: str, role: str):
    try:
        await http.post(
            f"{config['memory_url']}/store",
            json={"text": text, "role": role},
        )
    except Exception as e:
        log.warning(f"Memory store failed: {e}")


async def _resolve_action(action_text: str, emotion: str = "") -> str | None:
    if not action_text:
        return None

    # FBX function mode: validate against known function names, no vector DB
    if ANIMATION_MODE == "fbx_functions":
        if action_text in _FBX_FUNCTION_NAMES:
            log.info("Animation (fbx_functions): '%s' -- valid function", action_text)
            return action_text
        log.debug("Animation (fbx_functions): '%s' -- not a valid function name", action_text)
        return None

    if ANIMATION_MODE == "llm_select" and action_text in _ANIMATION_CLIP_NAMES:
        log.info("Animation (llm_select): '%s' -- direct match", action_text)
        return action_text

    query = f"{action_text} {emotion}".strip()
    try:
        for conversational_only in (True, False):
            resp = await http.post(
                f"{config['animation_url']}/query",
                json={"text": query, "n_results": 1, "conversational_only": conversational_only},
                timeout=5.0,
            )
            if resp.status_code != 200:
                continue
            clips = resp.json().get("clips", [])
            if clips:
                clip = clips[0]
                log.info(
                    "Animation resolved: '%s' -> %s (score=%.3f, conversational_only=%s)",
                    query, clip["clip"], clip["score"], conversational_only,
                )
                return clip["clip"]
    except Exception as e:
        log.warning(f"Animation query failed: {e}")
    return None


# Fixed bone order for motion binary encoding (must match Unity side).
_MOTION_BONES = [
    "Spine", "Chest", "UpperChest", "Neck",
    "LeftShoulder", "RightShoulder", "Head",
    "LeftUpperArm", "RightUpperArm",
    "LeftLowerArm", "RightLowerArm",
    "LeftHand", "RightHand",
]
_MOTION_BONE_COUNT = len(_MOTION_BONES)  # 13


def _pack_motion_b64(vrm_bones: list[dict]) -> str:
    """Pack per-frame VRM bone quaternions into base64 binary.

    Format: T frames × 13 bones × 4 floats (x,y,z,w), float32 little-endian.
    """
    buf = bytearray()
    identity = (0.0, 0.0, 0.0, 1.0)
    for frame in vrm_bones:
        for bone_name in _MOTION_BONES:
            quat = frame.get(bone_name, identity)
            if not isinstance(quat, (list, tuple)) or len(quat) != 4:
                quat = identity
            buf.extend(struct.pack("<4f", *quat))
    return base64.b64encode(bytes(buf)).decode()


async def _generate_gesture(audio_bytes: bytes | None) -> dict | None:
    """Call the gesture service to generate upper-body motion from TTS audio.

    Returns dict with 'motion_b64' (base64 binary) and 'motion_fps', or None.
    """
    if not audio_bytes:
        return None
    gesture_url = config.get("gesture_url", "")
    if not gesture_url:
        return None
    try:
        resp = await http.post(
            f"{gesture_url}/generate_bytes",
            files={"audio_bytes": ("audio.wav", audio_bytes, "audio/wav")},
            timeout=10.0,
        )
        if resp.status_code != 200:
            log.warning("Gesture service returned %d", resp.status_code)
            return None
        data = resp.json()
        vrm_bones = data.get("vrm_bones")
        if not vrm_bones:
            return None
        log.info(
            "Gesture generated: %d frames, %.1fms latency",
            data.get("num_frames", 0), data.get("latency_ms", 0),
        )
        num_frames = len(vrm_bones)
        reported = data.get("num_frames", num_frames)
        if reported != num_frames:
            log.warning("Gesture frame count mismatch: reported=%d actual=%d", reported, num_frames)
        return {
            "motion_b64": _pack_motion_b64(vrm_bones),
            "motion_fps": data.get("fps", 30),
            "motion_frames": num_frames,
        }
    except Exception as e:
        log.debug("Gesture generation unavailable: %s", e)
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


async def _llm_chat(user_text: str, memories: list[dict], job_id: int = 0,
                    log_ctx: CallContext | None = None) -> list[dict]:
    await _monitor_thread_start("llm", input_preview=user_text, job_id=job_id)

    system_prompt = build_system_prompt(
        animation_mode=ANIMATION_MODE,
    )
    messages = [{"role": "system", "content": system_prompt}]
    if memories:
        mem_lines = [f"- ({m['role']}): {m['text']}" for m in memories]
        messages.append({"role": "system", "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines)})

    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append({"role": entry["role"], "content": entry["content"]})

    messages.append({"role": "user", "content": user_text})

    try:
        t0 = time.monotonic()
        result = await llm_client.chat(messages)
        llm_ms = (time.monotonic() - t0) * 1000
        _pipeline_state["llm_ms"] = round(llm_ms, 1)

        _usage = result.get("usage", {})
        ctx = log_ctx or CallContext(triggered_by="channel", conversation_id=str(job_id))
        asyncio.create_task(call_log.log_call(
            ctx, model=llm_client.model,
            temperature=llm_client.default_temperature,
            max_tokens=llm_client.default_max_tokens,
            stream=False, tools_provided=False, messages=messages,
            response_content=result.get("content"),
            response_tool_calls=result.get("tool_calls"),
            finish_reason=result.get("finish_reason"),
            error=result.get("_error"), latency_ms=llm_ms,
            prompt_tokens=_usage.get("prompt_tokens"),
            completion_tokens=_usage.get("completion_tokens"),
            total_tokens=_usage.get("total_tokens"),
        ))

        content = result.get("content")
        if not content:
            log.error("LLM response empty")
            return [dict(_LLM_FALLBACK)]

        segments = _parse_llm_response(content)
        segments = await normalize_llm_segments(user_text, segments, job_id=job_id)
        await _monitor_thread_end("llm", elapsed_ms=llm_ms, input_preview=user_text, job_id=job_id)
        await _monitor_job_phase(job_id, "llm", "end", input_preview=user_text,
                                   total_segments=len(segments), elapsed_ms=llm_ms)
        log.info(
            "LLM parsed %d segment(s) in %.0fms: %s",
            len(segments), llm_ms,
            " | ".join(f'[{s["emotion"]}] {s["text"][:40]}' for s in segments),
        )
        return segments
    except httpx.ConnectError as e:
        log.error("LLM unreachable at %s: %s", config["llm_url"], e)
        await _monitor_thread_end("llm", job_id=job_id)
        return [dict(_LLM_FALLBACK)]
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        await _monitor_thread_end("llm", job_id=job_id)
        return [dict(_LLM_FALLBACK)]


def _drop_echo_segments(user_text: str, segments: list[dict]) -> list[dict]:
    """Drop clunky 'echo' first segments like 'My hair?' that restate the user."""
    if len(segments) < 2:
        return segments

    first = (segments[0].get("text") or "").strip()
    if not first:
        return segments[1:]

    # Keep explicit interruption reactions like "Oh?" / "Huh?"
    if len(first.split()) <= 2 and first.endswith("?"):
        if first.lower() in {"oh?", "huh?", "what?", "hm?", "hmm?"}:
            return segments

    # Heuristic: very short question that overlaps heavily with user tokens
    first_tokens = {t.lower().strip(".,!?\"'()") for t in first.split() if t.strip()}
    user_tokens = {t.lower().strip(".,!?\"'()") for t in (user_text or "").split() if t.strip()}
    overlap = len(first_tokens & user_tokens)

    if len(first_tokens) <= 4 and first.endswith("?") and overlap >= 1:
        return segments[1:]

    # Also drop short acknowledgements that add no content
    ack = first.lower()
    if ack in {"sure.", "sure!", "okay.", "okay!", "got it.", "got it!", "alright.", "alright!"}:
        return segments[1:]

    return segments


# ---------------------------------------------------------------------------
#  Streaming LLM — yields segments one at a time as Ollama produces tokens
# ---------------------------------------------------------------------------

async def _llm_chat_stream(
    user_text: str,
    memories: list[dict],
    job_id: int = 0,
    history_limit: int = MAX_HISTORY,
    unified_routing: bool = False,
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    enable_thinking: bool | None = None,
    log_ctx: CallContext | None = None,
):
    """Async generator for Unity streaming turns.

    Yields:

    - ``{"_stream": "text", "text": "..."}`` — ``text`` JSON field just completed.
    - ``{"_stream": "segment", "segment": {...}}`` — full segment (emotion/action).
    - ``{"_stream": "tool_calls", "tool_calls": [...], "message": {...}}`` — tool calls
      detected in the final ``done`` chunk (only when ``tools`` is provided).
    - Legacy fallback: ``{"_stream": "segment", "segment": {...}}`` for errors.

    The caller runs TTS on ``text`` when it is a single sentence; on ``segment``
    it resolves animation and finalises.
    """
    await _monitor_thread_start("llm", input_preview=user_text, job_id=job_id)

    system_prompt = build_system_prompt(
        animation_mode=ANIMATION_MODE,
        unified_routing=unified_routing,
        tools_available=bool(tools),
    )
    messages = [{"role": "system", "content": system_prompt}]
    if memories:
        mem_lines = [f"- ({m['role']}): {m['text']}" for m in memories]
        messages.append({"role": "system", "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines)})
    for entry in conversation_history[-history_limit:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_text})

    parser = _StreamingSegmentParser()
    t0 = time.monotonic()
    first_yielded = False
    seg_count = 0
    _log_content_parts: list[str] = []
    _log_ttft: float | None = None
    _log_usage: dict = {}
    _log_finish: str | None = None
    _log_tc: list[dict] | None = None
    _log_error: str | None = None

    def _emit_log(latency: float) -> None:
        ctx = log_ctx or CallContext(triggered_by="chat_stream", conversation_id=str(job_id))
        asyncio.create_task(call_log.log_call(
            ctx, model=llm_client.model,
            temperature=llm_client.default_temperature,
            max_tokens=max_tokens or llm_client.default_max_tokens,
            stream=True, enable_thinking=enable_thinking,
            tools_provided=bool(tools), messages=messages,
            response_content="".join(_log_content_parts) or None,
            response_tool_calls=_log_tc,
            finish_reason=_log_finish, error=_log_error,
            latency_ms=latency, ttft_ms=_log_ttft,
            prompt_tokens=_log_usage.get("prompt_tokens"),
            completion_tokens=_log_usage.get("completion_tokens"),
            total_tokens=_log_usage.get("total_tokens"),
        ))

    try:
        async for chunk in llm_client.chat_stream(messages, tools=tools, max_tokens=max_tokens, enable_thinking=enable_thinking):
            content = chunk.get("content", "")
            if content:
                _log_content_parts.append(content)
                if _log_ttft is None:
                    _log_ttft = (time.monotonic() - t0) * 1000
                for ev in parser.feed(content):
                    if not first_yielded:
                        if ev.get("kind") == "text":
                            first_yielded = True
                            log.info(
                                "LLM stream: first text field in %.0fms",
                                (time.monotonic() - t0) * 1000,
                            )
                        elif ev.get("kind") == "segment":
                            first_yielded = True
                            log.info(
                                "LLM stream: first segment in %.0fms",
                                (time.monotonic() - t0) * 1000,
                            )
                    if ev.get("kind") == "thinking_delta":
                        yield {"_stream": "thinking_delta", "content": ev["content"]}
                    elif ev.get("kind") == "thinking_done":
                        log.info(
                            "LLM stream: thinking done in %.0fms",
                            (time.monotonic() - t0) * 1000,
                        )
                        yield {"_stream": "thinking_done"}
                    elif ev.get("kind") == "text":
                        yield {"_stream": "text", "text": ev["text"]}
                    elif ev.get("kind") == "segment":
                        seg_count += 1
                        yield {"_stream": "segment", "segment": ev["segment"]}

            if chunk.get("done"):
                _log_usage = chunk.get("usage", {})
                _log_finish = chunk.get("finish_reason")
                # Check for tool_calls in the final chunk
                tc = chunk.get("tool_calls")
                if tc:
                    _log_tc = tc
                    llm_ms = (time.monotonic() - t0) * 1000
                    _pipeline_state["llm_ms"] = round(llm_ms, 1)
                    _emit_log(llm_ms)
                    await _monitor_thread_end(
                        "llm", elapsed_ms=llm_ms, input_preview=user_text, job_id=job_id,
                    )
                    yield {
                        "_stream": "tool_calls",
                        "tool_calls": tc,
                        "message": {"role": "assistant", "content": "", "tool_calls": tc},
                    }
                    return
                break

        for seg in parser.finish():
            seg_count += 1
            yield {"_stream": "segment", "segment": seg}

        llm_ms = (time.monotonic() - t0) * 1000
        _pipeline_state["llm_ms"] = round(llm_ms, 1)
        _emit_log(llm_ms)
        log.info("LLM stream done: %d segment object(s) in %.0fms", seg_count, llm_ms)
        await _monitor_thread_end(
            "llm", elapsed_ms=llm_ms, input_preview=user_text, job_id=job_id,
        )
        await _monitor_job_phase(
            job_id, "llm", "end", input_preview=user_text,
            total_segments=seg_count, elapsed_ms=llm_ms,
        )

    except httpx.ConnectError as e:
        _log_error = str(e)
        _emit_log((time.monotonic() - t0) * 1000)
        log.error("LLM unreachable at %s: %s", config["llm_url"], e)
        await _monitor_thread_end("llm", job_id=job_id)
        yield {"_stream": "segment", "segment": dict(_LLM_FALLBACK)}
    except Exception as e:
        _log_error = str(e)
        _emit_log((time.monotonic() - t0) * 1000)
        log.error("LLM stream failed: %s", e)
        await _monitor_thread_end("llm", job_id=job_id)
        if not first_yielded:
            yield {"_stream": "segment", "segment": dict(_LLM_FALLBACK)}


async def _synthesize(text: str) -> Optional[bytes]:
    try:
        resp = await http.post(
            f"{config['tts_url']}/synthesize",
            json={"text": text},
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

async def _process_one_segment(seg: dict, include_audio: bool,
                                 job_id: int = 0, seg_idx: int = 0,
                                 total_segments: int = 0) -> dict:
    """Resolve animation + TTS for a single segment. Run concurrently."""
    t0 = time.monotonic()
    clip_name = await _resolve_action(seg["action"], seg["emotion"])
    anim_ms = (time.monotonic() - t0) * 1000

    audio = None
    tts_ms = 0
    if include_audio:
        await _monitor_thread_start("tts", input_preview=seg["text"], job_id=job_id,
                                      segment=seg_idx, total_segments=total_segments)
        t1 = time.monotonic()
        audio = await _synthesize(seg["text"])
        tts_ms = (time.monotonic() - t1) * 1000
        await _monitor_thread_end("tts", elapsed_ms=tts_ms, input_preview=seg["text"],
                                    job_id=job_id, segment=seg_idx, total_segments=total_segments)
        await _monitor_job_phase(job_id, "tts", "end", input_preview=seg["text"],
                                   segment=seg_idx, total_segments=total_segments, elapsed_ms=tts_ms)

    viseme_data = await _generate_visemes(audio, seg.get("text", ""))
    return {**seg, "clip": clip_name, "audio": audio, "anim_ms": anim_ms, "tts_ms": tts_ms,
            "viseme_data": viseme_data}


async def _process_segments(segments: list[dict], include_audio: bool = True,
                              job_id: int = 0) -> list[dict]:
    """Resolve animation + TTS for all segments concurrently."""
    t0 = time.monotonic()
    total = len(segments)
    tasks = [_process_one_segment(seg, include_audio, job_id=job_id, seg_idx=i, total_segments=total)
             for i, seg in enumerate(segments)]
    results = await asyncio.gather(*tasks)
    total_ms = (time.monotonic() - t0) * 1000

    max_tts = max((r.get("tts_ms", 0) for r in results), default=0)
    max_anim = max((r.get("anim_ms", 0) for r in results), default=0)
    _pipeline_state["tts_ms"] = round(max_tts, 1)
    _pipeline_state["anim_ms"] = round(max_anim, 1)

    log.info(
        "Processed %d segments in %.0fms (max TTS=%.0fms, max anim=%.0fms)",
        len(results), total_ms, max_tts, max_anim,
    )
    return list(results)


# ---------------------------------------------------------------------------
#  Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup():
    asyncio.create_task(_init_call_log())
    asyncio.create_task(_idle_heartbeat_loop())
    asyncio.create_task(_llm_warmup())
    asyncio.create_task(_cache_rewarm_loop())
    asyncio.create_task(_start_channels())
    asyncio.create_task(_start_agent_loop())
    asyncio.create_task(_start_shiro())


_shiro_agent = None
_agent_loop_instance = None


def get_agent_loop():
    """Return the running AgentLoop singleton (or None if disabled/not started)."""
    return _agent_loop_instance


@app.on_event("shutdown")
async def _on_shutdown():
    if _shiro_agent:
        await _shiro_agent.stop()
    if _agent_loop_instance:
        await _agent_loop_instance.stop()
    await call_log.close_pool()
    await llm_client.close()


async def _start_shiro():
    """Start Shiro coaching agent if enabled."""
    global _shiro_agent
    await asyncio.sleep(5)  # let PG pool and other services init first
    shiro_cfg = full_config.get("shiro", {})
    if not shiro_cfg.get("enabled"):
        log.info("Shiro: disabled in config")
        return
    call_log_dsn = full_config.get("call_log", {}).get("dsn", "")
    if not call_log_dsn:
        log.warning("Shiro: call_log.dsn not configured — agent disabled")
        return
    try:
        from shiro.agent import ShiroAgent
        _shiro_agent = ShiroAgent(shiro_cfg, full_config.get("llm", {}), call_log_dsn)
        await _shiro_agent.start()
        log.info("Shiro coaching agent started")
    except Exception as exc:
        log.error("Failed to start Shiro: %s", exc)


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
    if tg_cfg.get("enabled") and tg_cfg.get("bot_token"):
        try:
            from channels.telegram import TelegramChannel
            ch = TelegramChannel(
                bot_token=tg_cfg["bot_token"],
                bridge_url=bridge_url,
                allowed_users=tg_cfg.get("allowed_users"),
            )
            registry.register(ch)
        except Exception as exc:
            log.error("Failed to init Telegram channel: %s", exc)

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

    system_prompt = build_system_prompt(
        animation_mode=ANIMATION_MODE,
    )
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    # Fetch recent memories so the memory prefix is also cached.
    try:
        resp = await http.post(
            f"{config['memory_url']}/query",
            json={"text": "recent context", "n_results": 5},
        )
        if resp.status_code == 200:
            mems = resp.json().get("memories", [])
            if mems:
                mem_lines = [f"- ({m['role']}): {m['text']}" for m in mems]
                messages.append({
                    "role": "system",
                    "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines),
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
    for name, url in [
        ("stt", config["stt_url"]),
        ("tts", config["tts_url"]),
        ("memory", config["memory_url"]),
        ("animation", config["animation_url"]),
    ]:
        try:
            r = await http.get(f"{url}/health", timeout=5.0)
            checks[name] = "ok" if r.status_code == 200 else f"error ({r.status_code})"
        except Exception as e:
            checks[name] = f"down ({e})"
    # LLM health via the dedicated client
    checks["llm"] = "ok" if await llm_client.health() else "down"

    all_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "services": checks,
        "model": llm_config.get("model", ""),
    }


@app.get("/debug/state")
async def debug_state():
    return {**_pipeline_state}


_SENTENCE_END_CHARS = frozenset(".!?")


def _segment_ends_sentence(text: str) -> bool:
    t = (text or "").rstrip()
    if not t:
        return True
    return t[-1] in _SENTENCE_END_CHARS


def _segments_full_text(segments: list[dict]) -> str:
    """Join segment strings for display and memory.

    The model often omits trailing punctuation on each segment; a plain space join
    then reads as one run-on sentence ("...for you We have..."). Insert ``. ``
    between segments when the previous segment does not already end with
    sentence-ending punctuation.
    """
    texts = [(s.get("text") or "").strip() for s in segments]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    out = texts[0]
    for i in range(1, len(texts)):
        prev = texts[i - 1]
        sep = " " if _segment_ends_sentence(prev) else ". "
        out = out + sep + texts[i]
    return out


# ---------------------------------------------------------------------------
#  GET /api/conversation — chat history for the dashboard
# ---------------------------------------------------------------------------

@app.get("/api/conversation")
async def api_conversation(limit: int = 0):
    entries = _chat_log[-limit:] if limit > 0 else _chat_log
    return JSONResponse({"exchanges": entries})


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
async def api_tts(req: TtsRequest):
    audio = await _synthesize(req.text)
    if audio is None:
        return JSONResponse({"error": "TTS synthesis failed"}, status_code=502)
    return StreamingResponse(io.BytesIO(audio), media_type="audio/wav")


# ---------------------------------------------------------------------------
#  GET /chat/stream — SSE streaming chat hub (dashboard + unified log)
#  Yields each segment as a server-sent event in real-time as the LLM produces it.
#  Supports an inline ReAct-style tool loop when tools.enabled is true.
# ---------------------------------------------------------------------------

@app.get("/chat/stream")
async def chat_stream(text: str, client_id: str = ""):
    _touch_interaction(user_text=text)
    jid = _new_job_id()
    _timeline_init(jid)
    await _broadcast_debug_state("thinking", last_input=text)
    mem_task = asyncio.create_task(_query_memories(text))
    all_segs: list[dict] = []
    seg_idx = 0
    _STREAM_TOTAL = -1
    tool_events: list[dict] = []   # tool status events for chat_log

    async def _process_and_yield_seg(seg, seg_idx_local):
        """TTS + animation + yield SSE for one segment.  Returns (seg_dict, seg_idx_local+1)."""
        await _broadcast_timeline_event(
            jid, "tts", "start", segment=seg_idx_local, total=_STREAM_TOTAL,
        )
        await _monitor_thread_start(
            "tts", input_preview=seg["text"], job_id=jid,
            segment=seg_idx_local, total_segments=_STREAM_TOTAL,
        )
        t_seg = time.monotonic()

        clip_task = asyncio.create_task(
            _resolve_action(seg.get("action") or "", seg.get("emotion", "neutral"))
        )
        audio_task = asyncio.create_task(_synthesize(seg["text"]))
        audio, clip_name = await asyncio.gather(audio_task, clip_task)

        tts_ms = (time.monotonic() - t_seg) * 1000
        await _monitor_thread_end(
            "tts", elapsed_ms=tts_ms, input_preview=seg["text"],
            job_id=jid, segment=seg_idx_local, total_segments=_STREAM_TOTAL,
        )
        await _monitor_job_phase(
            jid, "tts", "end", input_preview=seg["text"],
            segment=seg_idx_local, total_segments=_STREAM_TOTAL,
            elapsed_ms=tts_ms,
        )
        await _broadcast_timeline_event(
            jid, "tts", "end", segment=seg_idx_local, total=_STREAM_TOTAL,
        )

        seg["gesture"] = clip_name
        audio_b64 = base64.b64encode(audio).decode() if audio else None
        seg["audio_base64"] = audio_b64
        all_segs.append(seg)

        payload = json.dumps({
            "type": "segment",
            "index": seg_idx_local,
            "text": seg["text"],
            "emotion": seg.get("emotion", "neutral"),
            "gesture": clip_name or "",
            "audio_base64": audio_b64,
        })

        if unity_clients:
            unity_msg = {
                "type": "speech_segment",
                "job_id": jid,
                "index": seg_idx_local,
                "total": _STREAM_TOTAL,
                "text": seg["text"],
                "emotion": seg.get("emotion", "neutral"),
                "gesture": clip_name or "",
            }
            viseme_data = seg.get("viseme_data")
            if viseme_data:
                unity_msg["viseme_b64"] = viseme_data["viseme_b64"]
                unity_msg["viseme_fps"] = viseme_data["viseme_fps"]
                unity_msg["viseme_frames"] = viseme_data["viseme_frames"]
            if audio:
                unity_msg["audio_base64"] = audio_b64
            await _broadcast_to_unity(unity_msg)

        await _broadcast_timeline_event(
            jid, "sent_to_unity", "end", segment=seg_idx_local, total=_STREAM_TOTAL,
        )

        if seg_idx_local == 0:
            await _broadcast_debug_state(
                "speaking", segment_count=_STREAM_TOTAL,
                tts_ms=round(tts_ms, 1),
            )

        return f"data: {payload}\n\n"

    async def generate():
        nonlocal all_segs, seg_idx, tool_events
        _tts_seg_active = False

        try:
            await _broadcast_timeline_event(jid, "llm", "start")

            # ----------------------------------------------------------
            #  Phase 1: Unified routing (LLM decides: answer / tools / escalate)
            # ----------------------------------------------------------
            tool_calls_from_stream: list[dict] | None = None
            stream_msg: dict | None = None

            memories = await mem_task
            _cs_ctx = CallContext(triggered_by="chat_stream", source="web",
                                 conversation_id=str(jid))
            async for item in _unified_route(text, memories, job_id=jid, log_ctx=_cs_ctx):
                # Tool-call detection from the done chunk
                if item.get("_stream") == "tool_calls":
                    tool_calls_from_stream = item["tool_calls"]
                    stream_msg = item.get("message", {})
                    break

                if item.get("_stream") == "thinking_delta":
                    evt = json.dumps({
                        "type": "thinking_delta",
                        "content": item.get("content", ""),
                    })
                    yield f"data: {evt}\n\n"
                    continue

                if item.get("_stream") == "thinking_done":
                    yield f'data: {{"type":"thinking_done"}}\n\n'
                    continue

                if item.get("_stream") != "segment":
                    continue
                raw_seg = item.get("segment") or {}

                if not all_segs:
                    await _broadcast_debug_state(
                        "synthesizing", llm_ms=_pipeline_state.get("llm_ms", 0),
                    )

                segs = await normalize_llm_segments(text, [raw_seg], job_id=jid)
                for seg in segs:
                    _tts_seg_active = True
                    sse_line = await _process_and_yield_seg(seg, seg_idx)
                    _tts_seg_active = False
                    yield sse_line
                    seg_idx += 1

            # ----------------------------------------------------------
            #  Safety net: stalling without tool call → auto ask_nori
            #  (same tightening as the live /ws path — see above)
            # ----------------------------------------------------------
            if not tool_calls_from_stream and TOOLS_ENABLED and all_segs:
                _all_text = " ".join(s.get("text", "") for s in all_segs)
                _fire, _reason = _should_auto_route_to_nori(text, _all_text)
                if _fire:
                    log.warning("[safety-net/sse] Auto-injecting ask_nori (%s) for: %s", _reason, text[:80])
                    tool_calls_from_stream = [{
                        "id": "auto_nori_0",
                        "function": {
                            "name": "ask_nori",
                            "arguments": {"request": text},
                        },
                    }]
                    stream_msg = {"role": "assistant", "content": _all_text}
                else:
                    log.info("[safety-net/sse] suppressed: %s", _reason)

            # ----------------------------------------------------------
            #  Phase 2: Tool loop (if tool_calls detected)
            # ----------------------------------------------------------
            if tool_calls_from_stream and TOOLS_ENABLED:
                await _broadcast_timeline_event(jid, "llm", "end")

                # Inject stalling segment if none was produced
                if seg_idx == 0:
                    _first_tool_name = ""
                    if tool_calls_from_stream:
                        _first_tool_name = (
                            tool_calls_from_stream[0].get("function", {}).get("name")
                            or ""
                        )
                    _stall_text = _pick_stall_phrase(text, _first_tool_name)
                    stall_seg = await normalize_llm_segments(text, [{
                        "text": _stall_text,
                        "emotion": "thinking",
                        "action": "tap chin thoughtfully",
                    }], job_id=jid)
                    for seg in stall_seg:
                        _tts_seg_active = True
                        sse_line = await _process_and_yield_seg(seg, seg_idx)
                        _tts_seg_active = False
                        yield sse_line
                        seg_idx += 1

                # Keep stalling segments (already streamed to TTS), but
                # don't include them in the final storable text.
                stall_segs = [s for s in all_segs if s.get("_is_thinking")]
                all_segs.clear()
                all_segs.extend(stall_segs)
                seg_idx = len(stall_segs)

                # Build messages list for the tool conversation
                tool_system = build_system_prompt(
                    animation_mode=ANIMATION_MODE,
                    tools_available=True,
                )
                messages = [{"role": "system", "content": tool_system}]
                if memories:
                    mem_lines = [f"- ({m['role']}): {m['text']}" for m in memories]
                    messages.append({"role": "system", "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines)})
                _modals_line = get_open_modals_summary()
                if _modals_line:
                    messages.append({"role": "system", "content": f"[Currently on screen]: {_modals_line}. You can reference and close these directly."})
                for entry in conversation_history[-MAX_HISTORY:]:
                    messages.append({"role": entry["role"], "content": entry["content"]})
                messages.append({"role": "user", "content": text})

                # Process first batch of tool calls from streaming phase.
                # Re-serialize arguments to JSON strings for the OpenAI API.
                api_msg = {
                    "role": "assistant",
                    "content": stream_msg.get("content") or "",
                    "tool_calls": [
                        {
                            "id": tc.get("id", f"call_{i}"),
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": (
                                    json.dumps(tc["function"]["arguments"])
                                    if isinstance(tc["function"]["arguments"], dict)
                                    else tc["function"]["arguments"]
                                ),
                            },
                        }
                        for i, tc in enumerate(tool_calls_from_stream)
                    ],
                }
                messages.append(api_msg)
                current_tool_calls = tool_calls_from_stream
                final_content: str | None = None

                for round_num in range(_TOOL_MAX_ROUNDS):
                    # Round 0 stall was already spoken; keep character animated silently after.
                    if round_num > 0:
                        asyncio.create_task(_broadcast_thinking_gesture())

                    # Execute each tool call in this round
                    for tc in current_tool_calls:
                        fn = tc.get("function", {})
                        tool_name = fn.get("name", "unknown")
                        tool_args = fn.get("arguments", {})
                        if isinstance(tool_args, str):
                            try:
                                tool_args = json.loads(tool_args)
                            except json.JSONDecodeError:
                                tool_args = {}
                        args_preview = str(tool_args.get("command", "")) or json.dumps(tool_args)[:120]
                        log.info("Tool call [round %d]: %s(%s)", round_num + 1, tool_name, args_preview[:120])

                        # Yield call status SSE
                        call_evt = {
                            "type": "tool_status", "action": "call", "round": round_num + 1,
                            "tool_name": tool_name,
                            "tool_args_preview": args_preview[:200],
                        }
                        yield f"data: {json.dumps(call_evt)}\n\n"
                        tool_events.append(call_evt)

                        # Execute
                        await _broadcast_timeline_event(jid, "tool", "start", segment=round_num)
                        await _broadcast_monitor({
                            "type": "tool_activity", "action": "call",
                            "job_id": jid, "round": round_num + 1,
                            "tool_name": tool_name,
                            "tool_args": json.dumps(tool_args)[:500],
                        })
                        t_tool = time.monotonic()
                        result = await execute_tool(tool_name, tool_args)
                        tool_ms = (time.monotonic() - t_tool) * 1000
                        await _broadcast_timeline_event(jid, "tool", "end", segment=round_num)
                        log.info("Tool result [round %d]: %.0fms, %s", round_num + 1, tool_ms, result[:120])
                        await _broadcast_monitor({
                            "type": "tool_activity", "action": "result",
                            "job_id": jid, "round": round_num + 1,
                            "tool_name": tool_name,
                            "result_preview": result[:800],
                            "duration_ms": round(tool_ms, 1),
                        })

                        # Yield result status SSE
                        result_evt = {
                            "type": "tool_status", "action": "result", "round": round_num + 1,
                            "tool_name": tool_name,
                            "result_preview": result[:500],
                            "duration_ms": round(tool_ms, 1),
                        }
                        yield f"data: {json.dumps(result_evt)}\n\n"
                        tool_events.append(result_evt)

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", f"call_{round_num}"),
                            "content": result,
                        })

                    # Next LLM round — does it want more tools or is it done?
                    await _broadcast_timeline_event(jid, "llm_tool", "start", segment=round_num)
                    t_llm = time.monotonic()
                    _tr_ctx = CallContext(triggered_by="tool_round", source="web",
                                         conversation_id=str(jid), tool_round=round_num + 1)
                    msg = await _llm_tool_round(messages, job_id=jid, log_ctx=_tr_ctx)
                    llm_tool_ms = (time.monotonic() - t_llm) * 1000
                    await _broadcast_timeline_event(jid, "llm_tool", "end", segment=round_num)

                    if not msg:
                        break

                    tc = _extract_tool_calls(msg)
                    if tc:
                        # Re-serialize for the OpenAI API
                        api_followup = {
                            "role": "assistant",
                            "content": msg.get("content") or "",
                            "tool_calls": [
                                {
                                    "id": t.get("id", f"call_{i}"),
                                    "type": "function",
                                    "function": {
                                        "name": t["function"]["name"],
                                        "arguments": (
                                            json.dumps(t["function"]["arguments"])
                                            if isinstance(t["function"]["arguments"], dict)
                                            else t["function"]["arguments"]
                                        ),
                                    },
                                }
                                for i, t in enumerate(tc)
                            ],
                        }
                        messages.append(api_followup)
                        current_tool_calls = tc
                        continue
                    else:
                        final_content = msg.get("content", "")
                        break

                # Exhausted rounds — force a final answer without tools
                if final_content is None:
                    log.warning("Tool loop exhausted %d rounds, forcing final answer", _TOOL_MAX_ROUNDS)
                    try:
                        _force_msgs = messages + [{"role": "user", "content": "Please provide your final answer now."}]
                        _t0f = time.monotonic()
                        result = await llm_client.chat(_force_msgs)
                        _latf = (time.monotonic() - _t0f) * 1000
                        _uf = result.get("usage", {})
                        asyncio.create_task(call_log.log_call(
                            CallContext(triggered_by="tool_force_answer", conversation_id=str(jid)),
                            model=llm_client.model,
                            temperature=llm_client.default_temperature,
                            max_tokens=llm_client.default_max_tokens,
                            stream=False, tools_provided=False, messages=_force_msgs,
                            response_content=result.get("content"),
                            finish_reason=result.get("finish_reason"),
                            error=result.get("_error"), latency_ms=_latf,
                            prompt_tokens=_uf.get("prompt_tokens"),
                            completion_tokens=_uf.get("completion_tokens"),
                            total_tokens=_uf.get("total_tokens"),
                        ))
                        final_content = result.get("content", "")
                    except Exception:
                        pass

                # Parse final content into segments
                if final_content:
                    segments = _parse_llm_response(final_content)
                    segments = await normalize_llm_segments(text, segments, job_id=jid)
                else:
                    segments = [dict(_LLM_FALLBACK)]

                # Phase 3: Process final segments through TTS + animation
                for seg in segments:
                    _tts_seg_active = True
                    sse_line = await _process_and_yield_seg(seg, seg_idx)
                    _tts_seg_active = False
                    yield sse_line
                    seg_idx += 1

            else:
                # No tool calls — normal streaming path completed
                await _broadcast_timeline_event(jid, "llm", "end")

        except Exception as exc:
            log.error("chat_stream error: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return
        finally:
            if _tts_seg_active:
                try:
                    await _monitor_thread_end("tts", job_id=jid)
                except Exception:
                    pass

        # Send speech_end to Unity
        if unity_clients and seg_idx > 0:
            await _broadcast_to_unity({
                "type": "speech_end", "job_id": jid, "total": seg_idx,
            })

        # Store history after all segments received (exclude stalling segments)
        storable_segs = [s for s in all_segs
                         if not s.get("_is_thinking") and not s.get("needs_context")]
        if not storable_segs:
            storable_segs = all_segs
        if storable_segs and _llm_reply_ok(storable_segs[0]["text"]):
            full_text = _segments_full_text(storable_segs)
            conversation_history.append({"role": "user", "content": text})
            conversation_history.append({"role": "assistant", "content": full_text})
            asyncio.create_task(_store_memory(text, "user"))
            asyncio.create_task(_store_memory(full_text, "assistant"))
            entry = {
                "user_text": text,
                "assistant_text": full_text,
                "source": "dashboard",
                "timestamp": time.time(),
                "_client_id": client_id,
                "segments": [
                    {"text": s["text"], "emotion": s["emotion"], "action": s.get("action"),
                     "gesture": s.get("gesture"), "audio_base64": s.get("audio_base64")}
                    for s in storable_segs
                ],
                "tool_events": tool_events if tool_events else None,
            }
            _chat_log.append(entry)
            await _broadcast_chat_entry(entry)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        await _broadcast_debug_state("idle")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
#  POST /admin/reload-tools — hot-reload custom tools
# ---------------------------------------------------------------------------

@app.post("/admin/reload-tools")
async def reload_tools():
    """Hot-reload custom tools from tools/custom/."""
    global TOOL_SCHEMAS
    import tools.registry as _reg
    _reg.reload_custom_tools()
    TOOL_SCHEMAS = _reg.TOOL_SCHEMAS
    return JSONResponse({"status": "ok", "tools": _reg.TOOL_NAMES})


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

    await _broadcast_to_unity({
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
        "clients": len(unity_clients),
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
    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "clear_theme_preview",
    })
    return JSONResponse({"status": "preview_cleared", "clients": len(unity_clients)})


async def request_screenshot(timeout: float = 8.0) -> str:
    """Ask any connected web client to html2canvas its current viewport, then
    await the base64 PNG reply. Returns "" if no client is available or the
    deadline expires.
    """
    if not unity_clients:
        return ""

    req_id = uuid.uuid4().hex
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _screenshot_waiters[req_id] = fut

    await _broadcast_to_unity({
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
    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "reload_stylesheets",
        "v": int(time.time()),
    })
    return JSONResponse({"status": "reloaded", "clients": len(unity_clients)})


@app.post("/admin/clear-memory")
async def admin_clear_memory():
    """Wipe the Chroma memory collection (irreversible)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(f"{config['memory_url']}/clear")
            resp.raise_for_status()
        log.info("Memory cleared via admin endpoint")
        # Notify all monitor clients
        await _broadcast_monitor({"type": "memory_cleared"})
        return JSONResponse({"status": "cleared"})
    except Exception as exc:
        log.error("Failed to clear memory: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/admin/shiro/toggle")
async def admin_shiro_toggle():
    """Start or stop the Shiro coaching agent at runtime."""
    global _shiro_agent
    if _shiro_agent is not None:
        await _shiro_agent.stop()
        _shiro_agent = None
        log.info("Shiro stopped via admin endpoint")
        await _broadcast_monitor({"type": "shiro_state", "running": False})
        return JSONResponse({"status": "stopped"})
    else:
        asyncio.create_task(_start_shiro())
        log.info("Shiro start requested via admin endpoint")
        await _broadcast_monitor({"type": "shiro_state", "running": True})
        return JSONResponse({"status": "starting"})


@app.post("/admin/tts/restart")
async def admin_tts_restart():
    """Ask TTS service to reload its model (clears ref audio cache)."""
    try:
        resp = await http.post(f"{config['tts_url']}/reload", timeout=30.0)
        resp.raise_for_status()
        log.info("TTS model reloaded via admin endpoint")
        return JSONResponse({"status": "reloaded"})
    except Exception as exc:
        log.error("TTS reload failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


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

@app.post("/channel")
async def channel_intake(req: ChannelRequest):
    """Text-only pipeline for external channels.

    Runs memory + LLM (with optional tool loop) but skips TTS / Unity —
    channels only need the text response.
    """
    _touch_interaction(user_text=req.text)
    jid = _new_job_id()
    log.info("Channel [%s/%s]: %s", req.source, req.user_id, req.text[:80])

    # Learn / remember the primary user for each channel so the channel router
    # can DM them proactively (reminders, cron findings, etc.).
    if req.user_id and req.user_id != "agent":
        try:
            from bridge.channel_router import save_primary_user
            save_primary_user(req.source, req.user_id)
        except Exception as exc:
            log.warning("save_primary_user failed: %s", exc)

    await _broadcast_debug_state("thinking", last_input=req.text)

    memories = await _query_memories(req.text)

    _ch_ctx = CallContext(triggered_by="channel", source=req.source,
                          user_id=req.user_id, conversation_id=str(jid))
    if TOOLS_ENABLED:
        from tools.executor import tool_loop
        segments = await tool_loop(req.text, memories, llm_client, job_id=jid, log_ctx=_ch_ctx)
    else:
        segments = await _llm_chat(req.text, memories, job_id=jid, log_ctx=_ch_ctx)

    full_text = _segments_full_text(segments)
    if _llm_reply_ok(segments[0]["text"]):
        conversation_history.append({"role": "user", "content": req.text})
        conversation_history.append({"role": "assistant", "content": full_text})
        asyncio.create_task(_store_memory(req.text, "user"))
        asyncio.create_task(_store_memory(full_text, "assistant"))

    # Still push to Unity if connected (the 3D character reacts to channel messages too)
    if unity_clients:
        enriched = await _process_segments(segments, include_audio=True, job_id=jid)
        total = len(enriched)
        for idx, seg in enumerate(enriched):
            msg = {
                "type": "speech_segment",
                "index": idx,
                "total": total,
                "text": seg["text"],
                "emotion": seg["emotion"],
                "gesture": seg["clip"],
            }
            vd = seg.get("viseme_data")
            if vd:
                msg["viseme_b64"] = vd["viseme_b64"]
                msg["viseme_fps"] = vd["viseme_fps"]
                msg["viseme_frames"] = vd["viseme_frames"]
            if seg.get("audio"):
                msg["audio_base64"] = base64.b64encode(seg["audio"]).decode()
            await _broadcast_to_unity(msg)

    await _broadcast_debug_state("idle")

    _ch_resp = {
        "user_text": req.text,
        "assistant_text": full_text,
        "source": req.source,
        "timestamp": time.time(),
        "segments": [
            {"text": s["text"], "emotion": s["emotion"], "action": s.get("action")}
            for s in segments
        ],
    }
    _chat_log.append(_ch_resp)
    asyncio.create_task(_broadcast_chat_entry(_ch_resp))
    return JSONResponse(_ch_resp)


# ---------------------------------------------------------------------------
#  POST /voice
# ---------------------------------------------------------------------------

@app.post("/voice")
async def voice(file: UploadFile = File(...)):
    _touch_interaction()
    jid = _new_job_id()
    audio_bytes = await file.read()
    user_text = await _transcribe(audio_bytes, job_id=jid)

    if not user_text.strip():
        return JSONResponse({"error": "Could not transcribe audio", "text": ""})

    await _broadcast_debug_state("thinking", last_input=user_text)
    memories = await _query_memories(user_text)
    _voice_ctx = CallContext(triggered_by="voice", conversation_id=str(jid))
    segments = await _llm_chat(user_text, memories, job_id=jid, log_ctx=_voice_ctx)

    full_text = _segments_full_text(segments)
    if _llm_reply_ok(segments[0]["text"]):
        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": full_text})
        asyncio.create_task(_store_memory(user_text, "user"))
        asyncio.create_task(_store_memory(full_text, "assistant"))

    await _broadcast_debug_state("synthesizing", segment_count=len(segments))
    enriched = await _process_segments(segments, include_audio=True, job_id=jid)

    _voice_entry = {
        "user_text": user_text,
        "assistant_text": full_text,
        "source": "voice",
        "timestamp": time.time(),
        "segments": [
            {"text": s["text"], "emotion": s["emotion"], "action": s.get("action"),
             "gesture": s.get("clip")}
            for s in enriched
        ],
    }
    _chat_log.append(_voice_entry)
    asyncio.create_task(_broadcast_chat_entry(_voice_entry))

    if unity_clients:
        await _broadcast_debug_state("speaking")
        total = len(enriched)
        for idx, seg in enumerate(enriched):
            msg = {
                "type": "speech_segment",
                "index": idx,
                "total": total,
                "text": seg["text"],
                "emotion": seg["emotion"],
                "gesture": seg["clip"],
            }
            vd = seg.get("viseme_data")
            if vd:
                msg["viseme_b64"] = vd["viseme_b64"]
                msg["viseme_fps"] = vd["viseme_fps"]
                msg["viseme_frames"] = vd["viseme_frames"]
            if seg.get("audio"):
                msg["audio_base64"] = base64.b64encode(seg["audio"]).decode()
            await _broadcast_to_unity(msg)

    await _broadcast_debug_state("idle")

    first_audio = next((s["audio"] for s in enriched if s.get("audio")), None)
    if first_audio:
        return StreamingResponse(
            io.BytesIO(first_audio),
            media_type="audio/wav",
            headers={
                "X-User-Text": user_text,
                "X-Assistant-Text": full_text,
                "X-Emotion": enriched[0]["emotion"],
                "X-Gesture": enriched[0].get("clip") or "",
                "X-Segment-Count": str(len(enriched)),
            },
        )

    return JSONResponse({
        "user_text": user_text,
        "assistant_text": full_text,
        "segments": [
            {"text": s["text"], "emotion": s["emotion"], "gesture": s["clip"]}
            for s in enriched
        ],
    })


# ---------------------------------------------------------------------------
#  WebSocket helpers
# ---------------------------------------------------------------------------

async def _broadcast_to_unity(message: dict):
    dead = []
    data = json.dumps(message)
    for ws in unity_clients:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        unity_clients.remove(ws)


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


async def _unity_text_turn(
    ws: WebSocket,
    text: str,
    *,
    interrupted: bool = False,
    job_id_ref: list | None = None,
    conv_ctrl: "ConversationController | None" = None,
) -> None:
    """Streaming LLM → shared ``normalize_llm_segments`` per JSON object → TTS → WebSocket.

    Each completed segment object from the stream passes through the same pipeline
    as ``POST /chat`` (repair/split, echo drop, action fallback). TTS always uses
    normalized text so it stays consistent with the non-streaming path.
    Unity uses ``total: -1`` per ``speech_segment`` (unknown count until ``speech_end``).
    """
    _STREAM_TOTAL = -1

    _touch_interaction(user_text=text)
    jid = _new_job_id()
    if job_id_ref is not None:
        job_id_ref[0] = jid
    _timeline_init(jid)

    mem_task_unity = asyncio.create_task(_query_memories(text))
    await ws.send_text(json.dumps({"type": "interrupt"}))
    await _broadcast_debug_state("thinking", last_input=text)
    await _broadcast_timeline_event(jid, "llm", "start")

    memories = await mem_task_unity
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

    all_segments: list[dict] = []
    seg_idx = 0
    last_sent = time.monotonic()
    _tts_seg_active = False
    saw_first_segment_payload = False
    tool_calls_from_stream: list[dict] | None = None
    stream_msg: dict | None = None

    # -- Local helper: process one segment (TTS + animation + send to Unity) --
    async def _send_seg(seg, cur_idx):
        nonlocal last_sent, _tts_seg_active, saw_first_segment_payload

        if not saw_first_segment_payload:
            saw_first_segment_payload = True
            await _broadcast_debug_state(
                "synthesizing", llm_ms=_pipeline_state.get("llm_ms", 0),
            )

        log.info("[stream] Job %d: TTS seg %d start", jid, cur_idx + 1)
        await _broadcast_timeline_event(
            jid, "tts", "start", segment=cur_idx, total=_STREAM_TOTAL,
        )
        await _monitor_thread_start(
            "tts", input_preview=seg["text"], job_id=jid,
            segment=cur_idx, total_segments=_STREAM_TOTAL,
        )
        _tts_seg_active = True
        t_seg = time.monotonic()

        clip_task = asyncio.create_task(
            _resolve_action(seg["action"], seg["emotion"])
        )
        audio_task = asyncio.create_task(_synthesize(seg["text"]))
        audio, clip_name = await asyncio.gather(audio_task, clip_task)

        tts_ms = (time.monotonic() - t_seg) * 1000
        marginal_ms = (time.monotonic() - last_sent) * 1000
        log.info(
            "[stream] Job %d: TTS seg %d done (%.0fms)", jid, cur_idx + 1, tts_ms,
        )

        await _monitor_thread_end(
            "tts", elapsed_ms=tts_ms, input_preview=seg["text"],
            job_id=jid, segment=cur_idx, total_segments=_STREAM_TOTAL,
        )
        _tts_seg_active = False
        await _monitor_job_phase(
            jid, "tts", "end", input_preview=seg["text"],
            segment=cur_idx, total_segments=_STREAM_TOTAL,
            elapsed_ms=tts_ms, marginal_ms=round(marginal_ms, 1),
        )
        await _broadcast_timeline_event(
            jid, "tts", "end", segment=cur_idx, total=_STREAM_TOTAL,
        )

        response = {
            "type": "speech_segment",
            "job_id": jid,
            "index": cur_idx,
            "total": _STREAM_TOTAL,
            "text": seg["text"],
            "emotion": seg["emotion"],
            "gesture": clip_name,
        }
        viseme_data = seg.get("viseme_data")
        if viseme_data:
            response["viseme_b64"] = viseme_data["viseme_b64"]
            response["viseme_fps"] = viseme_data["viseme_fps"]
            response["viseme_frames"] = viseme_data["viseme_frames"]
        if audio:
            audio_b64 = base64.b64encode(audio).decode()
            response["audio_base64"] = audio_b64
            # Server-side echo mute: calculate audio duration and set mute deadline
            if conv_ctrl is not None:
                conv_ctrl.extend_mute(audio_b64)
        await ws.send_text(json.dumps(response))
        await _broadcast_timeline_event(
            jid, "sent_to_unity", "end", segment=cur_idx, total=_STREAM_TOTAL,
        )
        last_sent = time.monotonic()

        if cur_idx == 0:
            await _broadcast_debug_state(
                "speaking", segment_count=_STREAM_TOTAL, tts_ms=round(tts_ms, 1),
            )

        all_segments.append({
            "text": seg["text"], "emotion": seg["emotion"],
            "action": seg.get("action"), "clip": clip_name,
        })

    try:
        # Unified routing: LLM decides fast path vs tools vs full-context escalation.
        _unity_ctx = CallContext(triggered_by="ws_unity", source="ws_unity",
                                conversation_id=str(jid))
        stream_iter = _unified_route(text, memories, job_id=jid, log_ctx=_unity_ctx)

        async for item in stream_iter:
            st = item.get("_stream")

            # Detect tool calls — break out to run tool loop below
            if st == "tool_calls":
                tool_calls_from_stream = item["tool_calls"]
                stream_msg = item.get("message", {})
                break

            if st == "text":
                continue

            if st != "segment":
                continue

            raw_seg = item.get("segment") or {}
            segs = await normalize_llm_segments(text, [raw_seg], job_id=jid)
            for seg in segs:
                await _send_seg(seg, seg_idx)
                seg_idx += 1

        # --------------------------------------------------------------
        #  Safety net: if LLM stalled without calling a tool, auto-call ask_nori.
        #  Tightened — misfires on short affirmations ("yeah of course!") used
        #  to spawn Nori and hallucinate welcome decks. Now requires:
        #    (a) user text is a plausible research request, and
        #    (b) Mocha's own narration didn't point at a different agent/task.
        # --------------------------------------------------------------
        if not tool_calls_from_stream and TOOLS_ENABLED and all_segments:
            _all_text = " ".join(s.get("text", "") for s in all_segments)
            _fire, _reason = _should_auto_route_to_nori(text, _all_text)
            if _fire:
                log.warning("[safety-net] LLM stalled without tool call (%s), auto-injecting ask_nori for: %s", _reason, text[:80])
                tool_calls_from_stream = [{
                    "id": "auto_nori_0",
                    "function": {
                        "name": "ask_nori",
                        "arguments": {"request": text},
                    },
                }]
                stream_msg = {"role": "assistant", "content": _all_text}
            else:
                log.info("[safety-net] suppressed: %s", _reason)

        # --------------------------------------------------------------
        #  Tool execution loop (mirrors dashboard Phase 2)
        # --------------------------------------------------------------
        if tool_calls_from_stream and TOOLS_ENABLED:
            await _broadcast_timeline_event(jid, "llm", "end")

            # If no stalling segment was spoken yet, inject one so the user
            # hears something while Nori works (otherwise: 10-20s of silence).
            if seg_idx == 0:
                _first_tool_name = ""
                if tool_calls_from_stream:
                    _first_tool_name = (
                        tool_calls_from_stream[0].get("function", {}).get("name")
                        or ""
                    )
                _stall_text = _pick_stall_phrase(text, _first_tool_name)
                await _broadcast_agent_thought(source="mocha", kind="stall", text=_stall_text)
                stall_seg = await normalize_llm_segments(text, [{
                    "text": _stall_text,
                    "emotion": "thinking",
                    "action": "tap chin thoughtfully",
                }], job_id=jid)
                for seg in stall_seg:
                    await _send_seg(seg, seg_idx)
                    seg_idx += 1

            # Keep stalling segments, reset index for tool-produced segments
            stall_segs = [s for s in all_segments if s.get("_is_thinking")]
            all_segments.clear()
            all_segments.extend(stall_segs)
            seg_idx = len(stall_segs)

            # Build messages list for the tool conversation
            tool_system = build_system_prompt(
                animation_mode=ANIMATION_MODE,
                tools_available=True,
            )
            messages: list[dict] = [{"role": "system", "content": tool_system}]
            if memories:
                mem_lines = [f"- ({m['role']}): {m['text']}" for m in memories]
                messages.append({
                    "role": "system",
                    "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines),
                })
            _modals_line = get_open_modals_summary()
            if _modals_line:
                messages.append({
                    "role": "system",
                    "content": f"[Currently on screen]: {_modals_line}. You can reference and close these directly.",
                })
            for entry in conversation_history[-MAX_HISTORY:]:
                messages.append({"role": entry["role"], "content": entry["content"]})
            messages.append({"role": "user", "content": text})

            # Append assistant message with tool calls
            api_msg = {
                "role": "assistant",
                "content": stream_msg.get("content") or "",
                "tool_calls": [
                    {
                        "id": tc.get("id", f"call_{i}"),
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": (
                                json.dumps(tc["function"]["arguments"])
                                if isinstance(tc["function"]["arguments"], dict)
                                else tc["function"]["arguments"]
                            ),
                        },
                    }
                    for i, tc in enumerate(tool_calls_from_stream)
                ],
            }
            messages.append(api_msg)
            current_tool_calls = tool_calls_from_stream
            final_content: str | None = None

            for round_num in range(_TOOL_MAX_ROUNDS):
                # Round 0 stall was already spoken aloud; keep character animated
                # silently during all subsequent rounds.
                if round_num > 0:
                    asyncio.create_task(_broadcast_thinking_gesture())

                for tc in current_tool_calls:
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "unknown")
                    tool_args = fn.get("arguments", {})
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {}

                    log.info("[unity] Tool call [round %d]: %s(%s)",
                             round_num + 1, tool_name, str(tool_args)[:120])

                    await _broadcast_timeline_event(jid, "tool", "start", segment=round_num)
                    await _broadcast_monitor({
                        "type": "tool_activity", "action": "call",
                        "job_id": jid, "round": round_num + 1,
                        "tool_name": tool_name,
                        "tool_args": json.dumps(tool_args)[:500],
                    })
                    await _broadcast_agent_thought(
                        source="mocha", kind="tool_call",
                        text=f"{tool_name}({json.dumps(tool_args)[:140]})",
                        extra={"round": round_num + 1, "tool": tool_name},
                    )
                    t_tool = time.monotonic()
                    _mocha_state["last_tool_at_monotonic"] = t_tool
                    result = await execute_tool(tool_name, tool_args)
                    tool_ms = (time.monotonic() - t_tool) * 1000
                    await _broadcast_timeline_event(jid, "tool", "end", segment=round_num)
                    await _broadcast_monitor({
                        "type": "tool_activity", "action": "result",
                        "job_id": jid, "round": round_num + 1,
                        "tool_name": tool_name,
                        "result_preview": result[:800],
                        "duration_ms": round(tool_ms, 1),
                    })
                    # Errors get their own kind for UI coloring. Catch plain
                    # error strings AND JSON blobs that contain {"error": "..."}.
                    _lr = result.lstrip()
                    _is_err = (
                        _lr.startswith("Tool error")
                        or _lr.startswith("Missing ")
                        or _lr.startswith("Blocked by policy")
                        or _lr.lower().startswith("error")
                        or (_lr.startswith("{") and '"error"' in result[:400])
                    )
                    await _broadcast_agent_thought(
                        source="mocha", kind="tool_error" if _is_err else "tool_result",
                        text=f"{tool_name}: {result[:180]}",
                        extra={"round": round_num + 1, "tool": tool_name,
                               "duration_ms": round(tool_ms, 1)},
                    )
                    log.info("[unity] Tool result [round %d]: %.0fms, %s",
                             round_num + 1, tool_ms, result[:120])

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_{round_num}"),
                        "content": result,
                    })

                # Next LLM round — does it want more tools or is it done?
                await _broadcast_timeline_event(jid, "llm_tool", "start", segment=round_num)
                t_llm = time.monotonic()
                _utr_ctx = CallContext(triggered_by="tool_round", source="ws_unity",
                                      conversation_id=str(jid), tool_round=round_num + 1)
                msg = await _llm_tool_round(messages, job_id=jid, log_ctx=_utr_ctx)
                llm_tool_ms = (time.monotonic() - t_llm) * 1000
                await _broadcast_timeline_event(jid, "llm_tool", "end", segment=round_num)

                if not msg:
                    break

                tc_next = _extract_tool_calls(msg)
                if tc_next:
                    api_followup = {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": t.get("id", f"call_{i}"),
                                "type": "function",
                                "function": {
                                    "name": t["function"]["name"],
                                    "arguments": (
                                        json.dumps(t["function"]["arguments"])
                                        if isinstance(t["function"]["arguments"], dict)
                                        else t["function"]["arguments"]
                                    ),
                                },
                            }
                            for i, t in enumerate(tc_next)
                        ],
                    }
                    messages.append(api_followup)
                    current_tool_calls = tc_next
                    continue
                else:
                    final_content = msg.get("content", "")
                    break

            # Exhausted rounds — force a final answer
            if final_content is None:
                log.warning("[unity] Tool loop exhausted %d rounds, forcing final answer",
                            _TOOL_MAX_ROUNDS)
                try:
                    _force_msgs2 = messages + [{"role": "user", "content": "Please provide your final answer now."}]
                    _t0f2 = time.monotonic()
                    forced = await llm_client.chat(_force_msgs2)
                    _latf2 = (time.monotonic() - _t0f2) * 1000
                    _uf2 = forced.get("usage", {})
                    asyncio.create_task(call_log.log_call(
                        CallContext(triggered_by="tool_force_answer", source="ws_unity",
                                   conversation_id=str(jid)),
                        model=llm_client.model,
                        temperature=llm_client.default_temperature,
                        max_tokens=llm_client.default_max_tokens,
                        stream=False, tools_provided=False, messages=_force_msgs2,
                        response_content=forced.get("content"),
                        finish_reason=forced.get("finish_reason"),
                        error=forced.get("_error"), latency_ms=_latf2,
                        prompt_tokens=_uf2.get("prompt_tokens"),
                        completion_tokens=_uf2.get("completion_tokens"),
                        total_tokens=_uf2.get("total_tokens"),
                    ))
                    final_content = forced.get("content", "")
                except Exception:
                    pass

            # Parse final content into segments
            if final_content:
                tool_segments = _parse_llm_response(final_content)
                tool_segments = await normalize_llm_segments(text, tool_segments, job_id=jid)
            else:
                tool_segments = [dict(_LLM_FALLBACK)]

            # Process final segments through TTS + send to Unity
            for seg in tool_segments:
                await _send_seg(seg, seg_idx)
                seg_idx += 1

        else:
            await _broadcast_timeline_event(jid, "llm", "end")

        if seg_idx > 0:
            await ws.send_text(json.dumps({
                "type": "speech_end", "job_id": jid, "total": seg_idx,
            }))
            if conv_ctrl is not None:
                conv_ctrl.mark_segments_sent()

    except asyncio.CancelledError:
        log.info("[stream] Job %d: cancelled", jid)
        _was_cancelled = True
    except Exception as exc:
        log.info("[stream] Job %d: error (%s)", jid, exc)
        _was_cancelled = True
    else:
        _was_cancelled = False
    finally:
        if _tts_seg_active:
            try:
                await _monitor_thread_end("tts", job_id=jid)
            except Exception:
                pass

    # Only store to conversation_history if the generation completed normally.
    # Cancelled/interrupted generations produce partial responses that would
    # pollute future context.
    if all_segments and not _was_cancelled:
        storable = [s for s in all_segments
                    if not s.get("_is_thinking") and not s.get("needs_context")]
        if not storable:
            storable = all_segments
        full_text = _segments_full_text(storable)
        if _llm_reply_ok(storable[0]["text"]):
            conversation_history.append({"role": "user", "content": text})
            conversation_history.append({"role": "assistant", "content": full_text})
            asyncio.create_task(_store_memory(text, "user"))
            asyncio.create_task(_store_memory(full_text, "assistant"))
            _unity_entry = {
                "user_text": text,
                "assistant_text": full_text,
                "source": "unity",
                "timestamp": time.time(),
                "segments": [
                    {"text": s["text"], "emotion": s["emotion"], "action": s.get("action"),
                     "clip": s.get("clip")}
                    for s in storable
                ],
            }
            _chat_log.append(_unity_entry)
            asyncio.create_task(_broadcast_chat_entry(_unity_entry))

    await _broadcast_debug_state("idle")


# ---------------------------------------------------------------------------
#  WS /ws/unity — main Unity connection with barge-in support
# ---------------------------------------------------------------------------

@app.websocket("/ws/unity")
async def unity_ws(ws: WebSocket):
    global _active_generation
    await ws.accept()
    unity_clients.append(ws)
    log.info("Unity client connected. Total: %d", len(unity_clients))

    job_id_ref: list = [0]

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
                        was_speaking = _active_generation is not None and not _active_generation.done()
                        if was_speaking:
                            _active_generation.cancel()
                            log.info("Barge-in: cancelled previous generation")
                        _active_generation = asyncio.create_task(
                            _unity_text_turn(ws, text, interrupted=was_speaking, job_id_ref=job_id_ref)
                        )

                elif mtype in ("segment_play_start", "segment_play_end"):
                    seg_idx = msg.get("index", 0)
                    seg_total = msg.get("total", 0)
                    jid = int(msg.get("job_id") or 0) or job_id_ref[0]
                    act = "start" if mtype == "segment_play_start" else "end"
                    asyncio.create_task(
                        _broadcast_timeline_event(
                            jid, "unity_play", act,
                            segment=seg_idx, total=seg_total,
                        )
                    )

            elif "bytes" in raw:
                audio_bytes = raw["bytes"]
                if not audio_bytes:
                    continue

                # Barge-in: cancel previous generation on new audio
                if _active_generation and not _active_generation.done():
                    _active_generation.cancel()
                    await ws.send_text(json.dumps({"type": "interrupt"}))

                await ws.send_text(json.dumps({"type": "stt_start"}))
                await _broadcast_debug_state("listening")
                user_text = await _transcribe(audio_bytes)
                if not user_text.strip():
                    await ws.send_text(json.dumps({"type": "stt_empty"}))
                    await _broadcast_debug_state("idle")
                    continue
                await ws.send_text(json.dumps({"type": "stt_result", "text": user_text}))
                _active_generation = asyncio.create_task(
                    _unity_text_turn(ws, user_text, interrupted=True, job_id_ref=job_id_ref)
                )

    except (WebSocketDisconnect, RuntimeError):
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if ws in unity_clients:
            unity_clients.remove(ws)
        log.info("Unity client disconnected. Total: %d", len(unity_clients))


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

    Owns the conversation turn and coordinates VAD/STT → LLM → TTS → Unity.
    Implements speculative LLM execution with cancellation: if the user keeps
    talking after a VAD final, the in-flight LLM call is cancelled and
    resubmitted with the combined text.

    States: IDLE → LISTENING → PROCESSING → SPEAKING → IDLE
    """

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"

    def __init__(self, ws: WebSocket, vad: VadUtteranceSegmenter, job_id_ref: list):
        self.state = self.IDLE
        self._ws = ws
        self._vad = vad
        self._job_id_ref = job_id_ref
        self._active_gen: Optional[asyncio.Task] = None
        self._gen_id: int = 0
        self._pending_text: str = ""      # accumulated text across finals
        self._mute_until: float = 0.0     # server-side mute deadline (monotonic)
        self._lock = asyncio.Lock()       # serialize all state transitions
        # Event-based mute tracking (driven by Unity segment_play_start/end)
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
        """Called by _send_seg when a speech_segment with audio is sent to Unity.

        Parses the actual WAV sample rate from the header and extends the
        server-side mute deadline so the VAD doesn't pick up TTS echo.
        Also sends mic_mute:true to Unity on first segment.
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
        """Called after speech_end is sent to Unity."""
        self._all_segments_sent = True

    async def on_playback_event(self, event_type: str, job_id: int, seg_idx: int) -> None:
        """Called when Unity reports segment_play_start or segment_play_end."""
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
        """Wrapper around _unity_text_turn that tracks state."""
        _was_cancelled = False
        try:
            await _unity_text_turn(
                self._ws, text,
                interrupted=interrupted,
                job_id_ref=self._job_id_ref,
                conv_ctrl=self,
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
                # Don't reset to IDLE if segments are still playing on Unity
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
    """Stream PCM16 mono 16 kHz in fixed frame multiples (20 ms = 640 bytes per frame).

    Server runs Silero VAD (with webrtcvad fallback), packages utterances on
    silence, runs Whisper per utterance, then the same pipeline as /ws/unity.

    The ConversationController state machine serializes all processing and
    handles speculative LLM execution, barge-in, and echo suppression.
    """
    await ws.accept()
    if not _live_mode_enabled():
        await ws.close(code=4403)
        return
    unity_clients.append(ws)
    log.info("Live (streaming) client connected. Total unity_clients: %d", len(unity_clients))

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

    ctrl = ConversationController(ws, vad, job_id_ref)
    ws._vad_segmenter = vad  # expose for runtime config updates

    _uplink_idle_task: Optional[asyncio.Task] = None
    _uplink_lit = False
    _logged_first_pcm = False

    async def _notify_uplink_pcm(nbytes: int) -> None:
        """Monitor: light 'uplink' while Unity streams PCM on /ws/live."""
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
                        await ctrl.on_text_input(text)

                elif mtype in ("segment_play_start", "segment_play_end"):
                    seg_idx = msg.get("index", 0)
                    seg_total = msg.get("total", 0)
                    jid = int(msg.get("job_id") or 0) or job_id_ref[0]
                    act = "start" if mtype == "segment_play_start" else "end"
                    # Feed playback events to ConversationController for mute coordination
                    await ctrl.on_playback_event(mtype, jid, seg_idx)
                    asyncio.create_task(
                        _broadcast_timeline_event(
                            jid, "unity_play", act,
                            segment=seg_idx, total=seg_total,
                        )
                    )

                elif mtype == "client_hello":
                    log.info("client_hello from session %s", msg.get("session_id", "?"))
                    asyncio.create_task(_handle_autonomy_hello())

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
                    pass  # honor the frontend decision; autonomy won't force speech on a hidden tab

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
        if ws in unity_clients:
            unity_clients.remove(ws)
        log.info("Live client disconnected. Total: %d", len(unity_clients))


# ---------------------------------------------------------------------------
#  WS /ws/voice-stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/voice-stream")
async def voice_stream(ws: WebSocket):
    await ws.accept()
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

            memories = await _query_memories(user_text)
            await _broadcast_timeline_event(jid, "llm", "start")
            _vs_ctx = CallContext(triggered_by="ws_voice_stream", conversation_id=str(jid))
            segments = await _llm_chat(user_text, memories, job_id=jid, log_ctx=_vs_ctx)
            await _broadcast_timeline_event(jid, "llm", "end")

            full_text = _segments_full_text(segments)
            if _llm_reply_ok(segments[0]["text"]):
                conversation_history.append({"role": "user", "content": user_text})
                conversation_history.append({"role": "assistant", "content": full_text})
                asyncio.create_task(_store_memory(user_text, "user"))
                asyncio.create_task(_store_memory(full_text, "assistant"))
                _vs_entry = {
                    "user_text": user_text,
                    "assistant_text": full_text,
                    "source": "voice-stream",
                    "timestamp": time.time(),
                    "segments": [
                        {"text": s["text"], "emotion": s["emotion"], "action": s.get("action")}
                        for s in segments
                    ],
                }
                _chat_log.append(_vs_entry)
                asyncio.create_task(_broadcast_chat_entry(_vs_entry))

            # Sequential streaming to the client + timeline events (matches /ws/unity behavior)
            total = len(segments)
            for idx, seg in enumerate(segments):
                await _broadcast_timeline_event(jid, "tts", "start", segment=idx, total=total)
                await _monitor_thread_start("tts", input_preview=seg["text"], job_id=jid,
                                              segment=idx, total_segments=total)
                t0 = time.monotonic()
                clip_task = asyncio.create_task(_resolve_action(seg["action"], seg["emotion"]))
                audio_task = asyncio.create_task(_synthesize(seg["text"]))
                clip_name, audio = await asyncio.gather(clip_task, audio_task)
                tts_ms = (time.monotonic() - t0) * 1000
                await _monitor_thread_end("tts", elapsed_ms=tts_ms, input_preview=seg["text"],
                                            job_id=jid, segment=idx, total_segments=total)
                await _monitor_job_phase(jid, "tts", "end", input_preview=seg["text"],
                                           segment=idx, total_segments=total, elapsed_ms=tts_ms, marginal_ms=0)
                await _broadcast_timeline_event(jid, "tts", "end", segment=idx, total=total)

                await ws.send_json({
                    "type": "assistant_text",
                    "index": idx,
                    "total": total,
                    "text": seg["text"],
                    "emotion": seg["emotion"],
                    "gesture": clip_name,
                })
                await _broadcast_timeline_event(jid, "sent_to_unity", "end", segment=idx, total=total)
                if audio:
                    await ws.send_bytes(audio)

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
    # Send Shiro running state
    await ws.send_text(json.dumps({"type": "shiro_state", "running": _shiro_agent is not None}))
    # Send current config values for Config tab
    await ws.send_text(json.dumps({
        "type": "config_state",
        "vad_final_ms": _cfg_vad_final_ms,
        "vad_interim_ms": _cfg_vad_interim_ms,
        "llm_temperature": getattr(llm_client, "default_temperature", 0.8),
        "pass1_history": COMPLEXITY_SHORT_HISTORY,
        "pass2_history": MAX_HISTORY,
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
    global COMPLEXITY_SHORT_HISTORY, MAX_HISTORY

    if msg.get("type") != "config_update":
        return

    key = msg.get("key")
    value = msg.get("value")

    if key == "echo_mode" and value in ("room", "headphone"):
        LIVE_ECHO_MODE = value
        MUTE_MIC_WHILE_AGENT_TALKING = (value == "room")
        _persist_config("bridge.live.echo_mode", value)
        log.info("Echo mode changed via dashboard: %s (mute_mic=%s)", value, MUTE_MIC_WHILE_AGENT_TALKING)
        await _broadcast_to_unity({"type": "echo_mode", "mode": value})
        for mc in monitor_clients:
            try:
                await mc.send_text(json.dumps({"type": "echo_mode", "mode": value}))
            except Exception:
                pass

    elif key == "vad_final_ms":
        _cfg_vad_final_ms = int(value)
        _persist_config("bridge.live.silence_ms_final", _cfg_vad_final_ms)
        # Update any active segmenters
        for ws in list(unity_clients):
            seg = getattr(ws, "_vad_segmenter", None)
            if seg and hasattr(seg, "update_timings"):
                seg.update_timings(silence_ms_final=_cfg_vad_final_ms)
        log.info("VAD final silence changed via dashboard: %dms", _cfg_vad_final_ms)

    elif key == "vad_interim_ms":
        _cfg_vad_interim_ms = int(value)
        _persist_config("bridge.live.silence_ms_interim", _cfg_vad_interim_ms)
        for ws in list(unity_clients):
            seg = getattr(ws, "_vad_segmenter", None)
            if seg and hasattr(seg, "update_timings"):
                seg.update_timings(silence_ms_interim=_cfg_vad_interim_ms)
        log.info("VAD interim silence changed via dashboard: %dms", _cfg_vad_interim_ms)

    elif key == "llm_temperature":
        llm_client.default_temperature = float(value)
        _persist_config("llm.temperature", float(value))
        log.info("LLM temperature changed via dashboard: %.2f", llm_client.default_temperature)

    elif key == "pass1_history":
        COMPLEXITY_SHORT_HISTORY = max(1, int(value))
        _persist_config("llm.complexity_routing.short_history", COMPLEXITY_SHORT_HISTORY)
        log.info("Pass 1 history changed via dashboard: %d", COMPLEXITY_SHORT_HISTORY)

    elif key == "pass2_history":
        MAX_HISTORY = max(1, int(value))
        _persist_config("memory.short_term_limit", MAX_HISTORY)
        log.info("Pass 2 history changed via dashboard: %d", MAX_HISTORY)


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
