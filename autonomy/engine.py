"""
Autonomy engine — decides when Mocha should speak up on her own.

States: IDLE → DRIFT_THINKING → BORED → LONELY, plus one-shot RECONNECT_HELLO
on fresh ``client_hello``.

Driven by the existing ``_idle_heartbeat_loop`` in ``bridge/server.py`` (one tick
every ~5s). We do not spawn our own background task. On each tick we check
rate-limits, decide the current state, and (sometimes) call the LLM to compose
a short autonomous utterance that's routed through ``bridge/channel_router``.

Autonomy turns NEVER append to ``conversation_history`` — they're observations,
not conversation. They DO get logged via ``call_log`` with ``triggered_by='autonomy'``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import time
from typing import Optional

log = logging.getLogger("autonomy")

# ---------------------------------------------------------------------------
#  Config (loaded lazily from config.yaml via bridge/server full_config)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "enabled": True,
    "drift_after_s": 45,
    "bored_after_s": 120,
    "lonely_after_s": 300,
    "eval_interval_s": 45,
    "min_interval_between_autonomous_turns_s": 180,
    "daily_max_autonomous_turns": 12,
    "reconnect_debounce_s": 300,
    "modes": {
        "drift": True,
        "bored": True,
        "lonely": True,
        "reconnect_hello": True,
    },
}


def _cfg() -> dict:
    """Merge user config over defaults on every access (picks up hot edits)."""
    try:
        from bridge.server import full_config
        user = full_config.get("autonomy") or {}
    except Exception:
        user = {}
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in user.items() if k != "modes"})
    modes = dict(_DEFAULTS["modes"])
    modes.update((user.get("modes") or {}))
    merged["modes"] = modes
    return merged


# ---------------------------------------------------------------------------
#  Internal state — eval timestamps (separate from _mocha_state)
# ---------------------------------------------------------------------------

_last_eval_monotonic: float = 0.0
_day_key: str = ""


def _maybe_reset_daily_counter() -> None:
    from bridge.server import _mocha_state
    global _day_key
    today = dt.date.today().isoformat()
    if today != _day_key:
        _day_key = today
        _mocha_state["autonomous_turns_today"] = 0


# ---------------------------------------------------------------------------
#  State classification
# ---------------------------------------------------------------------------

def _classify(elapsed_s: float, topic: str, cfg: dict) -> str:
    """Return 'idle' | 'drift' | 'bored' | 'lonely'."""
    if elapsed_s < cfg["drift_after_s"]:
        return "idle"
    if elapsed_s < cfg["bored_after_s"]:
        # Drift only makes sense if there was a substantive last topic.
        return "drift" if topic and len(topic) >= 12 else "idle"
    if elapsed_s < cfg["lonely_after_s"]:
        return "bored"
    return "lonely"


def _mood_for_state(state: str) -> str:
    return {
        "idle": "curious",
        "drift": "thinking",
        "bored": "playful",
        "lonely": "thoughtful",
    }.get(state, "neutral")


# ---------------------------------------------------------------------------
#  Prompt composition
# ---------------------------------------------------------------------------

def _build_mood_system_message(state: str, elapsed_s: float, topic: str) -> str:
    mood = _mood_for_state(state)
    topic_line = f'last_topic: "{topic}"' if topic else "last_topic: (none)"
    return (
        "[Inner state]\n"
        f"mood: {mood}\n"
        f"silence_duration_s: {int(elapsed_s)}\n"
        f"{topic_line}\n"
        "This is not a rule; it's what you're feeling right now. "
        "Let it color tone, not dominate content."
    )


def _internal_prompt_for_state(state: str, topic: str, findings_preview: str = "") -> str:
    if state == "drift":
        return (
            f"[autonomy-mode: drift] Ika has been silent on the topic "
            f"\"{topic}\" for a little while. Decide: add ONE short observation "
            f"or question, or return {{\"segments\":[]}} to stay silent. "
            f"Never ask 'are you still there'."
        )
    if state == "bored":
        tail = f" You can reference the last topic (\"{topic}\") if it's genuinely interesting." if topic else ""
        return (
            f"[autonomy-mode: bored] Ika has been quiet for a few minutes while "
            f"still around. ONE short sentence. Light, curious, not needy. "
            f"Never say 'Hello?' or 'Are you there?'.{tail}"
        )
    if state == "lonely":
        return (
            "[autonomy-mode: lonely] Ika has been silent for a long time but is "
            "still here. ONE short sentence. Slightly more emotionally honest "
            "('It's been quiet, huh' is fine). Not whiny. Don't beg."
        )
    if state == "reconnect":
        base = (
            "[autonomy-mode: reconnect] Ika just came back. Greet briefly by name."
        )
        if findings_preview:
            base += (
                f" Pending findings from while they were away:\n{findings_preview}\n"
                "Mention 1-2 headlines only, end with a hook question."
            )
        else:
            base += " No pending findings. Welcome them back in one or two sentences, end with a hook."
        return base
    if state == "first_hello":
        return (
            "[autonomy-mode: first_hello] Someone just opened your window for the "
            "very first time — you've never met. No prior conversation, no name yet. "
            "Greet them in one short, warm sentence (your voice, not assistant-coded), "
            "and ask what to call them. Don't introduce yourself with a long bio. "
            "Don't say 'Hello!' — just sound like a person who noticed someone walked in."
        )
    return "[autonomy] say something short and natural."


# ---------------------------------------------------------------------------
#  LLM invocation
# ---------------------------------------------------------------------------

async def _compose_utterance(state: str, topic: str, elapsed_s: float,
                              findings_preview: str = "") -> list[dict]:
    """Ask the LLM for segments. Empty list means 'stay silent'.

    Returns a list of pseudo-segment dicts ``{text, emotion, gesture}``
    parsed from the inline-tag output.
    """
    from bridge.server import (
        build_system_prompt, llm_client, ANIMATION_MODE,
        conversation_history, MAX_HISTORY, _new_job_id,
        call_log,
    )
    from bridge.call_log import CallContext
    from bridge.inline_tag_parser import InlineTagParser

    system_prompt = build_system_prompt(animation_mode=ANIMATION_MODE)
    mood_msg = _build_mood_system_message(state, elapsed_s, topic)
    internal_prompt = _internal_prompt_for_state(state, topic, findings_preview)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": mood_msg},
    ]
    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": internal_prompt})

    jid = _new_job_id()
    ctx = CallContext(triggered_by="autonomy", conversation_id=str(jid),
                      source=f"autonomy:{state}")

    try:
        t0 = time.monotonic()
        # enable_thinking=False is critical: autonomy turns are short ("one sentence
        # check-in") and Qwen's <think> block frequently gets cut off mid-thought at
        # low max_tokens, leaving a stray <think> without </think> — which then
        # survives _parse_llm_response and leaks the reasoning into TTS.
        result = await llm_client.chat(messages, max_tokens=384, enable_thinking=False)
        llm_ms = (time.monotonic() - t0) * 1000
    except Exception as exc:
        log.warning("autonomy LLM call failed: %s", exc)
        return []

    asyncio.create_task(call_log.log_call(
        ctx, model=llm_client.model,
        temperature=llm_client.default_temperature,
        max_tokens=128, stream=False, tools_provided=False, messages=messages,
        response_content=result.get("content"),
        response_tool_calls=result.get("tool_calls"),
        finish_reason=result.get("finish_reason"),
        error=result.get("_error"),
        latency_ms=llm_ms,
        prompt_tokens=(result.get("usage") or {}).get("prompt_tokens"),
        completion_tokens=(result.get("usage") or {}).get("completion_tokens"),
        total_tokens=(result.get("usage") or {}).get("total_tokens"),
    ))

    content = (result.get("content") or "").strip()
    if not content:
        return []

    # Parse inline-tag output → build pseudo-segments grouped by emotion/gesture.
    parser = InlineTagParser()
    events = parser.feed(content) + parser.finish()
    cur_text: list[str] = []
    cur_emotion = "neutral"
    cur_gesture = ""
    segments: list[dict] = []

    def _flush():
        t = "".join(cur_text).strip()
        if t:
            segments.append({"text": t, "emotion": cur_emotion, "gesture": cur_gesture,
                             "action": cur_gesture})
        cur_text.clear()

    for ev in events:
        kind = ev["kind"]
        if kind == "text_delta":
            cur_text.append(ev["text"])
        elif kind == "flush":
            _flush()
        elif kind == "emotion":
            _flush()
            cur_emotion = ev["id"]
        elif kind == "gesture":
            _flush()
            cur_gesture = ev["name"]
    _flush()

    segments = _post_filter(segments)
    return segments


_FORBIDDEN_PHRASES = (
    "hello?",
    "are you there",
    "are you still there",
    "you still around",
    "are you around",
)


def _post_filter(segments: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        if any(bad in low for bad in _FORBIDDEN_PHRASES):
            log.info("autonomy: dropping forbidden phrase: %r", text)
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
#  Delivery
# ---------------------------------------------------------------------------

async def _deliver(segments: list[dict], state: str) -> bool:
    if not segments:
        return False
    from bridge.channel_router import route_autonomous

    # Join segments into one utterance; the router handles single speech_segment.
    text = " ".join((s.get("text") or "").strip() for s in segments if s.get("text")).strip()
    if not text:
        return False
    emotion = segments[0].get("emotion") or _mood_for_state(state)
    action = segments[0].get("action") or ""

    where = await route_autonomous({
        "text": text,
        "emotion": emotion,
        "action": action,
        "autonomous": True,
        "source": f"autonomy:{state}",
        "kind": f"autonomy_{state}",
    })
    log.info("autonomy %s → %s: %s", state, where, text[:120])
    try:
        from bridge.server import _broadcast_agent_thought
        await _broadcast_agent_thought(
            source="autonomy", kind=f"speak_{state}",
            text=text, extra={"route": where},
        )
    except Exception:
        pass
    return where != "empty"


def _mark_spoke() -> None:
    from bridge.server import _mocha_state
    _mocha_state["last_autonomous_spoke_at"] = time.monotonic()
    _mocha_state["autonomous_turns_today"] = int(
        _mocha_state.get("autonomous_turns_today", 0)
    ) + 1


# ---------------------------------------------------------------------------
#  Tick — called from _idle_heartbeat_loop
# ---------------------------------------------------------------------------

async def decide_tick() -> None:
    """One heartbeat: evaluate state, maybe speak."""
    global _last_eval_monotonic
    cfg = _cfg()
    if not cfg["enabled"]:
        return

    from bridge.server import _mocha_state, _last_interaction_time, _ws_clients

    _maybe_reset_daily_counter()

    now = time.monotonic()

    # Muted?
    if now < _mocha_state.get("muted_until_monotonic", 0.0):
        return
    # Daily ceiling?
    if int(_mocha_state.get("autonomous_turns_today", 0)) >= cfg["daily_max_autonomous_turns"]:
        return
    # Min interval between turns?
    last_spoke = _mocha_state.get("last_autonomous_spoke_at", 0.0)
    if last_spoke and (now - last_spoke) < cfg["min_interval_between_autonomous_turns_s"]:
        return
    # Min interval between eval LLM calls?
    if _last_eval_monotonic and (now - _last_eval_monotonic) < cfg["eval_interval_s"]:
        return

    # Task-in-flight guard: don't interrupt an active exchange. If a tool
    # ran in the last `task_quiet_after_s` seconds, the user is still mid-task
    # and drift would cut in rudely. We consult the PG call log timestamps
    # lazily; for now use _last_tool_at (a simple monotonic mirror).
    quiet_required = float(cfg.get("task_quiet_after_s", 180.0))
    last_tool_at = _mocha_state.get("last_tool_at_monotonic", 0.0)
    if last_tool_at and (now - last_tool_at) < quiet_required:
        return

    elapsed = now - _last_interaction_time
    topic = _mocha_state.get("last_topic_summary") or ""
    state = _classify(elapsed, topic, cfg)

    if state == "idle":
        return

    mode_cfg = cfg["modes"]
    if state == "drift" and not mode_cfg.get("drift", True):
        return
    if state == "bored" and not mode_cfg.get("bored", True):
        return
    if state == "lonely" and not mode_cfg.get("lonely", True):
        return

    # For drift, 40% chance to actually evaluate (the state's whole point is
    # "sometimes don't even think about it"). Bored/lonely always evaluate
    # (the cooldown above keeps them rare anyway).
    if state == "drift" and random.random() > 0.4:
        return

    # Require a surface (web or telegram) where the output could land.
    from bridge.channel_router import load_primary_user
    primary = load_primary_user() or {}
    if not _ws_clients and not primary.get("telegram_user_id"):
        log.debug("autonomy tick: no surface available (no web, no telegram) — skip")
        return

    _last_eval_monotonic = now

    segments = await _compose_utterance(state, topic, elapsed)
    spoke = await _deliver(segments, state)
    if spoke:
        _mark_spoke()
        _mocha_state["mood"] = _mood_for_state(state)


# ---------------------------------------------------------------------------
#  Reconnect hello — called from /ws/live on client_hello
# ---------------------------------------------------------------------------

async def handle_client_hello(user_id: str | None = None,
                              is_new_user: bool = False) -> None:
    cfg = _cfg()
    if not cfg["enabled"] or not cfg["modes"].get("reconnect_hello", True):
        return

    from bridge.server import _mocha_state
    from bridge import notifications

    now = time.monotonic()

    # Muted? Skip both reconnect and first_hello.
    if now < _mocha_state.get("muted_until_monotonic", 0.0):
        return

    # Brand-new user (anon, never named) → fire a warm first-meeting greeting
    # immediately, bypassing the reconnect debounce. Otherwise the standard
    # rage-refresh cooldown applies.
    if is_new_user:
        log.info("autonomy: first_hello for new user uid=%s", (user_id or "?")[:8])
        segments = await _compose_utterance("first_hello", "", elapsed_s=0.0)
        if not segments:
            return
        delivered = await _deliver(segments, "first_hello")
        if delivered:
            _mocha_state["last_hello_at"] = now
            _mark_spoke()
            _mocha_state["mood"] = "curious"
        return

    # Returning-user reconnect path — debounce rage-refreshes.
    if (now - _mocha_state.get("last_hello_at", 0.0)) < cfg["reconnect_debounce_s"]:
        log.info("autonomy: hello debounced")
        return

    pending = await notifications.list_undelivered()
    # Cap to the 2 most recent items for the preview — leave the rest queued.
    preview_items = pending[-2:]
    findings_preview = ""
    if preview_items:
        findings_preview = "\n".join(
            f"- {(it.get('summary') or '')[:160]}" for it in preview_items
        )

    segments = await _compose_utterance("reconnect", _mocha_state.get("last_topic_summary") or "",
                                         elapsed_s=0.0, findings_preview=findings_preview)
    if not segments:
        return

    delivered = await _deliver(segments, "reconnect")
    if delivered:
        _mocha_state["last_hello_at"] = now
        _mark_spoke()
        _mocha_state["mood"] = "happy"
        if preview_items:
            await notifications.mark_delivered([it["id"] for it in preview_items])
