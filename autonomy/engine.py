"""
Autonomy engine — decides when Mocha should speak up on her own.

States: IDLE → DRIFT_THINKING → BORED → LONELY, plus one-shot RECONNECT_HELLO
on fresh ``client_hello``.

Driven by the existing ``_idle_heartbeat_loop`` in ``bridge/server.py`` (one tick
every ~5s). We do not spawn our own background task. On each tick we check
rate-limits, decide the current state, and (sometimes) call the LLM to compose
a short autonomous utterance that's routed through ``bridge/channel_router``.

Delivered autonomy turns ARE appended to ``conversation_history`` (as assistant
turns) so that when Ika replies to something Mocha said on her own, the next
conversational turn has the context — otherwise she's blind to her own proactive
lines (e.g. she mentions Micron, Ika says "micron??", and she has no idea why).
They are also logged via ``call_log`` with ``triggered_by='autonomy'``. The
composer keeps its own separate anti-repeat ledger (data/autonomy_state.json).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("autonomy")

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
#  Config (loaded lazily from config.yaml via bridge/server full_config)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "enabled": True,
    "drift_after_s": 45,
    "bored_after_s": 120,
    "lonely_after_s": 300,
    "eval_interval_s": 90,
    # Hard floor between any two *spoken* autonomous turns (news or check-in).
    # Raised from 180 → 600 so proactive turns are genuinely rare.
    "min_interval_between_autonomous_turns_s": 600,
    "daily_max_autonomous_turns": 10,
    # Probability that a bored/lonely *fallback check-in* (only reached when the
    # curiosity pool is empty AND allow_empty_checkins is on) actually fires.
    "checkin_probability": 0.5,
    # Presence gate: Mocha is only proactive once Ika has been silent (no message,
    # no in-flight turn) for this long. The moment Ika speaks she's CONVERSING and
    # holds her tongue; after idle_after_s of quiet she returns to IDLE and may
    # surface news. See autonomy/presence.py. This is the load-bearing "don't talk
    # over me" knob (it replaced the never-armed last_tool_at guard).
    "idle_after_s": 600,
    # When the curiosity pool is empty, may she still fire a content-free check-in?
    # Default OFF: she only speaks unprompted when she has a genuinely fresh item,
    # never ambient filler.
    "allow_empty_checkins": False,
    # Reconnect "welcome back" greetings are gated by ONE thing: a 30-min debounce,
    # persisted as wall-clock so it survives bridge restarts (a flurry of reconnects
    # — tab focus, network blips, deploys — can't spam greetings). No daily cap, so
    # an active day of real returns always gets greeted. They draw on a counter
    # separate from news/check-ins so a greeting never starves the news budget.
    "reconnect_debounce_s": 1800,
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

# Persistent autonomy state. The old daily counter lived only in ``_mocha_state``
# (in-memory), so every bridge restart reset it to 0 and handed Mocha a fresh
# budget — on a churny day (e.g. a migration) that turned a 12/day ceiling into
# 70+ check-ins. We persist it to disk so the cap actually survives restarts, and
# piggy-back a short ledger of recent autonomous utterances so the composer can
# avoid repeating itself (autonomy turns never enter conversation_history, so her
# normal anti-repetition rule is blind to them).
_STATE_PATH = ROOT / "data" / "autonomy_state.json"
_RECENT_MAX = 12
_persist: Optional[dict] = None


def _load_persist() -> dict:
    try:
        if _STATE_PATH.exists():
            d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            d.setdefault("day", "")
            d.setdefault("turns", 0)            # news / check-in budget
            d.setdefault("reconnect_turns", 0)  # separate "welcome back" budget
            d.setdefault("recent", [])
            return d
    except Exception as exc:
        log.warning("autonomy: failed to read state (%s) — starting fresh", exc)
    return {"day": "", "turns": 0, "reconnect_turns": 0, "recent": []}


def _save_persist() -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(_persist, ensure_ascii=False),
                               encoding="utf-8")
    except Exception as exc:
        log.warning("autonomy: failed to save state: %s", exc)


def _get_persist() -> dict:
    global _persist
    if _persist is None:
        _persist = _load_persist()
    return _persist


def _recent_utterances() -> list[str]:
    return list(_get_persist().get("recent", []))


def _maybe_reset_daily_counter() -> None:
    """Roll the persisted counter over at local midnight, and mirror the live
    count into ``_mocha_state`` so the existing cap check stays authoritative
    across restarts."""
    from bridge.server import _mocha_state
    p = _get_persist()
    today = dt.date.today().isoformat()
    if today != p.get("day"):
        p["day"] = today
        p["turns"] = 0
        p["reconnect_turns"] = 0
        p["recent"] = []
        _save_persist()
    # Restart-proofing: seed the in-memory mirror from the persisted truth.
    _mocha_state["autonomous_turns_today"] = int(p.get("turns", 0))
    _mocha_state["reconnect_turns_today"] = int(p.get("reconnect_turns", 0))


def _greeting_debounced(cfg: dict) -> bool:
    """Has a welcome-back greeting fired within reconnect_debounce_s?

    The timestamp is persisted as wall-clock so the debounce survives bridge
    restarts — a deploy storm can't re-greet on every reconnect. This is the
    SOLE rate limit on greetings (no daily cap), so an active day of logins
    always gets a hello while rapid refreshes inside the window stay quiet."""
    last = float(_get_persist().get("last_hello_epoch", 0.0))
    return (time.time() - last) < float(cfg["reconnect_debounce_s"])


def _stamp_greeting() -> None:
    """Persist 'a greeting just fired' (wall-clock). Call BEFORE the
    compose/deliver await so two near-simultaneous hellos can't both pass."""
    p = _get_persist()
    p["last_hello_epoch"] = time.time()
    _save_persist()


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


def _pnl_mood_message() -> Optional[str]:
    """The desk-P&L inner-state line (or None). Shared with the conversational
    graph so Mocha's mood is the same whether she's chatting or drifting."""
    try:
        from bridge import pnl_mood
        return pnl_mood.mood_system_message()
    except Exception:
        return None


def _internal_prompt_for_state(state: str, topic: str, findings_preview: str = "") -> str:
    if state == "drift":
        return (
            f"[autonomy-mode: drift] Ika went quiet on \"{topic}\" a moment ago. "
            f"If you have ONE genuine new thought about it — an angle, a small "
            f"question, something it reminded you of — say it in one short line. "
            f"Riff on the FEELING or your own angle, NOT new facts: do not introduce "
            f"any news, company, number, or claim that wasn't already said. "
            f"Otherwise return {{\"segments\":[]}} and stay silent. Do NOT comment "
            f"on the silence or ask if they're there."
        )
    if state == "bored":
        tail = f" The last thing you two touched was \"{topic}\" — only pick it back up if it's genuinely worth it." if topic else ""
        return (
            "[autonomy-mode: bored] Offer ONE short, genuine thing — a stray "
            "observation, a half-finished thought, something you actually find "
            "interesting right now. Light, not needy. Keep it to feelings, opinions, "
            "or a callback to this conversation — do NOT state any news, company, "
            "ticker, number, date, or current event (you have no live data here). "
            "Do NOT mention that it's quiet, that time has passed, or that you're "
            "waiting — that's the lazy line. If nothing real comes to mind, return "
            "{\"segments\":[]}."
            + tail
        )
    if state == "lonely":
        return (
            "[autonomy-mode: lonely] It's been a long quiet stretch but Ika is "
            "still around. Your tone can be a touch more honest, but say something "
            "with actual content — a thought, a small confession, a question "
            "that's genuinely new. Keep it to inner-state, opinion, or a callback "
            "to this conversation — do NOT state any news, company, number, date, "
            "or current event (you have no live data). Do NOT remark on the silence, "
            "the quiet, or the waiting (that is the lazy, repetitive line — avoid it "
            "entirely). One short sentence, or stay silent: {\"segments\":[]}."
        )
    if state == "reconnect":
        base = (
            "[autonomy-mode: reconnect] Ika just came back. Greet him by name in one "
            "short, warm sentence — like a person glad to see him, not an assistant."
        )
        if findings_preview:
            base += (
                "\nReal, recent things you ACTUALLY found or shared (your ONLY "
                f"permitted hooks — quote their substance, don't embellish):\n{findings_preview}\n"
                "You MAY pick up ONE of these as a light, natural hook, in your own "
                "words."
            )
        else:
            base += (
                " You have NO current news or findings. Do NOT reference any news, "
                "market event, company, earnings, report, or 'recent' anything — you "
                "have nothing to cite, and inventing one is a lie. A callback to your "
                "last topic is fine; otherwise just be glad he's back."
            )
        base += (
            "\nHARD RULE: if you're about to name a company, ticker, earnings result, "
            "economic report, study, or number that is NOT explicitly written above, "
            "you are hallucinating it — drop it. Never imply you read news you didn't."
        )
        return base
    if state == "first_hello":
        return (
            "[autonomy-mode: first_hello] Ika just opened your window. Greet him by "
            "name in one short, warm sentence (your voice, not assistant-coded). You "
            "already know who he is — don't ask his name. Don't introduce yourself "
            "with a long bio, don't say 'Hello!', and don't mention any news, company, "
            "or number — you have nothing to cite. Just sound glad he showed up."
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
        build_system_prompt, ANIMATION_MODE,
        conversation_history, MAX_HISTORY,
    )

    system_prompt = build_system_prompt(animation_mode=ANIMATION_MODE)
    mood_msg = _build_mood_system_message(state, elapsed_s, topic)
    internal_prompt = _internal_prompt_for_state(state, topic, findings_preview)

    recent = _recent_utterances()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": mood_msg},
    ]
    pnl_msg = _pnl_mood_message()
    if pnl_msg:
        messages.append({"role": "system", "content": pnl_msg})
    if recent:
        messages.append({"role": "system", "content": _avoid_message(recent)})
    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": internal_prompt})

    return await _llm_to_segments(messages, source=f"autonomy:{state}",
                                  recent_avoid=recent)


async def _compose_news_share(item: dict) -> list[dict]:
    """Compose Mocha's in-voice reaction to one interesting news item.

    Returns pseudo-segments (or [] if she chose silence / the call failed).
    """
    from bridge.server import (
        build_system_prompt, ANIMATION_MODE,
        conversation_history, MAX_HISTORY,
    )

    system_prompt = build_system_prompt(animation_mode=ANIMATION_MODE)
    # A find reads as a curious/thinking beat regardless of how deep the idle was.
    mood_msg = _build_mood_system_message("drift", 0.0, item.get("title", ""))

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": mood_msg},
    ]
    pnl_msg = _pnl_mood_message()
    if pnl_msg:
        messages.append({"role": "system", "content": pnl_msg})
    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": _news_share_prompt(item)})

    # Knowledge-graph garnish: if the item names a company we have a cited
    # relationship for, hand Mocha that ONE grounded fact (read-only proxy).
    kg_line = await _kg_news_annotation(item)
    if kg_line:
        messages.insert(len(messages) - 1, {"role": "system", "content": (
            "[Knowledge graph — a CITED relationship from the desk's shared graph; "
            "trust this over guessing. Weave it into ONE clause ONLY if it's relevant "
            "to the item; do not read the evidence id aloud]:\n- " + kg_line)})

    return await _llm_to_segments(messages, source="autonomy:news")


# All-caps tokens that look like tickers but aren't — keeps the regex fallback
# from annotating a news item with an unrelated company's relationships.
_TICKER_STOPWORDS = frozenset({
    "AI", "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "GDP", "CPI", "FED", "SEC",
    "FDA", "USA", "EPS", "NYSE", "ESG", "SPAC", "YOY", "FY", "USD", "API", "EV",
    "AND", "THE", "FOR", "NEW", "Q1", "Q2", "Q3", "Q4",
})


async def _kg_news_annotation(item: dict) -> str | None:
    """One-line CITED KG relationship for the company in a news item, via the
    read-only opus proxy (kg_neighbors). Fail-silent + bounded — never blocks the
    autonomy heartbeat and never writes the desk DB. Returns None when there's no
    confidently-identified ticker or no grounded relationship to add."""
    try:
        import json
        import re
        ent = (item.get("symbol") or item.get("ticker") or "").strip().upper()
        if not ent:   # fallback: a clean ALL-CAPS token in the title, minus stopwords
            for m in re.findall(r"\b([A-Z]{2,5})\b", item.get("title") or ""):
                if m not in _TICKER_STOPWORDS:
                    ent = m
                    break
        if not ent:
            return None
        from tools.custom._opus_proxy import call_opus
        out = await call_opus("kg_neighbors", {"entity": ent, "caller": "mocha"}, "Knowledge graph")
        obj = json.loads(out)
        if obj.get("__panel__") or "error" in obj or not obj.get("found") or not obj.get("edges"):
            return None
        e = obj["edges"][0]   # highest-confidence edge (kg_query sorts)
        subj = e.get("subject_ticker") or e.get("subject")
        objn = e.get("object_ticker") or e.get("object")
        ev = e.get("evidence_id")
        cite = f" [desk evidence #{ev}]" if ev else ""
        return f"{subj} {e.get('rel')} {objn}{cite}"
    except Exception as exc:  # noqa: BLE001 — annotation is best-effort
        log.info("autonomy: kg news annotation skipped: %s", exc)
        return None


def _avoid_message(recent: list[str]) -> str:
    lines = "\n".join(f"- {r}" for r in recent[-_RECENT_MAX:])
    return (
        "[Don't repeat yourself] You've recently said these unprompted lines:\n"
        f"{lines}\n"
        "Say something different in substance AND wording, or stay silent. Do "
        "not paraphrase any of the above."
    )


def _news_share_prompt(item: dict) -> str:
    title = (item.get("title") or "").strip()
    snippet = (item.get("snippet") or "").strip()
    source = (item.get("source") or "").strip()
    kind = item.get("kind") or "self"
    flavor = ("It's one of those small-but-huge things you like."
              if kind == "self"
              else "It's from the markets/world the trading desk lives in.")
    material = f'"{title}"'
    if snippet:
        material += f" — {snippet}"
    if source:
        material += f" (via {source})"
    return (
        "[autonomy-mode: share-find] You drifted off and noticed something. "
        f"{flavor}\n{material}\n"
        "React in ONE or two short sentences, in your own voice — what's actually "
        "interesting or strange about it, not a summary. State ONLY what's in the "
        "headline/snippet above — do not invent figures, outcomes (beat/missed), or "
        "context that isn't there. End with a small hook that invites Ika in. Don't "
        "read the URL, don't say 'I read an article', and don't recite the headline "
        "word-for-word. If it's genuinely dull, return {\"segments\":[]}."
    )


async def _llm_to_segments(messages: list[dict], source: str,
                           max_tokens: int = 384,
                           recent_avoid: Optional[list[str]] = None) -> list[dict]:
    """Shared tail for autonomy composers: call the LLM (thinking off), log it,
    parse inline tags into pseudo-segments, post-filter. [] = silence / failure."""
    from bridge.server import llm_client, _new_job_id, call_log
    from bridge.call_log import CallContext
    from bridge.inline_tag_parser import InlineTagParser

    jid = _new_job_id()
    ctx = CallContext(triggered_by="autonomy", conversation_id=str(jid),
                      source=source)
    try:
        t0 = time.monotonic()
        # enable_thinking=False is critical: autonomy turns are short and Qwen's
        # <think> block frequently gets cut off mid-thought at low max_tokens,
        # leaving a stray <think> without </think> that leaks into TTS.
        result = await llm_client.chat(messages, max_tokens=max_tokens,
                                       enable_thinking=False)
        llm_ms = (time.monotonic() - t0) * 1000
    except Exception as exc:
        log.warning("autonomy LLM call failed: %s", exc)
        return []

    asyncio.create_task(call_log.log_call(
        ctx, model=llm_client.model,
        temperature=llm_client.default_temperature,
        max_tokens=max_tokens, stream=False, tools_provided=False, messages=messages,
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

    # Some prompts invite a {"segments": [...]} JSON reply (the same shape used to
    # signal silence with []). The model frequently takes that literally — even to
    # SPEAK — so handle it before inline-tag parsing, else the raw JSON object
    # leaks into TTS verbatim as her utterance.
    seg_json = _try_parse_segments_json(content)
    if seg_json is not None:
        return _post_filter(seg_json, recent_avoid)

    # Parse inline-tag output → pseudo-segments grouped by emotion/gesture.
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

    return _post_filter(segments, recent_avoid)


_FORBIDDEN_PHRASES = (
    "hello?",
    "are you there",
    "are you still there",
    "you still around",
    "are you around",
    # The content-free "comment on the silence" filler that dominated idle turns.
    # These are the exact lazy lines the de-seeded prompts now forbid; we also
    # drop them defensively in case the model reaches for one anyway.
    "it's been quiet",
    "it's quiet",
    "been pretty quiet",
    "bit quiet",
    "quiet in here",
    "quiet around here",
    "quiet, huh",
    "it's been a while",
    "been a while",
    "time's passed",
    "awkward pause",
    "waiting for something",
    "feels a bit quiet",
)


def _normalize(text: str) -> set:
    """Lowercased word-set for cheap near-duplicate detection."""
    import re
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _is_near_dup(text: str, recent: list[str]) -> bool:
    """True if ``text`` overlaps heavily (Jaccard ≥ 0.5) with anything recent —
    a backstop for re-paraphrases the prompt-level avoid-list didn't stop. The
    bias is intentionally toward variety: a borderline match means she stays
    silent rather than echoing herself, which is exactly what we want."""
    a = _normalize(text)
    if not a:
        return False
    for prev in recent:
        b = _normalize(prev)
        if not b:
            continue
        inter = len(a & b)
        union = len(a | b)
        if union and inter / union >= 0.5:
            return True
    return False


def _try_parse_segments_json(content: str) -> Optional[list[dict]]:
    """If the model replied with a ``{"segments": [...]}`` object (the shape the
    prompts use for the silence signal, which the model also reaches for to
    speak), return pseudo-segments: ``[]`` for silence, a populated list for
    speech. Returns ``None`` when the content isn't that shape, so the caller
    falls back to inline-tag parsing."""
    s = content.strip()
    if s.startswith("```"):                      # strip a ```json fence
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    if '"segments"' not in s and "'segments'" not in s:
        return None
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    obj = None
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                obj = s[start:i + 1]
                break
    if not obj:
        return None
    try:
        data = json.loads(obj)
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        return None

    out: list[dict] = []
    for it in data["segments"]:
        if isinstance(it, str):
            t = it.strip()
            if t:
                out.append({"text": t, "emotion": "neutral", "gesture": "", "action": ""})
        elif isinstance(it, dict):
            t = (it.get("text") or "").strip()
            if t:
                g = it.get("gesture") or it.get("action") or ""
                out.append({"text": t, "emotion": it.get("emotion") or "neutral",
                            "gesture": g, "action": g})
    return out  # [] => silence


def _post_filter(segments: list[dict],
                 recent_avoid: Optional[list[str]] = None) -> list[dict]:
    out: list[dict] = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        import re as _re
        if not _re.search(r"[A-Za-z]{4,}", text):
            # No real word (≥4 letters) — a malformed fragment like "353 hed?".
            log.info("autonomy: dropping garbage fragment: %r", text)
            continue
        if any(bad in low for bad in _FORBIDDEN_PHRASES):
            log.info("autonomy: dropping silence-filler phrase: %r", text)
            continue
        if recent_avoid and _is_near_dup(text, recent_avoid):
            log.info("autonomy: dropping near-duplicate of a recent line: %r", text)
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
#  Delivery
# ---------------------------------------------------------------------------

async def _deliver(segments: list[dict], state: str, *,
                   connect_triggered: bool = False) -> dict:
    """Route a composed utterance. Returns
    ``{"spoke": bool, "tg_msg_id": int|None, "route": str}`` — tg_msg_id is the
    Telegram message_id when the share landed on Telegram (so the caller can
    record it against the article it referred to)."""
    result = {"spoke": False, "tg_msg_id": None, "route": "empty"}
    if not segments:
        return result
    from bridge.channel_router import route_autonomous

    # Join segments into one utterance; the router handles single speech_segment.
    text = " ".join((s.get("text") or "").strip() for s in segments if s.get("text")).strip()
    # Deterministic scrub: autonomous/idle messages bypass the conversational
    # verifier, so this is their guard against a leaked {"segments":…}/think/tag
    # artifact reaching Telegram.
    try:
        from bridge.server import _sanitize_outgoing
        text = _sanitize_outgoing(text)
    except Exception:
        pass
    if not text:
        return result
    emotion = segments[0].get("emotion") or _mood_for_state(state)
    action = segments[0].get("action") or ""

    payload = {
        "text": text,
        "emotion": emotion,
        "action": action,
        "autonomous": True,
        "source": f"autonomy:{state}",
        "kind": f"autonomy_{state}",
    }
    if connect_triggered:
        # Fired by a fresh web connect → deliver to the just-connected tab even if
        # the 2h "web attended" window has lapsed (the user is here, looking now).
        payload["connect_triggered"] = True
    where = await route_autonomous(payload)
    result["route"] = where
    result["tg_msg_id"] = payload.get("telegram_message_id")
    result["spoke"] = where != "empty"
    if result["spoke"]:
        # Make proactive utterances visible to the NEXT conversational turn, so
        # when Ika replies to something Mocha said on her own ("micron??") she has
        # the context. (Autonomy used to be excluded from conversation_history,
        # which left her blind to her own proactive lines.) The composer's
        # separate anti-repeat ledger is unaffected.
        try:
            from bridge.server import _append_history, IKA_USER_ID
            _append_history(IKA_USER_ID, "assistant", text)
        except Exception:
            pass
    log.info("autonomy %s → %s: %s", state, where, text[:120])
    try:
        from bridge.server import _broadcast_agent_thought
        await _broadcast_agent_thought(
            source="autonomy", kind=f"speak_{state}",
            text=text, extra={"route": where},
        )
    except Exception:
        pass
    return result


def _segments_text(segments: list[dict]) -> str:
    return " ".join((s.get("text") or "").strip()
                    for s in segments if s.get("text")).strip()


def _mark_spoke(text: str = "", *, reconnect: bool = False) -> None:
    from bridge.server import _mocha_state
    _mocha_state["last_autonomous_spoke_at"] = time.monotonic()
    # Persisted truth (survives restarts); mirror into _mocha_state for the cap
    # checks. Reconnect greetings draw on their OWN budget so they never consume
    # (or get blocked by) the news/check-in budget.
    p = _get_persist()
    if reconnect:
        p["reconnect_turns"] = int(p.get("reconnect_turns", 0)) + 1
    else:
        p["turns"] = int(p.get("turns", 0)) + 1
    if text:
        recent = p.setdefault("recent", [])
        recent.append(text)
        del recent[:-_RECENT_MAX]
    _save_persist()
    _mocha_state["autonomous_turns_today"] = int(p.get("turns", 0))
    _mocha_state["reconnect_turns_today"] = int(p.get("reconnect_turns", 0))


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

    # Presence gate — the load-bearing "don't talk over me" check. Mocha is only
    # proactive while Ika is IDLE: no conversational turn in flight AND no message
    # within idle_after_s. The instant Ika speaks she's CONVERSING and silent;
    # after idle_after_s of quiet she's IDLE again. This is an explicit state
    # machine (autonomy/presence.py) that replaced the never-armed last_tool_at
    # guard.
    from autonomy import presence
    if not presence.is_idle(float(cfg.get("idle_after_s", 600.0))):
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

    # Require a surface (web or telegram) where the output could land — checked
    # BEFORE any LLM/news work so we never burn the Brave API or the shared vLLM
    # when nobody's listening.
    from bridge.channel_router import load_primary_user
    primary = load_primary_user() or {}
    if not _ws_clients and not primary.get("telegram_user_id"):
        log.debug("autonomy tick: no surface available (no web, no telegram) — skip")
        return

    _last_eval_monotonic = now

    # 1) Prefer surfacing something genuinely interesting over content-free filler.
    #    The pool refills on its own throttle; next_item() returns None when the
    #    pool is empty or curiosity is disabled, in which case we fall through to
    #    a (rare) check-in. A dull item she declines to share is simply dropped —
    #    it doesn't burn a spoken turn.
    try:
        from autonomy import curiosity
        await curiosity.maybe_refill()
        item = await curiosity.next_item()
    except Exception as exc:
        log.warning("autonomy: curiosity lookup failed: %s", exc)
        item = None
    if item is not None:
        segments = await _compose_news_share(item)
        take = _segments_text(segments)
        res = await _deliver(segments, "news")
        if res["spoke"]:
            _mark_spoke(take)
            _mocha_state["mood"] = "curious"
            # Record what she sent — keyed by the Telegram message_id so a later
            # reply resolves back to this exact article, plus a mem0 record for
            # "find similar". Fail-silent (prime directive).
            try:
                from autonomy import news_ledger
                await news_ledger.record_shared(res.get("tg_msg_id"), item, take)
            except Exception as exc:
                log.warning("autonomy: news_ledger record failed: %s", exc)
        return  # spent this tick on a real find — don't also fire filler

    # 2) No find available. By default she stays silent — she only speaks
    #    unprompted when she has a genuinely fresh item, never content-free
    #    filler. Re-enable ambient check-ins with autonomy.allow_empty_checkins.
    if not cfg.get("allow_empty_checkins", False):
        return
    if state == "drift" and random.random() > 0.4:
        return
    if state in ("bored", "lonely") and random.random() > float(cfg.get("checkin_probability", 0.5)):
        return

    segments = await _compose_utterance(state, topic, elapsed)
    res = await _deliver(segments, state)
    if res["spoke"]:
        _mark_spoke(_segments_text(segments))
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

    # Greetings draw on a counter separate from the news budget (so a hello never
    # eats it) and are rate-limited solely by the restart-proof debounce below —
    # no daily cap, so every genuine return (>30 min away) gets greeted.
    _maybe_reset_daily_counter()

    # Brand-new user (anon, never named) → fire a warm first-meeting greeting
    # immediately, bypassing the reconnect debounce. Otherwise the standard
    # rage-refresh cooldown applies.
    if is_new_user:
        log.info("autonomy: first_hello for new user uid=%s", (user_id or "?")[:8])
        segments = await _compose_utterance("first_hello", "", elapsed_s=0.0)
        if not segments:
            return
        res = await _deliver(segments, "first_hello", connect_triggered=True)
        if res["spoke"]:
            _stamp_greeting()
            _mark_spoke(_segments_text(segments), reconnect=True)
            _mocha_state["mood"] = "curious"
        return

    # Returning-user reconnect path — debounce rage-refreshes. Claim the slot
    # BEFORE the compose/deliver await, so two near-simultaneous client_hello
    # events (reconnects, two tabs) can't both pass the check and double-greet.
    if _greeting_debounced(cfg):
        log.info("autonomy: hello debounced")
        return
    _stamp_greeting()

    # Ground the greeting in REAL, cited items she actually has — never invent.
    # Priority: news she SHARED (ledger, newest first) → fresh news she FOUND
    # (curiosity pool, non-destructive peek) → queued findings. If nothing real
    # is available, findings_preview stays empty and the prompt forbids any news.
    grounding: list[str] = []
    try:
        from autonomy import news_ledger
        for rec in reversed(news_ledger.recent(3)):
            h = (rec.get("headline") or "").strip()
            if h:
                src = rec.get("source") or ""
                grounding.append(f"- {h}{f' — {src}' if src else ''} (you shared this)")
    except Exception as exc:
        log.debug("reconnect grounding: ledger read failed: %s", exc)
    if len(grounding) < 3:
        try:
            from autonomy import curiosity
            for it in await curiosity.peek(3):
                h = (it.get("title") or "").strip()
                if h:
                    src = it.get("source") or ""
                    grounding.append(f"- {h}{f' — {src}' if src else ''} (you noticed this)")
                if len(grounding) >= 3:
                    break
        except Exception as exc:
            log.debug("reconnect grounding: curiosity peek failed: %s", exc)

    pending = await notifications.list_undelivered()
    # Cap to the 2 most recent queued findings — leave the rest queued.
    preview_items = pending[-2:]
    for it in preview_items:
        s = (it.get("summary") or "").strip()
        if s:
            grounding.append(f"- {s[:160]}")
    findings_preview = "\n".join(grounding[:3])

    segments = await _compose_utterance("reconnect", _mocha_state.get("last_topic_summary") or "",
                                         elapsed_s=0.0, findings_preview=findings_preview)
    if not segments:
        return

    res = await _deliver(segments, "reconnect", connect_triggered=True)
    if res["spoke"]:
        _stamp_greeting()
        _mark_spoke(_segments_text(segments), reconnect=True)
        _mocha_state["mood"] = "happy"
        if preview_items:
            await notifications.mark_delivered([it["id"] for it in preview_items])
