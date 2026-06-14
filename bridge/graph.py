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
    "1. The tool result above contains `num:xxxxxxxx` handles wherever "
    "a numeric value belongs (prices, percentages, temperatures, counts).\n"
    "2. Your conversation history and memory fragments may contain "
    "numeric-looking tokens from previous turns. Those are STALE — "
    "today's data may be completely different. You must IGNORE every "
    "number-like token that is NOT inside a `num:xxxxxxxx` handle.\n"
    "3. The ONLY numbers you may reference in this reply are `num:xxxxxxxx` "
    "handles from the most recent tool result. Copy each handle VERBATIM. "
    "Do not write out the value — write the handle, the bridge resolves it.\n"
    "4. If a claim you want to make needs a value you don't have a handle "
    "for, OMIT the claim entirely. Do not invent, recall, or approximate.\n"
    "5. The bridge resolves handles to real formatted values right before "
    "TTS. The user hears the real number even though you wrote the handle."
)


async def _emit(state: TurnState, ev: dict) -> None:
    await state["emit"].put(ev)


async def _emit_stall(state: TurnState) -> None:
    """Speak a short, FRESH filler (generated on the fast 3B, always) so the user
    isn't left in silence while a tool + deep synthesis run — like a quick
    "ooh, let me check that". Best-effort: never blocks/breaks the turn. Not added
    to full_text_parts (it's transient filler, not part of the recorded reply)."""
    from bridge import server as S
    try:
        res = await S.llm_client.chat(
            [{"role": "system", "content": S._STALL_SYSTEM},
             {"role": "user", "content": (state.get("user_text") or "")[:400]}],
            temperature=0.9, max_tokens=16,
        )
        line = (res.get("content") or "").strip().strip('"').strip()
        if not line:
            return
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
    state["route"] = S._route_model(state.get("user_text", ""))
    return state


async def build_messages_node(state: TurnState) -> dict:
    """System prompt + bounded history + memories + redaction; init counters."""
    from bridge import server as S

    state["messages"] = S._build_inline_messages(
        state["user_text"], state["memories"], user_id=state.get("user_id")
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
    llm_stream = client.chat_stream(messages, tools=None)

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
                await _event_gen.aclose()
                break
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
    """Conditional edge: loop into tools, or finalize. Mirrors the old guard."""
    from bridge import server as S

    if (state["pending_tool_calls"]
            and S.TOOLS_ENABLED
            and state["tool_round"] < S._TOOL_MAX_ROUNDS):
        return "run_tools"
    return "finalize"


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

    # Perceived-latency: once per turn, speak a fresh filler while the tool +
    # (possibly deep) synthesis run, so the user always hears something. The
    # filler's audio plays client-side while the slow work proceeds here.
    if not state.get("stalled"):
        state["stalled"] = True
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
    await _emit(state, {
        "type": "speech_end",
        "total_chunks": state["chunk_idx"],
        "full_text": "".join(state["full_text_parts"]),
    })
    return state


def _build_graph():
    g = StateGraph(TurnState)
    g.add_node("router", route_node)
    g.add_node("build_messages", build_messages_node)
    g.add_node("llm_pass", llm_pass_node)
    g.add_node("log_pass", log_pass_node)
    g.add_node("run_tools", run_tools_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "router")
    g.add_edge("router", "build_messages")
    g.add_edge("build_messages", "llm_pass")
    g.add_edge("llm_pass", "log_pass")
    g.add_conditional_edges(
        "log_pass", should_continue,
        {"run_tools": "run_tools", "finalize": "finalize"},
    )
    g.add_edge("run_tools", "llm_pass")  # ReAct loop-back
    g.add_edge("finalize", END)
    return g.compile()


# Compiled once at import. Building the graph does not import server or run any
# node, so there is no import cycle.
MOCHA_GRAPH = _build_graph()
