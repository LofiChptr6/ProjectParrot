"""TurnState — the LangGraph state for one Mocha conversational turn.

The graph (``bridge/graph.py``) replaces the hand-rolled ReAct loop that used
to live in ``bridge/server.py:_run_inline_turn``. Streaming UI events do NOT
flow through LangGraph's state (that would lose token-level timing); instead a
live ``asyncio.Queue`` (``emit``) is carried by reference and drained by the
thin async-generator wrapper in ``server.py``. This is safe because the graph
runs in-process with no checkpointer.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, TypedDict


class TurnState(TypedDict, total=False):
    # ── Identity / partition ────────────────────────────────────────────────
    user_id: Optional[str]      # hard partition key (history, memory, tools)
    job_id: int                 # also the conversation_id
    source: str                 # "web" | "channel" | "voice" | "eval" | ...
    base_ctx: Any               # bridge.call_log.CallContext (base)

    # ── Inputs ──────────────────────────────────────────────────────────────
    user_text: str
    memories: list              # pre-fetched mem0 facts (caller supplies)

    # ── Routing (which model serves this turn) ──────────────────────────────
    route: str                  # "fast" (Llama-3B) | "deep" (Qwen-32B)
    pass_model: str             # model id actually used this pass (for logging)

    # ── Working set (mutated across the ReAct loop) ─────────────────────────
    messages: list              # OpenAI-format message array
    tool_round: int             # ReAct counter, bounded by tools.max_rounds
    chunk_idx: int              # monotonic speech-chunk counter
    full_text_parts: list       # resolved speech, joined for speech_end

    # ── Per-pass scratch (set by llm_pass, read by log_pass / should_continue)
    pending_tool_calls: list
    pass_content: str
    pass_started: float
    pass_ttft: Optional[float]
    pass_usage: dict
    pass_finish: Optional[str]
    pass_error: Optional[str]

    # ── Streaming side channel (carried by reference; never serialized) ──────
    emit: "asyncio.Queue"
