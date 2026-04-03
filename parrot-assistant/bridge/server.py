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

Relationship system:
  A simple affection/familiarity tracker that persists to disk and influences
  the LLM's tone via prompt injection.  Tiers: stranger -> acquaintance ->
  friend -> close_friend -> confidant.

Debug state:
  The bridge broadcasts "debug_state" messages to Unity at each pipeline stage
  with timing info so the overlay can show what's happening.

Exposes:
  POST /chat                -- text in, text + audio out
  POST /voice               -- audio in, text + audio out
  WS   /ws/unity            -- WebSocket for Unity client
  WS   /ws/voice-stream     -- WebSocket for real-time voice
  GET  /health              -- health check
  GET  /debug/state         -- current pipeline state + timing
  GET  /relationship        -- current relationship state
"""

import asyncio
import base64
import io
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from animation.ingest import parse_actions_file, describe_clip
from character.context import build_system_prompt

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bridge")

ROOT = Path(__file__).resolve().parent.parent
full_config = yaml.safe_load((ROOT / "config.yaml").read_text())
config = full_config["bridge"]
llm_config = full_config["llm"]
anim_config = full_config.get("animation", {})

# ---------------------------------------------------------------------------
#  Animation mode
# ---------------------------------------------------------------------------
ANIMATION_MODE: str = anim_config.get("mode", "vector_db")

_ANIMATION_CLIPS: list[dict] = []
_ANIMATION_CLIP_NAMES: set[str] = set()

if ANIMATION_MODE == "llm_select":
    _all = parse_actions_file()
    _ANIMATION_CLIPS = [c for c in _all if not c.get("phase")]
    _ANIMATION_CLIP_NAMES = {c["clip"] for c in _ANIMATION_CLIPS}
    log.info(
        "Animation mode=llm_select: loaded %d base clips (%d total in file)",
        len(_ANIMATION_CLIPS), len(_all),
    )

app = FastAPI(title="Parrot Bridge")
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
MAX_HISTORY = full_config["memory"].get("short_term_limit", 20)

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
    "relationship_tier": "stranger",
}

# ---------------------------------------------------------------------------
#  Monitor: real-time thread status + job history for the dashboard
# ---------------------------------------------------------------------------
_thread_states: dict = {
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
#  Relationship system
# ---------------------------------------------------------------------------
RELATIONSHIP_FILE = ROOT / "memory" / "relationship.json"

RELATIONSHIP_TIERS = [
    {"name": "stranger",     "min_affection": 0,   "prompt": "You just met this person. Be polite and curious, a bit reserved."},
    {"name": "acquaintance", "min_affection": 10,  "prompt": "You're getting to know this person. Be warmer, use their patterns."},
    {"name": "friend",       "min_affection": 30,  "prompt": "You're friends! Be casual, joke around, tease a little. Reference shared memories."},
    {"name": "close_friend", "min_affection": 60,  "prompt": "You're close friends. Be very natural, share opinions freely, be supportive."},
    {"name": "confidant",    "min_affection": 100, "prompt": "This person is your confidant. Be deeply authentic, vulnerable when appropriate, fiercely loyal."},
]

_relationship: dict = {
    "affection": 0,
    "familiarity": 0,
    "interactions": 0,
    "mood": "neutral",
    "tier": "stranger",
    "last_interaction": 0,
}


def _load_relationship():
    global _relationship
    if RELATIONSHIP_FILE.exists():
        try:
            _relationship = json.loads(RELATIONSHIP_FILE.read_text())
            _update_tier()
            log.info("Relationship loaded: tier=%s affection=%d interactions=%d",
                     _relationship["tier"], _relationship["affection"], _relationship["interactions"])
        except Exception as e:
            log.warning("Failed to load relationship: %s", e)


def _save_relationship():
    try:
        RELATIONSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        RELATIONSHIP_FILE.write_text(json.dumps(_relationship, indent=2))
    except Exception as e:
        log.warning("Failed to save relationship: %s", e)


def _update_tier():
    aff = _relationship.get("affection", 0)
    tier = "stranger"
    for t in RELATIONSHIP_TIERS:
        if aff >= t["min_affection"]:
            tier = t["name"]
    _relationship["tier"] = tier
    _pipeline_state["relationship_tier"] = tier


def _tick_relationship(user_text: str, assistant_text: str):
    """Update relationship after a successful exchange."""
    _relationship["interactions"] = _relationship.get("interactions", 0) + 1
    _relationship["last_interaction"] = time.time()

    # Affection grows slowly: +1 per interaction, +2 if user text is long (engaged)
    delta = 1
    if len(user_text.split()) > 10:
        delta = 2
    # Bonus for positive words
    positive = {"thank", "thanks", "love", "amazing", "awesome", "great", "cool", "nice", "happy", "appreciate"}
    if any(w in user_text.lower().split() for w in positive):
        delta += 1

    _relationship["affection"] = min(200, _relationship.get("affection", 0) + delta)
    _relationship["familiarity"] = min(200, _relationship.get("familiarity", 0) + 1)
    _update_tier()
    _save_relationship()


def _get_relationship_prompt() -> str:
    tier = _relationship.get("tier", "stranger")
    for t in RELATIONSHIP_TIERS:
        if t["name"] == tier:
            interactions = _relationship.get("interactions", 0)
            return (
                f"\n\n[Relationship context]\n"
                f"Relationship tier: {tier} (affection: {_relationship.get('affection', 0)}, "
                f"interactions: {interactions})\n"
                f"Tone guidance: {t['prompt']}\n"
            )
    return ""


# ---------------------------------------------------------------------------
#  Idle heartbeat
# ---------------------------------------------------------------------------
idle_config = full_config.get("idle", {})
IDLE_ENABLED = idle_config.get("enabled", True)
IDLE_INITIAL_DELAY = idle_config.get("initial_delay", 20)
IDLE_INTERVAL = idle_config.get("interval", 15)
IDLE_MAX_DURATION = idle_config.get("max_idle_duration", 300)

_last_interaction_time: float = time.monotonic()

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


def _touch_interaction():
    global _last_interaction_time
    _last_interaction_time = time.monotonic()


async def _idle_heartbeat_loop():
    if not IDLE_ENABLED:
        return
    while True:
        await asyncio.sleep(5)
        if not unity_clients:
            continue
        elapsed = time.monotonic() - _last_interaction_time
        if elapsed < IDLE_INITIAL_DELAY:
            continue
        if IDLE_MAX_DURATION > 0 and elapsed > IDLE_MAX_DURATION:
            continue

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


def _needs_repair(segments: list[dict]) -> bool:
    if not segments:
        return False
    if len(segments) == 1 and len(_split_sentences(segments[0].get("text", ""))) > 1:
        return True
    return any(len(_split_sentences(s.get("text", ""))) > 1 for s in segments)


async def _llm_repair_segments(user_text: str, draft_segments: list[dict]) -> list[dict]:
    draft_text = _segments_full_text(draft_segments)
    repair_system = (
        "You are repairing an assistant response into a strict JSON format for a 3D character.\n"
        "Return VALID JSON ONLY. No markdown, no code fences, no extra text.\n"
        'Output format:\n'
        '{"segments": [{"text": "one sentence", "emotion": "emotion_id", "action": "physical action"}, ...]}\n'
        "\n"
        "Rules:\n"
        "- Each segment.text MUST be exactly ONE sentence.\n"
        "- Split naturally at sentence boundaries.\n"
        "- Choose emotion/action per sentence; vary them naturally across segments.\n"
        "- Preserve the meaning and ordering of the original content; do not add new facts.\n"
        "- Keep segment texts suitable for TTS.\n"
    )
    repair_user = (
        f"User message:\n{user_text}\n\n"
        f"Assistant draft to repair:\n{draft_text}\n"
    )

    try:
        body: dict = {
            "model": llm_config["model"],
            "messages": [
                {"role": "system", "content": repair_system},
                {"role": "user", "content": repair_user},
            ],
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": min(2048, llm_config.get("max_tokens", 4096)),
            },
        }
        if "ollama_think" in llm_config:
            body["think"] = llm_config["ollama_think"]
        else:
            body["think"] = False

        resp = await http.post(f"{config['llm_url']}/api/chat", json=body)
        if resp.status_code != 200:
            return draft_segments
        data = resp.json()
        msg = data.get("message") or {}
        content = msg.get("content") or ""
        repaired = _parse_llm_response(content)
        repaired = _enforce_one_sentence_per_segment(repaired)
        return repaired or draft_segments
    except Exception:
        return draft_segments


def _parse_llm_response(raw: str) -> list[dict]:
    raw = raw.strip()
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

    return [{"text": raw, "emotion": "neutral", "action": None}]


class ChatRequest(BaseModel):
    text: str
    include_audio: bool = True
    json_response: bool = False


class ChatResponse(BaseModel):
    user_text: str
    assistant_text: str
    audio_url: Optional[str] = None
    memories_used: list[dict] = []


# ---------------------------------------------------------------------------
#  Debug state broadcasting
# ---------------------------------------------------------------------------

async def _broadcast_debug_state(phase: str, **extra):
    _pipeline_state["phase"] = phase
    _pipeline_state.update(extra)
    msg = {"type": "debug_state", "phase": phase, "relationship_tier": _relationship.get("tier", "stranger")}
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


async def _llm_chat(user_text: str, memories: list[dict], job_id: int = 0) -> list[dict]:
    await _monitor_thread_start("llm", input_preview=user_text, job_id=job_id)

    memory_context = ""
    if memories:
        memory_context = "\n\n[Relevant memories from past conversations]:\n"
        for m in memories:
            memory_context += f"- ({m['role']}): {m['text']}\n"

    # Inject relationship context
    memory_context += _get_relationship_prompt()

    system_prompt = build_system_prompt(
        memory_context,
        animation_mode=ANIMATION_MODE,
        animation_clips=_ANIMATION_CLIPS if ANIMATION_MODE == "llm_select" else None,
    )
    messages = [{"role": "system", "content": system_prompt}]

    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append(entry)

    messages.append({"role": "user", "content": user_text})

    try:
        body: dict = {
            "model": llm_config["model"],
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": llm_config.get("temperature", 0.8),
                "num_predict": llm_config.get("max_tokens", 4096),
            },
            # Keep model in VRAM between requests so KV cache stays hot.
            # Default is "5m"; set higher for always-on characters.
            "keep_alive": llm_config.get("keep_alive", "30m"),
        }
        if "ollama_think" in llm_config:
            body["think"] = llm_config["ollama_think"]
        else:
            body["think"] = False

        t0 = time.monotonic()
        resp = await http.post(f"{config['llm_url']}/api/chat", json=body)
        llm_ms = (time.monotonic() - t0) * 1000
        _pipeline_state["llm_ms"] = round(llm_ms, 1)

        if resp.status_code != 200:
            log.error("LLM HTTP %s: %s", resp.status_code, resp.text[:2000] if resp.text else "(empty)")
            return [dict(_LLM_FALLBACK)]
        data = resp.json()
        msg = data.get("message") or {}
        content = msg.get("content")
        if not content:
            log.error("LLM response missing message.content: %s", str(data)[:2000])
            return [dict(_LLM_FALLBACK)]

        segments = _parse_llm_response(content)
        if _needs_repair(segments):
            segments = await _llm_repair_segments(user_text, segments)
        segments = _enforce_one_sentence_per_segment(segments)
        segments = _drop_echo_segments(user_text, segments)
        for seg in segments:
            if not seg.get("action"):
                seg["action"] = _fallback_action(user_text, seg.get("emotion", "neutral"))
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

    return {**seg, "clip": clip_name, "audio": audio, "anim_ms": anim_ms, "tts_ms": tts_ms}


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
    _load_relationship()
    asyncio.create_task(_idle_heartbeat_loop())
    asyncio.create_task(_llm_warmup())


async def _llm_warmup():
    """Send a minimal silent request to Ollama on startup so the model is
    loaded into VRAM and the system-prompt KV cache is hot before the first
    real user interaction.  Typically saves 10-25 s off the first response."""
    await asyncio.sleep(3)          # let other services finish starting
    log.info("LLM warm-up: sending dummy request to pre-load model…")
    t0 = time.monotonic()
    try:
        body = {
            "model": llm_config["model"],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": {
                "temperature": 0,
                "num_predict": 1,   # generate only 1 token — we don't use the reply
            },
            "keep_alive": llm_config.get("keep_alive", "30m"),
            "think": False,
        }
        resp = await http.post(
            f"{config['llm_url']}/api/chat",
            json=body,
            timeout=60.0,
        )
        ms = (time.monotonic() - t0) * 1000
        if resp.status_code == 200:
            log.info("LLM warm-up done in %.0f ms — model is hot.", ms)
        else:
            log.warning("LLM warm-up got HTTP %s (%.0f ms).", resp.status_code, ms)
    except Exception as e:
        log.warning("LLM warm-up failed (non-fatal): %s", e)


@app.get("/", include_in_schema=False)
async def voice_chat_ui():
    html = STATIC_DIR / "index.html"
    if html.is_file():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(html.read_text())
    return JSONResponse({"error": "Voice chat UI not found"}, status_code=404)


@app.get("/health")
async def health():
    checks = {}
    for name, url in [
        ("stt", config["stt_url"]),
        ("tts", config["tts_url"]),
        ("memory", config["memory_url"]),
        ("animation", config["animation_url"]),
        ("llm", config["llm_url"]),
    ]:
        try:
            endpoint = f"{url}/health" if name != "llm" else f"{url}/api/tags"
            r = await http.get(endpoint, timeout=5.0)
            checks[name] = "ok" if r.status_code == 200 else f"error ({r.status_code})"
        except Exception as e:
            checks[name] = f"down ({e})"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", "services": checks}


@app.get("/debug/state")
async def debug_state():
    return {**_pipeline_state, "relationship": _relationship}


@app.get("/relationship")
async def get_relationship():
    return _relationship


def _segments_full_text(segments: list[dict]) -> str:
    return " ".join(s["text"] for s in segments)


# ---------------------------------------------------------------------------
#  POST /chat
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat(req: ChatRequest):
    _touch_interaction()
    jid = _new_job_id()
    await _broadcast_debug_state("thinking", last_input=req.text)

    memories = await _query_memories(req.text)
    segments = await _llm_chat(req.text, memories, job_id=jid)

    full_text = _segments_full_text(segments)
    if _llm_reply_ok(segments[0]["text"]):
        conversation_history.append({"role": "user", "content": req.text})
        conversation_history.append({"role": "assistant", "content": full_text})
        asyncio.create_task(_store_memory(req.text, "user"))
        asyncio.create_task(_store_memory(full_text, "assistant"))
        _tick_relationship(req.text, full_text)

    await _broadcast_debug_state("synthesizing", segment_count=len(segments),
                                  llm_ms=_pipeline_state["llm_ms"])
    enriched = await _process_segments(segments, include_audio=req.include_audio, job_id=jid)

    if unity_clients:
        await _broadcast_debug_state("speaking", segment_count=len(enriched),
                                      tts_ms=_pipeline_state["tts_ms"],
                                      anim_ms=_pipeline_state["anim_ms"])
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
            if seg.get("audio"):
                msg["audio_base64"] = base64.b64encode(seg["audio"]).decode()
            await _broadcast_to_unity(msg)

    await _broadcast_debug_state("idle")

    response = {
        "user_text": req.text,
        "assistant_text": full_text,
        "segments": [
            {
                "text": s["text"],
                "emotion": s["emotion"],
                "action": s["action"],
                "gesture": s["clip"],
                "audio_generated": bool(s.get("audio")),
            }
            for s in enriched
        ],
        "memories_used": memories,
        "relationship": {"tier": _relationship["tier"], "affection": _relationship["affection"]},
    }

    if req.include_audio:
        any_audio = any(s.get("audio") for s in enriched)
        response["audio_generated"] = any_audio
        if not any_audio:
            response["audio_hint"] = (
                "TTS returned no audio. Check: parrot-assistant/logs/tts.log, "
                "GET http://127.0.0.1:8002/health, and audio/reference_voice.wav."
            )

    if req.include_audio and not req.json_response:
        first_audio = next((s["audio"] for s in enriched if s.get("audio")), None)
        if first_audio:
            return StreamingResponse(
                io.BytesIO(first_audio),
                media_type="audio/wav",
                headers={
                    "X-Assistant-Text": full_text,
                    "X-Emotion": enriched[0]["emotion"],
                    "X-Gesture": enriched[0].get("clip") or "",
                    "X-Segment-Count": str(len(enriched)),
                },
            )

    return JSONResponse(response)


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
    segments = await _llm_chat(user_text, memories, job_id=jid)

    full_text = _segments_full_text(segments)
    if _llm_reply_ok(segments[0]["text"]):
        conversation_history.append({"role": "user", "content": user_text})
        conversation_history.append({"role": "assistant", "content": full_text})
        asyncio.create_task(_store_memory(user_text, "user"))
        asyncio.create_task(_store_memory(full_text, "assistant"))
        _tick_relationship(user_text, full_text)

    await _broadcast_debug_state("synthesizing", segment_count=len(segments))
    enriched = await _process_segments(segments, include_audio=True, job_id=jid)

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


# ---------------------------------------------------------------------------
#  WS /ws/unity — main Unity connection with barge-in support
# ---------------------------------------------------------------------------

@app.websocket("/ws/unity")
async def unity_ws(ws: WebSocket):
    global _active_generation
    await ws.accept()
    unity_clients.append(ws)
    log.info("Unity client connected. Total: %d", len(unity_clients))

    async def _handle_text_input(text: str, *, interrupted: bool = False):
        nonlocal _current_job_id
        _touch_interaction()
        jid = _new_job_id()
        _current_job_id = jid
        _timeline_init(jid)

        await ws.send_text(json.dumps({"type": "interrupt"}))
        await _broadcast_debug_state("thinking", last_input=text)

        await _broadcast_timeline_event(jid, "llm", "start")

        memories = await _query_memories(text)
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
        segments = await _llm_chat(text, memories, job_id=jid)
        await _broadcast_timeline_event(jid, "llm", "end")

        full_text = _segments_full_text(segments)
        if _llm_reply_ok(segments[0]["text"]):
            conversation_history.append({"role": "user", "content": text})
            conversation_history.append({"role": "assistant", "content": full_text})
            asyncio.create_task(_store_memory(text, "user"))
            asyncio.create_task(_store_memory(full_text, "assistant"))
            _tick_relationship(text, full_text)

        await _broadcast_debug_state("synthesizing", segment_count=len(segments),
                                      llm_ms=_pipeline_state["llm_ms"])

        total = len(segments)
        last_sent = time.monotonic()
        log.info("[timeline] Job %d: entering sequential TTS loop (%d segments)", jid, total)
        for idx, seg in enumerate(segments):
            log.info("[timeline] Job %d: TTS seg %d/%d start", jid, idx + 1, total)
            await _broadcast_timeline_event(jid, "tts", "start", segment=idx, total=total)
            await _monitor_thread_start("tts", input_preview=seg["text"], job_id=jid,
                                          segment=idx, total_segments=total)
            t_seg = time.monotonic()
            clip_task = asyncio.create_task(_resolve_action(seg["action"], seg["emotion"]))
            audio_task = asyncio.create_task(_synthesize(seg["text"]))
            clip_name, audio = await asyncio.gather(clip_task, audio_task)
            tts_ms = (time.monotonic() - t_seg) * 1000
            marginal_ms = (time.monotonic() - last_sent) * 1000
            log.info("[timeline] Job %d: TTS seg %d/%d done (%.0fms)", jid, idx + 1, total, tts_ms)

            await _monitor_thread_end("tts", elapsed_ms=tts_ms, input_preview=seg["text"],
                                        job_id=jid, segment=idx, total_segments=total)
            await _monitor_job_phase(jid, "tts", "end", input_preview=seg["text"],
                                       segment=idx, total_segments=total,
                                       elapsed_ms=tts_ms, marginal_ms=round(marginal_ms, 1))
            await _broadcast_timeline_event(jid, "tts", "end", segment=idx, total=total)

            response = {
                "type": "speech_segment",
                "job_id": jid,
                "index": idx,
                "total": total,
                "text": seg["text"],
                "emotion": seg["emotion"],
                "gesture": clip_name,
            }
            if audio:
                response["audio_base64"] = base64.b64encode(audio).decode()
            await ws.send_text(json.dumps(response))
            await _broadcast_timeline_event(jid, "sent_to_unity", "end", segment=idx, total=total)
            last_sent = time.monotonic()

            if idx == 0:
                await _broadcast_debug_state("speaking", segment_count=total,
                                              tts_ms=round(tts_ms, 1))

        await _broadcast_debug_state("idle")

    _current_job_id: int = 0

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
                        _active_generation = asyncio.create_task(_handle_text_input(text, interrupted=was_speaking))

                elif mtype in ("segment_play_start", "segment_play_end"):
                    seg_idx = msg.get("index", 0)
                    seg_total = msg.get("total", 0)
                    jid = int(msg.get("job_id") or 0) or _current_job_id
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
                _active_generation = asyncio.create_task(_handle_text_input(user_text, interrupted=True))

    except (WebSocketDisconnect, RuntimeError):
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if ws in unity_clients:
            unity_clients.remove(ws)
        log.info("Unity client disconnected. Total: %d", len(unity_clients))


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
            _touch_interaction()

            memories = await _query_memories(user_text)
            await _broadcast_timeline_event(jid, "llm", "start")
            segments = await _llm_chat(user_text, memories, job_id=jid)
            await _broadcast_timeline_event(jid, "llm", "end")

            full_text = _segments_full_text(segments)
            if _llm_reply_ok(segments[0]["text"]):
                conversation_history.append({"role": "user", "content": user_text})
                conversation_history.append({"role": "assistant", "content": full_text})
                asyncio.create_task(_store_memory(user_text, "user"))
                asyncio.create_task(_store_memory(full_text, "assistant"))
                _tick_relationship(user_text, full_text)

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

    try:
        while True:
            await ws.receive()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if ws in monitor_clients:
            monitor_clients.remove(ws)
        log.info("Monitor client disconnected. Total: %d", len(monitor_clients))


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
