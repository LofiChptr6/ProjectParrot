"""Nori — autonomous research analyst sub-agent.

Nori receives requests from Mocha, runs its own tool loop (data fetching,
analysis, presentation building), and returns narration guidance.

Nori is self-contained: owns its own LLM client, reads config directly,
and only depends on tools/executor for tool execution + bridge.server for
UI broadcasting (via the tools themselves, not direct imports).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import yaml

log = logging.getLogger("nori")

_SOUL_PATH = Path(__file__).parent / "soul.md"
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_MAX_ROUNDS = 6  # Nori gets more rounds than Mocha (research is multi-step)

import re

def _extract_tool_calls_from_text(content: str) -> list[dict] | None:
    """Extract tool calls from <tool_call> XML tags in content (Qwen3 quirk).

    Also handles raw JSON tool call objects in content.
    """
    if not content:
        return None

    calls = []

    # Pattern 1: <tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>
    # Also handles unclosed tags (LLM ran out of tokens before </tool_call>)
    tag_pattern = r'<tool_call>\s*(\{.*?)(?:</tool_call>|$)'
    for m in re.finditer(tag_pattern, content, re.DOTALL):
        raw = m.group(1).strip()
        # Use raw_decode to handle trailing garbage (extra braces, etc.)
        # Also try closing brackets for truncated JSON
        for attempt in [raw, raw + '}', raw + ']}', raw + ']}]}', raw + '"]}]}']:
            try:
                decoder = json.JSONDecoder()
                parsed, _ = decoder.raw_decode(attempt)
                if isinstance(parsed, dict) and "name" in parsed:
                    calls.append({
                        "id": f"extracted_{len(calls)}",
                        "function": {
                            "name": parsed["name"],
                            "arguments": parsed.get("arguments") or parsed.get("parameters") or {},
                        },
                    })
                    break
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    # Pattern 2: raw JSON starting with { or [ (no XML tags)
    if not calls:
        stripped = content.strip()
        if stripped and stripped[0] in ('{', '['):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and "name" in parsed:
                    calls.append({
                        "id": "extracted_0",
                        "function": {
                            "name": parsed["name"],
                            "arguments": parsed.get("arguments") or parsed.get("parameters") or {},
                        },
                    })
                elif isinstance(parsed, list):
                    for i, tc in enumerate(parsed):
                        if isinstance(tc, dict) and "name" in tc:
                            calls.append({
                                "id": f"extracted_{i}",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": tc.get("arguments") or tc.get("parameters") or {},
                                },
                            })
            except (json.JSONDecodeError, KeyError):
                pass

    return calls if calls else None

# Tool schemas Nori can use.
# Shell access (bash_exec) is gated by the blocklist in tools/executor.py.
_NORI_TOOL_NAMES = [
    "get_stock_data", "get_news", "show_weather", "calculate", "web_search",
    "show_slides", "show_card", "show_notification",
    "bash_exec",
    "schedule_cron", "list_cron_jobs", "cancel_cron_job",
    # Diary recall — Nori can look up past activity when researching
    "recall_diary",
    # Theme assembly: Nori is the URL-validator + HTML composer for the
    # three-agent theme flow. Hana plans, Nori fetches + validates + fires
    # theme_propose, Mocha just relays user approval.
    "validate_url", "theme_propose",
    # Video player: Nori finds a YouTube video (music or clip) and fires the
    # persistent floating player. Unified for music + general videos.
    "video_player",
    # Polygon.io (Massive) financial toolbox — higher-precision stock data
    # than yfinance-based get_stock_data. Nori-only; Mocha routes via ask_nori.
    "polygon_prev_close", "polygon_bars", "polygon_snapshot",
    "polygon_ticker_details", "polygon_ticker_news", "polygon_market_status",
]

# Lazy-initialized LLM client (Nori's own instance)
_llm_client = None


def _get_llm_client():
    """Create or return Nori's own LLM client (reads config directly)."""
    global _llm_client
    if _llm_client is None:
        from bridge.llm_client import LLMClient
        cfg = yaml.safe_load(_CONFIG_PATH.read_text())
        _llm_client = LLMClient(cfg.get("llm", {}))
        log.info("Nori LLM client initialized: %s", _llm_client.model)
    return _llm_client


def _build_nori_prompt() -> str:
    """Read Nori's soul file."""
    try:
        return _SOUL_PATH.read_text()
    except FileNotFoundError:
        return "You are Nori, a research analyst. Fetch data, build visuals, write narration for Mocha."


def _get_nori_tools() -> list[dict]:
    """Get tool schemas for the tools Nori is allowed to use."""
    from tools.registry import TOOL_DEFINITIONS
    return [TOOL_DEFINITIONS[n] for n in _NORI_TOOL_NAMES if n in TOOL_DEFINITIONS]


async def process_request(request: str, context: str = "") -> str:
    """Run Nori's research loop and return narration guidance for Mocha.

    Args:
        request: What Mocha wants Nori to research (translated from user intent).
        context: Optional extra context (e.g., recent conversation summary).

    Returns:
        String result for Mocha — either structured narration JSON or plain text.
    """
    llm_client = _get_llm_client()
    from bridge.call_log import log_call, CallContext
    from tools.executor import execute_tool

    nori_tools = _get_nori_tools()
    system_prompt = _build_nori_prompt()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]
    if context:
        messages.append({"role": "system", "content": f"[Context from conversation]: {context}"})
    messages.append({"role": "user", "content": request})

    log.info("Nori processing: %s", request[:120])
    total_start = time.monotonic()

    # Broadcast Nori status to frontend
    async def _nori_status(status: str, preview: str = "", elapsed_ms: float = 0):
        try:
            from bridge.server import _broadcast_monitor
            await _broadcast_monitor({
                "type": "thread_status",
                "thread": "nori",
                "status": status,
                "input_preview": preview[:30],
                "elapsed_ms": elapsed_ms,
            })
        except Exception:
            pass

    await _nori_status("processing", request)

    for round_num in range(_MAX_ROUNDS):
        await _nori_status("processing", f"Round {round_num + 1}: thinking...")

        t0 = time.monotonic()
        result = await llm_client.chat(
            messages,
            tools=nori_tools,
            enable_thinking=False,
            max_tokens=4096,  # Nori needs room for large tool calls (news articles, slide data)
        )
        latency = (time.monotonic() - t0) * 1000
        usage = result.get("usage", {})

        # Log to PostgreSQL
        asyncio.create_task(log_call(
            CallContext(triggered_by="nori", tool_round=round_num + 1),
            model=llm_client.model,
            temperature=llm_client.default_temperature,
            max_tokens=llm_client.default_max_tokens,
            stream=False,
            enable_thinking=False,
            tools_provided=True,
            messages=messages,
            response_content=result.get("content"),
            response_tool_calls=result.get("tool_calls"),
            finish_reason=result.get("finish_reason"),
            latency_ms=latency,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        ))

        # Check for tool calls (native field OR <tool_call> tags in content)
        tool_calls = result.get("tool_calls") or _extract_tool_calls_from_text(result.get("content", ""))
        if not tool_calls:
            # Nori is done — return final content
            content = result.get("content", "")
            total_ms = (time.monotonic() - total_start) * 1000
            log.info("Nori done in %.0fms (%d rounds):\n%s",
                     total_ms, round_num + 1, content[:1500])
            await _nori_status("idle", f"Done ({total_ms/1000:.1f}s)", total_ms)
            return content

        # Append assistant message with tool calls
        messages.append({
            "role": "assistant",
            "content": result.get("content") or "",
            "tool_calls": [
                {
                    "id": tc.get("id", f"nori_call_{round_num}_{i}"),
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
                for i, tc in enumerate(tool_calls)
            ],
        })

        # Execute each tool call
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            tool_name = fn.get("name", "unknown")
            tool_args = fn.get("arguments", {})
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except json.JSONDecodeError:
                    tool_args = {}

            log.info("Nori tool [round %d]: %s(%s)",
                     round_num + 1, tool_name, str(tool_args)[:100])
            elapsed = (time.monotonic() - total_start) * 1000
            await _nori_status("processing", f"R{round_num+1}: {tool_name}", elapsed)

            # Broadcast tool activity to monitor
            try:
                from bridge.server import _broadcast_monitor
                await _broadcast_monitor({
                    "type": "tool_activity", "action": "call",
                    "job_id": 0, "round": round_num + 1,
                    "tool_name": f"nori:{tool_name}",
                    "tool_args": json.dumps(tool_args)[:500],
                })
            except Exception:
                pass

            t_tool = time.monotonic()
            tool_result = await execute_tool(tool_name, tool_args)
            tool_ms = (time.monotonic() - t_tool) * 1000

            log.info("Nori tool result [round %d]: %.0fms, %s",
                     round_num + 1, tool_ms, tool_result[:100])

            try:
                await _broadcast_monitor({
                    "type": "tool_activity", "action": "result",
                    "job_id": 0, "round": round_num + 1,
                    "tool_name": f"nori:{tool_name}",
                    "result_preview": tool_result[:800],
                    "duration_ms": round(tool_ms, 1),
                })
            except Exception:
                pass

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"nori_call_{round_num}_{i}"),
                "content": tool_result,
            })

    # Exhausted rounds — force a final answer
    log.warning("Nori exhausted %d rounds, forcing answer", _MAX_ROUNDS)
    await _nori_status("processing", "Wrapping up...")
    messages.append({"role": "user", "content": (
        "Wrap up now. Return your narration JSON.\n\n"
        "CRITICAL: every specific number in your narration (prices, percentages, "
        "temperatures, counts, dates) must be a `num:XXXXXXXX` handle copied VERBATIM "
        "from the tool results above. Do NOT write a raw number like \"$39.75\" — you "
        "do not know the true value, only the handle. If a data tool returned "
        "`\"price\": \"num:3voHEDFQ\"`, write `num:3voHEDFQ` in your narration where "
        "Mocha should say the price. The bridge substitutes each handle with the real "
        "formatted value right before TTS. If you don't have a handle for a number, "
        "omit the claim entirely rather than inventing the number."
    )})
    result = await llm_client.chat(messages)
    total_ms = (time.monotonic() - total_start) * 1000
    await _nori_status("idle", f"Done ({total_ms/1000:.1f}s)", total_ms)
    return result.get("content", "I wasn't able to complete the research in time.")
