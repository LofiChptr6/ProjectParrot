"""Mocha's conversational orchestration as a LangGraph StateGraph.

This replaces the hand-rolled ReAct ``while`` loop that lived in
``bridge/server.py:_run_inline_turn``. The control flow is now a graph:

    build_messages → llm_pass → log_pass → [should_continue?]
                          ↑                    │
                          └──── run_tools ←────┤ (pending tools & round < max)
                                               └→ finalize → END

LangGraph owns ONLY the orchestration (the loop, the conditional tool edge,
node sequencing). The streaming LLM call + inline-tag parser stay inside the
``llm_pass`` node, exactly as before, so token/emotion/gesture/speech timing is
unchanged. Per-turn UI events are pushed onto the live ``asyncio.Queue`` carried
in ``TurnState["emit"]``; the thin wrapper ``server._run_inline_turn`` drains
that queue and yields the same wire events its 5 callers already expect.

Node bodies are lifted ~verbatim from the old ``_run_inline_turn``. Server-side
helpers are imported lazily inside each node (the module is fully loaded by the
time a turn runs) so importing this module never triggers an import cycle.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import re
import time

import httpx
from langgraph.graph import StateGraph, START, END

from bridge.graph_state import TurnState
from bridge.inline_route import drive_inline_stream

log = logging.getLogger("bridge.graph")

# Pushed onto the emit queue by the wrapper's finally to signal end-of-turn.
SENTINEL = object()

# Verbatim from the old _run_inline_turn: reinforce handle-quoting after tools.
_HANDLE_REMINDER = (
    "CRITICAL — read before replying:\n\n"
    "1. The tool result above contains `num:<id>` handles wherever "
    "a numeric value belongs (prices, percentages, temperatures, counts).\n"
    "2. Your conversation history and memory fragments may contain "
    "numeric-looking tokens from previous turns. Those are STALE — "
    "today's data may be completely different. You must IGNORE every "
    "number-like token that is NOT inside a `num:<id>` handle.\n"
    "3. The ONLY numbers you may reference in this reply are `num:<id>` "
    "handles from the most recent tool result. Copy each handle VERBATIM. "
    "Do not write out the value — write the handle, the bridge resolves it.\n"
    "4. If a claim you want to make needs a value you don't have a handle "
    "for, OMIT the claim entirely. Do not invent, recall, or approximate.\n"
    "5. The bridge resolves handles to real formatted values right before "
    "TTS. The user hears the real number even though you wrote the handle."
)


async def _emit(state: TurnState, ev: dict) -> None:
    await state["emit"].put(ev)


# Arg keys most likely to carry the "what" of a lookup, in priority order.
_STALL_HINT_KEYS = (
    "symbol", "ticker", "symbols", "query", "q", "topic", "subject",
    "location", "city", "name", "title", "expression", "text",
)


def _describe_pending_action(pending: list) -> str:
    """Compact, human-readable description of the tool call(s) about to run, so the
    stall filler can NAME what it's fetching (e.g. ``get_stock_quote (NVDA)``)
    instead of guessing. Best-effort; returns "" when nothing useful is found."""
    from bridge import server as S
    parts = []
    for tc in (pending or [])[:2]:
        name = (tc.get("name") or "").strip()
        if not name:
            continue
        try:
            args = S._parse_tool_args_str(tc.get("arguments") or "") or {}
        except Exception:
            args = {}
        hint = ""
        for k in _STALL_HINT_KEYS:
            v = args.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                hint = str(v).strip()
                break
        if not hint:
            for v in args.values():
                if isinstance(v, str) and v.strip():
                    hint = v.strip()
                    break
        parts.append(f"{name} ({hint})" if hint else name)
    return "; ".join(parts)


async def _emit_stall(state: TurnState, reserved_idx: int | None = None) -> None:
    """Speak a short, FRESH filler grounded in the action she's about to take —
    e.g. "getting NVIDIA's price right now", "looking up the latest TSLA news" —
    so the user isn't left in silence while a tool + (often deep) synthesis run.
    Generated on the fast 3B so it's instant even on deep/tool turns. Best-effort:
    never blocks/breaks the turn. Not added to full_text_parts (transient filler,
    not part of the recorded reply).

    ``reserved_idx`` lets a caller pre-allocate this chunk's index (so the stall
    can run as a concurrent task without racing the main stream for chunk_idx);
    when None, it allocates from state itself (the original in-loop behaviour)."""
    from bridge import server as S
    try:
        action = _describe_pending_action(state.get("pending_tool_calls") or [])
        user_msg = f"They asked: {(state.get('user_text') or '')[:240]}"
        if action:
            user_msg += f"\nThe lookup you're about to run: {action}"
        else:
            # Cold start (fired before the model picked a tool): don't let it
            # invent specifics — keep it a vague, warm acknowledgment.
            user_msg += ("\nYou don't know the exact details yet — just warmly say you're "
                         "on it / looking into it. Keep it vague; do NOT invent specifics, "
                         "facts, or numbers (e.g. say \"let me pull that up\", not "
                         "\"getting the Q3 report\").")
        user_msg += "\nSay your one short line now."
        res = await S.llm_client.chat(
            [{"role": "system", "content": S._STALL_SYSTEM},
             {"role": "user", "content": user_msg}],
            temperature=0.8, max_tokens=20,
        )
        line = (res.get("content") or "").strip().strip('"').strip()
        if not line:
            return
        if reserved_idx is not None:
            idx = reserved_idx
        else:
            idx = state["chunk_idx"]
            state["chunk_idx"] = idx + 1
        audio = await S._synthesize(line, user_id=state.get("user_id"))
        viseme = (await S._generate_visemes(audio, line) if audio else None) or {}
        await _emit(state, {
            "type": "speech_chunk", "chunk_idx": idx, "text": line,
            "audio_base64": base64.b64encode(audio).decode() if audio else None,
            "viseme_b64": viseme.get("viseme_b64"),
            "viseme_fps": viseme.get("viseme_fps", 30),
            "viseme_frames": viseme.get("viseme_frames", 0),
        })
    except Exception as e:
        log.warning("[graph] stall filler failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────
#  Nodes
# ──────────────────────────────────────────────────────────────────────────

async def route_node(state: TurnState) -> dict:
    """Heuristic model pick for this turn (fast 3B vs deep Qwen-32B). No LLM
    call, so it costs ~0ms / no TTFT hit. Tool turns later escalate to deep."""
    from bridge import server as S
    # A reply to a shared news item needs the deep model: it carries the article
    # context and may call recall_news to find similar pieces (the fast 3B has no
    # tools and would just have to escalate).
    if state.get("reply_context"):
        state["route"] = "deep"
    else:
        state["route"] = S._route_model(state.get("user_text", ""))
    return state


async def build_messages_node(state: TurnState) -> dict:
    """System prompt + bounded history + memories + redaction; init counters."""
    from bridge import server as S

    # Chat-only fast 3B (no tools → it can't over-tool/mis-tool, its weakest skill);
    # the deep Qwen gets the full toolkit. Route is already decided (router ran first).
    tools_available = S.TOOLS_ENABLED and state.get("route") == "deep"
    state["messages"] = S._build_inline_messages(
        state["user_text"], state["memories"], user_id=state.get("user_id"),
        tools_available=tools_available, reply_context=state.get("reply_context"),
    )
    state["tool_round"] = 0
    state["chunk_idx"] = 0
    state["full_text_parts"] = []
    await S._monitor_thread_start(
        "llm", input_preview=state["user_text"], job_id=state["job_id"]
    )
    return state


async def llm_pass_node(state: TurnState) -> dict:
    """Stream one LLM pass: drive the inline-tag parser, emit speech/emotion/
    gesture events, and collect any tool calls. The streaming pipeline is
    identical to the old loop; only the surrounding control flow moved."""
    from bridge import server as S

    messages = state["messages"]
    pass_tool_calls: list[dict] = []
    pass_content_parts: list[str] = []
    pass_started = time.monotonic()
    pass_ttft: float | None = None
    pass_usage: dict = {}
    pass_finish: str | None = None
    pass_error: str | None = None
    state["pass_started"] = pass_started

    chunk_idx = state["chunk_idx"]
    full_text_parts = state["full_text_parts"]

    # Route: deep model (Qwen-32B) for reasoning/tool turns, else the fast 3B.
    client = S.llm_deep if state.get("route") == "deep" else S.llm_client
    state["pass_model"] = client.model
    # Disable Qwen's <think> on the FIRST pass (and on escalated reasoning turns).
    # Why: the first pass only decides whether to call a tool and speaks a lead-in,
    # but a <think> block (1–3k chars ≈ 8–20s, all silent) lands BEFORE that first
    # spoken word — so her stall arrives very late, and on a tool turn the entire
    # think block is then thrown away when she calls the tool. Answering/deciding
    # directly makes the lead-in land at TTFT (~0.3s). The SYNTHESIS pass (after a
    # tool, tool_round>0) keeps thinking — it reasons over the data, and its think
    # gap is already covered by the run_tools stall.
    _think = False if (state.get("escalated") or int(state.get("tool_round", 0)) == 0) else None
    llm_stream = client.chat_stream(messages, tools=None, enable_thinking=_think)

    # Parallel stall: on a DEEP turn's FIRST pass, fire an instant filler on the
    # fast 3B *concurrently* with Qwen's (slow) prefill — so the user hears an
    # acknowledgment in ~0.4s instead of waiting on the 32B's first token (or on
    # nothing at all, if Qwen is down). It runs as its own task, emits one
    # speech_chunk on a reserved index, and Qwen's real content streams after.
    # (Qwen is told to skip its own lead-in before a tool call — see context.py —
    # so this is the single opening cover, not a double.)
    _stall_task = None
    if (state.get("route") == "deep" and int(state.get("tool_round", 0)) == 0
            and not state.get("started_stall") and not state.get("escalated")):
        # (Skip on escalated turns: the fast model's <escalate/> cover already spoke.)
        state["started_stall"] = True
        _stall_reserved = chunk_idx
        chunk_idx += 1
        _stall_task = asyncio.create_task(_emit_stall(state, reserved_idx=_stall_reserved))

    async def _token_feeder():
        nonlocal pass_ttft, pass_usage, pass_finish
        async for chunk in llm_stream:
            content = chunk.get("content", "")
            if content:
                pass_content_parts.append(content)
                if pass_ttft is None:
                    pass_ttft = (time.monotonic() - pass_started) * 1000
                yield content
            if chunk.get("done"):
                pass_usage = chunk.get("usage", {})
                pass_finish = chunk.get("finish_reason")
                break

    _event_gen = drive_inline_stream(_token_feeder())
    try:
        async for ev in _event_gen:
            t = ev["type"]
            if t == "thinking":
                await _emit(state, {"type": "thinking_delta", "content": ev["text"]})
            elif t == "emotion":
                await _emit(state, {"type": "emotion", "id": ev["id"]})
            elif t == "gesture":
                resolved = await S._resolve_action(ev["name"])
                await _emit(state, {"type": "gesture", "name": resolved or ev["name"]})
            elif t == "speech_chunk":
                raw_text = ev["text"]
                S._check_unmapped_numeric_literals(raw_text, state["job_id"], chunk_idx)
                chunk_text, unmapped = S.substitute_handles_in_text(raw_text)
                if unmapped:
                    log.warning(
                        "[graph] job=%s chunk_idx=%d unmapped handles: %s",
                        state["job_id"], chunk_idx, unmapped,
                    )
                # Scrub the STREAMED chunk (webapp shows these live, before finalize):
                # strip a mimicked [past #], leaked <strong>/HTML, and **markdown**.
                chunk_text = S._clean_spoken(chunk_text)
                full_text_parts.append(chunk_text)
                audio_bytes = await S._synthesize(chunk_text, user_id=state.get("user_id"))
                viseme = (
                    await S._generate_visemes(audio_bytes, chunk_text)
                    if audio_bytes else None
                ) or {}
                await _emit(state, {
                    "type": "speech_chunk",
                    "chunk_idx": chunk_idx,
                    "text": chunk_text,
                    "audio_base64": (
                        base64.b64encode(audio_bytes).decode() if audio_bytes else None
                    ),
                    "viseme_b64": viseme.get("viseme_b64"),
                    "viseme_fps": viseme.get("viseme_fps", 30),
                    "viseme_frames": viseme.get("viseme_frames", 0),
                })
                chunk_idx += 1
            elif t == "tool_call":
                pass_tool_calls.append(ev)
                # Abort the rest of this pass — anything after <tool_call> is
                # speculation without tool results. The tool-loop re-fire is the
                # sole author of post-tool speech.
                log.info(
                    "[graph] job=%s <tool_call name=%s> — aborting stream",
                    state["job_id"], ev.get("name"),
                )
                # Just break; the `finally` closes the generator. Calling aclose()
                # here — mid-iteration — raises "async generator is already running"
                # (harmless but noisy, and now more frequent with tool routing).
                break
            elif t == "escalate":
                # Self-handoff: the fast model judged this beyond it. Honor only if
                # it hasn't spoken yet (escalate must come first), we're on fast, and
                # we haven't already escalated — then abort and re-run on deep.
                if (not full_text_parts and state.get("route") != "deep"
                        and not state.get("escalated")):
                    state["escalate_requested"] = True
                    log.info("[graph] job=%s <escalate/> — handing off to deep model",
                             state["job_id"])
                    break
                log.info("[graph] job=%s <escalate/> ignored (already spoke / already deep)",
                         state["job_id"])
            elif t == "reads":
                log.info("[reads] job=%s state=%s", state["job_id"], ev.get("state"))
                await _emit(state, {"type": "reads_debug", "state": ev.get("state")})
            elif t == "end":
                pass
    except httpx.ConnectError as e:
        pass_error = str(e)
        log.error("LLM unreachable: %s", e)
    except Exception as e:
        pass_error = str(e)
        log.exception("LLM stream failed")
    finally:
        try:
            await _event_gen.aclose()
        except Exception:
            pass
        # Ensure the parallel stall finished emitting before the turn advances
        # (it almost always completes long before Qwen; this just reaps it).
        if _stall_task is not None:
            try:
                await _stall_task
            except Exception:
                pass

    state["chunk_idx"] = chunk_idx
    state["pending_tool_calls"] = pass_tool_calls
    state["pass_content"] = "".join(pass_content_parts)
    state["pass_ttft"] = pass_ttft
    state["pass_usage"] = pass_usage
    state["pass_finish"] = pass_finish
    state["pass_error"] = pass_error
    return state


async def log_pass_node(state: TurnState) -> dict:
    """Fire-and-forget one PG call_log row per LLM pass (single log site)."""
    from bridge import server as S

    latency_ms = (time.monotonic() - state["pass_started"]) * 1000
    S._pipeline_state["llm_ms"] = round(latency_ms, 1)
    _pass_ctx = dataclasses.replace(state["base_ctx"], pass_number=1)
    _logc = S.llm_deep if state.get("route") == "deep" else S.llm_client
    asyncio.create_task(S.call_log.log_call(
        _pass_ctx, model=_logc.model,
        temperature=_logc.default_temperature,
        max_tokens=_logc.default_max_tokens,
        stream=True, enable_thinking=False,
        tools_provided=False, messages=state["messages"],
        response_content=state["pass_content"] or None,
        finish_reason=state["pass_finish"], error=state["pass_error"],
        latency_ms=latency_ms, ttft_ms=state["pass_ttft"],
        prompt_tokens=state["pass_usage"].get("prompt_tokens"),
        completion_tokens=state["pass_usage"].get("completion_tokens"),
        total_tokens=state["pass_usage"].get("total_tokens"),
    ))
    return state


def should_continue(state: TurnState) -> str:
    """Conditional edge: hand off to deep (escalate), loop into tools, or finalize."""
    from bridge import server as S

    if (state.get("escalate_requested")
            and state.get("route") != "deep"
            and not state.get("escalated")):
        return "escalate"
    if (state["pending_tool_calls"]
            and S.TOOLS_ENABLED
            and state["tool_round"] < S._TOOL_MAX_ROUNDS):
        return "run_tools"
    return "verify"


async def escalate_node(state: TurnState) -> dict:
    """Hand the turn from the fast 3B to the deep Qwen after an `<escalate/>`.

    The fast model judged the question beyond it (hard reasoning, or a specific
    fact it isn't sure of and can't tool-verify). We discard its pre-speech
    partial, flip the route to deep, speak a brief cover line so the handoff
    isn't silent, and loop back to llm_pass for a careful answer. Once-only."""
    from bridge import server as S

    state["route"] = "deep"
    state["escalated"] = True
    state["escalate_requested"] = False
    state["pending_tool_calls"] = []
    state["full_text_parts"] = []   # discard the fast partial (should be empty)
    state["pass_content"] = ""
    log.info("[graph] job=%s escalated fast→deep", state["job_id"])

    # The fast prompt was built CHAT-ONLY (no tools). The deep pass needs the
    # toolkit — rebuild the system message with tools so Qwen can look things up.
    try:
        msgs = state.get("messages") or []
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = S.build_system_prompt(
                animation_mode=S.ANIMATION_MODE, tools_available=S.TOOLS_ENABLED,
                user_id=state.get("user_id"),
            )
    except Exception as e:
        log.warning("[graph] escalate prompt rebuild failed: %s", e)

    # Perceived-latency cover for the (slower) deep pass — transient, not recorded
    # in full_text_parts (mirrors the tool stall).
    if not state.get("stalled"):
        state["stalled"] = True
        try:
            line = S._escalate_stall_line(state.get("job_id", 0))
            idx = state["chunk_idx"]
            state["chunk_idx"] = idx + 1
            audio = await S._synthesize(line, user_id=state.get("user_id"))
            viseme = (await S._generate_visemes(audio, line) if audio else None) or {}
            await _emit(state, {
                "type": "speech_chunk", "chunk_idx": idx, "text": line,
                "audio_base64": base64.b64encode(audio).decode() if audio else None,
                "viseme_b64": viseme.get("viseme_b64"),
                "viseme_fps": viseme.get("viseme_fps", 30),
                "viseme_frames": viseme.get("viseme_frames", 0),
            })
        except Exception as e:
            log.warning("[graph] escalate stall failed: %s", e)
    return state


async def run_tools_node(state: TurnState) -> dict:
    """Execute the pending tool calls and append results; loop back to llm_pass.

    All handle invariants + the ``__panel__`` broadcast live inside
    ``execute_tool`` — this node only dispatches and threads results back."""
    from bridge import server as S

    messages = state["messages"]
    pending = state["pending_tool_calls"]
    tool_round = state["tool_round"]

    # Record the assistant's last spoken output so the follow-up call has context.
    messages.append({"role": "assistant", "content": state["pass_content"]})

    # Perceived-latency cover: speak a fresh, action-grounded filler — but ONLY if
    # she went silent before the tool fired. If the model already streamed a lead-in
    # this turn (it lives in full_text_parts; the injected filler deliberately does
    # not), that's the user's audible cover already and a second line would just be
    # redundant. The decision is made once per turn either way.
    if not state.get("stalled"):
        state["stalled"] = True
        spoken_so_far = "".join(state.get("full_text_parts") or []).strip()
        if len(spoken_so_far) < 12:
            await _emit_stall(state)

    for tc in pending:
        tool_round += 1
        tool_name = tc["name"]
        tool_args_str = tc["arguments"]
        tool_id = tc["id"]
        args = S._parse_tool_args_str(tool_args_str)

        await _emit(state, {
            "type": "tool_status", "action": "call", "round": tool_round,
            "tool_name": tool_name,
            "tool_args_preview": tool_args_str[:200],
            "tool_args": args,
        })
        await S._broadcast_monitor({
            "type": "tool_activity", "action": "call",
            "job_id": state["job_id"], "round": tool_round, "tool_name": tool_name,
            "tool_args": json.dumps(args)[:500],
        })

        t_tool = time.monotonic()
        try:
            result = await S.execute_tool(tool_name, args)
        except Exception as e:
            log.exception("Tool %s failed", tool_name)
            result = f"Tool error: {e}"
        tool_ms = (time.monotonic() - t_tool) * 1000

        await _emit(state, {
            "type": "tool_status", "action": "result", "round": tool_round,
            "tool_name": tool_name, "result_preview": result[:500],
            "duration_ms": round(tool_ms, 1),
        })
        await S._broadcast_monitor({
            "type": "tool_activity", "action": "result",
            "job_id": state["job_id"], "round": tool_round, "tool_name": tool_name,
            "result_preview": result[:800], "duration_ms": round(tool_ms, 1),
        })

        messages.append({
            "role": "tool",
            "tool_call_id": tool_id,
            "content": result,
        })

    if any("num:" in (m.get("content") or "") for m in messages[-len(pending):]):
        messages.append({"role": "system", "content": _HANDLE_REMINDER})

    # A tool fired → synthesize the result on the deep model (Qwen-32B), which is
    # far more reliable at the inline-tag format + handle-quoting than the 3B.
    state["route"] = "deep"
    state["tool_round"] = tool_round
    return state


async def finalize_node(state: TurnState) -> dict:
    """End the turn: monitor end + speech_end. (SENTINEL is emitted by the
    wrapper's finally so it fires even if a node raised.)"""
    from bridge import server as S

    await S._monitor_thread_end(
        "llm", elapsed_ms=(time.monotonic() - state["pass_started"]) * 1000,
        input_preview=state["user_text"], job_id=state["job_id"],
    )
    # Deterministic final scrub on every surface (cheap, no LLM) — last guard
    # against leaked <think>/JSON/tag artifacts AND leaked handle syntax
    # (e.g. a fabricated `num:513.32…`) reaching the user, even on the realtime
    # webapp path that skips the Qwen verifier.
    full_text = S._clean_leaked_handles(S._sanitize_outgoing("".join(state["full_text_parts"])))

    # Empty-output guard: a realtime turn can think-truncate to nothing (the
    # verifier's empty-rescue only runs non-realtime). Don't leave the user in
    # silence — speak a short fallback so voice/text surfaces both get something.
    if not full_text and (int(state.get("tool_round", 0)) > 0
                          or (state.get("pass_content") or "").strip()
                          or state.get("pass_error")):
        # A connection/stream error (e.g. the deep Qwen backend down) leaves
        # tool_round 0 and no content — without this it would speak-end silent.
        fb = (S._backend_down_line(state.get("job_id", 0))
              if state.get("pass_error") else S._empty_fallback(state.get("job_id", 0)))
        try:
            idx = state["chunk_idx"]
            state["chunk_idx"] = idx + 1
            audio = await S._synthesize(fb, user_id=state.get("user_id"))
            viseme = (await S._generate_visemes(audio, fb) if audio else None) or {}
            await _emit(state, {
                "type": "speech_chunk", "chunk_idx": idx, "text": fb,
                "audio_base64": base64.b64encode(audio).decode() if audio else None,
                "viseme_b64": viseme.get("viseme_b64"),
                "viseme_fps": viseme.get("viseme_fps", 30),
                "viseme_frames": viseme.get("viseme_frames", 0),
            })
        except Exception as e:
            log.warning("[graph] empty-fallback synth failed: %s", e)
        full_text = fb
        log.info("[graph] empty turn (job=%s) → spoke fallback", state.get("job_id"))

    await _emit(state, {
        "type": "speech_end",
        "total_chunks": state["chunk_idx"],
        "full_text": full_text,
    })
    return state


# ──────────────────────────────────────────────────────────────────────────
#  Verifier (non-realtime only)
# ──────────────────────────────────────────────────────────────────────────

_VERIFY_SYSTEM = (
    "You are the final editor for Mocha's outgoing message on a TEXT channel "
    "(not live voice), so correctness matters more than speed. You get the user's "
    "message, the SOURCE DATA from any tools she used this turn, and her DRAFT "
    "reply. Return a corrected reply that:\n"
    "1. Removes leaked artifacts: <think>…</think>, raw JSON like {\"segments\":…}, "
    "stray <tool_call>/<emotion>/<gesture> tags, code fences, or an unfinished "
    "trailing sentence.\n"
    "2. Grounds its claims. Any number/name/ticker/date/event backed by the SOURCE "
    "DATA must match it exactly. Any SPECIFIC factual claim NOT backed by the source "
    "(and not obvious general knowledge) must be softened to acknowledge uncertainty "
    "(\"I think…\", \"if I recall\") or dropped — never let her assert an unverified "
    "specific with false confidence. Never invent or guess a value.\n"
    "3. Preserves her voice, tone, and wording wherever it's already correct. You "
    "are fixing errors, NOT rewriting her style or padding it out.\n"
    "Output ONLY the corrected reply text — no preamble, no tags, no quotes. If the "
    "draft is already clean and fully supported, output it verbatim."
)

_VERIFY_RESCUE_SYSTEM = (
    "You are speaking AS Mocha — warm, direct, concise, a little dry; she doesn't "
    "pad or gush. She tried to reply to the user but produced no usable text (she "
    "got stuck mid-thought). Using the user's message, the SOURCE DATA from any "
    "tools, and her own unfinished reasoning, write the reply she meant to give — "
    "in her voice, 1-3 short sentences. Use ONLY facts present in the source/"
    "reasoning; never invent a number or claim. Output ONLY the reply text — no "
    "tags, no preamble, no quotes."
)

_PREAMBLE_RE = re.compile(
    r"^\s*(?:corrected reply|corrected|reply|here(?:'s| is)[^:]*)\s*:\s*", re.IGNORECASE)


def _clean_verifier_output(s: str) -> str:
    """Cleanup of the verifier's own output (told to return bare text, but small
    slips shouldn't reach the user). Reuses the shared deterministic scrubber."""
    from bridge import server as S
    s = S._sanitize_outgoing(s or "")
    s = _PREAMBLE_RE.sub("", s)
    return s.strip().strip('"').strip()


async def verify_node(state: TurnState) -> dict:
    """Non-realtime turns only: Qwen checks/repairs the assembled draft (format
    hygiene + faithfulness to tool results) and rewrites ``full_text_parts`` so the
    channel sends the corrected text. Pass-through on realtime / when disabled.
    Fail-open — any error keeps the original draft (never breaks the turn)."""
    from bridge import server as S

    if state.get("realtime") or not S._verifier_enabled():
        return state
    draft = "".join(state.get("full_text_parts") or []).strip()
    tool_ran = int(state.get("tool_round", 0)) > 0
    raw_attempt = (state.get("pass_content") or "").strip()

    # An empty / stub draft on a non-realtime turn is itself a format failure —
    # usually the model spent its whole budget inside <think> and emitted no
    # speech. Rescue it (compose from the source + its own reasoning) instead of
    # sending silence — but only when there's material to work from.
    rescue = ((not draft) or (tool_ran and len(draft) < 24)) and (tool_ran or bool(raw_attempt))
    # Every non-realtime turn verifies (per request — telegram et al. always get a
    # check). Only bail when there's nothing to work with: an empty draft with
    # nothing to rescue from.
    if not rescue and not draft:
        return state

    try:
        # Resolve num: handles in tool results so Qwen checks against real values.
        sources: list[str] = []
        for m in state.get("messages") or []:
            if m.get("role") == "tool":
                txt, _ = S.substitute_handles_in_text(m.get("content") or "")
                if txt.strip():
                    sources.append(txt.strip()[:800])
        source_block = "\n---\n".join(sources[-4:]) if sources else "(no tools used this turn)"

        if rescue:
            sys_prompt = _VERIFY_RESCUE_SYSTEM
            user_block = (
                f"USER MESSAGE:\n{(state.get('user_text') or '')[:600]}\n\n"
                f"SOURCE DATA (tool results this turn):\n{source_block[:2400]}\n\n"
                f"MOCHA'S UNFINISHED REASONING (she never produced a reply):\n"
                f"{raw_attempt[:1500] or '(none)'}"
            )
            max_tok = 500
        else:
            sys_prompt = _VERIFY_SYSTEM
            user_block = (
                f"USER MESSAGE:\n{(state.get('user_text') or '')[:600]}\n\n"
                f"SOURCE DATA (tool results this turn):\n{source_block[:2400]}\n\n"
                f"DRAFT REPLY:\n{draft}"
            )
            max_tok = 600

        t0 = time.monotonic()
        res = await S.llm_deep.chat(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": user_block}],
            temperature=0.3 if rescue else 0.2, max_tokens=max_tok, enable_thinking=False,
        )
        verify_ms = (time.monotonic() - t0) * 1000
        fixed = _clean_verifier_output(res.get("content") or "")

        # Log the verify pass (pass_number=2 mirrors the old complexity-routing tag).
        try:
            _vctx = dataclasses.replace(state["base_ctx"], pass_number=2)
            asyncio.create_task(S.call_log.log_call(
                _vctx, model=S.llm_deep.model, temperature=0.2, max_tokens=600,
                stream=False, enable_thinking=False, tools_provided=False,
                messages=[{"role": "system", "content": "[verify]"},
                          {"role": "user", "content": user_block}],
                response_content=fixed or None, finish_reason=res.get("finish_reason"),
                latency_ms=verify_ms,
                prompt_tokens=(res.get("usage") or {}).get("prompt_tokens"),
                completion_tokens=(res.get("usage") or {}).get("completion_tokens"),
                total_tokens=(res.get("usage") or {}).get("total_tokens"),
            ))
        except Exception:
            pass

        if fixed and (rescue or fixed != draft):
            log.info("[graph] verify %s (job=%s): %d→%d chars",
                     "rescued empty draft" if rescue else "repaired draft",
                     state.get("job_id"), len(draft), len(fixed))
            state["full_text_parts"] = [fixed]
            state["verified"] = True
    except Exception as e:
        log.warning("[graph] verify_node failed (keeping draft): %s", e)
    return state


def _build_graph():
    g = StateGraph(TurnState)
    g.add_node("router", route_node)
    g.add_node("build_messages", build_messages_node)
    g.add_node("llm_pass", llm_pass_node)
    g.add_node("log_pass", log_pass_node)
    g.add_node("run_tools", run_tools_node)
    g.add_node("escalate", escalate_node)
    g.add_node("verify", verify_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "router")
    g.add_edge("router", "build_messages")
    g.add_edge("build_messages", "llm_pass")
    g.add_edge("llm_pass", "log_pass")
    g.add_conditional_edges(
        "log_pass", should_continue,
        {"run_tools": "run_tools", "escalate": "escalate", "verify": "verify"},
    )
    g.add_edge("run_tools", "llm_pass")  # ReAct loop-back
    g.add_edge("escalate", "llm_pass")   # fast→deep handoff, re-run on deep
    g.add_edge("verify", "finalize")     # non-realtime repair; pass-through on realtime
    g.add_edge("finalize", END)
    return g.compile()


# Compiled once at import. Building the graph does not import server or run any
# node, so there is no import cycle.
MOCHA_GRAPH = _build_graph()
