"""
Tool executor — runs tool calls requested by the LLM and provides results.

Also contains the ReAct-style ``tool_loop`` that alternates between LLM
inference and tool execution until the model produces a final conversational
answer (no more tool calls).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from pathlib import Path

import yaml

from bridge.call_log import CallContext
from bridge import call_log
from tools.registry import TOOL_SCHEMAS, TOOL_NAMES

log = logging.getLogger("tools")

ROOT = Path(__file__).resolve().parent.parent
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
_tools_cfg = _cfg.get("tools", {})
_WORKING_DIR = _tools_cfg.get("working_dir", str(ROOT))
_MAX_TOOL_ROUNDS = _tools_cfg.get("max_rounds", 5)
_TOOL_TIMEOUT = _tools_cfg.get("timeout", 30)
_BASH_BLOCKLIST_DEFAULT = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){ :|:&};:",
    ":(){:|:&};:",
    ">/dev/sd",
    "> /dev/sd",
    "chmod -R 777 /",
]
_BASH_BLOCKLIST = list(_tools_cfg.get("bash_exec", {}).get("blocklist_patterns", _BASH_BLOCKLIST_DEFAULT))

# Custom tools registered by Shiro via tools/custom/ hot-reload
_CUSTOM_EXECUTORS: dict[str, callable] = {}


async def execute_tool(name: str, arguments: dict) -> str:
    """Dispatch a single tool call and return the result as a string."""
    try:
        if name == "bash_exec":
            return await _bash_exec(
                arguments["command"],
                working_dir=arguments.get("working_dir", _WORKING_DIR),
                timeout=arguments.get("timeout", _TOOL_TIMEOUT),
            )
        elif name == "read_file":
            return _read_file(arguments["path"])
        elif name == "write_file":
            return _write_file(arguments["path"], arguments["content"])
        elif name == "git_status":
            return await _bash_exec("git status", working_dir=_WORKING_DIR, timeout=10)
        elif name == "list_dir":
            return _list_dir(arguments.get("path", _WORKING_DIR))
        elif name == "web_search":
            return _web_search(
                query=arguments.get("query", ""),
                max_results=int(arguments.get("max_results", 5)),
            )
        elif name in _CUSTOM_EXECUTORS:
            return await _CUSTOM_EXECUTORS[name](arguments)
        else:
            return f"Unknown tool: {name}"
    except Exception as exc:
        return f"Tool error ({name}): {exc}"


_BRAVE_API_KEY = "BSAztOV-ipiwcO1nXSMzAxIYl3kveoM"

def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Brave Search API and return top results."""
    log.info("Tool web_search: %r (max_results=%d)", query[:80], max_results)
    max_results = min(max(1, max_results), 10)
    try:
        import httpx
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": _BRAVE_API_KEY, "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in (data.get("web", {}).get("results") or [])[:max_results]:
            desc = r.get("description", "")
            title = r.get("title", "")
            url = r.get("url", "")
            results.append(f"{desc}\n(source: {title} — {url})")
        return "\n\n---\n\n".join(results) if results else "No results found."
    except Exception as exc:
        return f"Search failed: {exc}"


async def _bash_exec(command: str, working_dir: str = _WORKING_DIR, timeout: int = 30) -> str:
    """Run a shell command and return combined stdout+stderr."""
    log.info("Tool bash_exec: %s (cwd=%s)", command[:80], working_dir)
    normalized = " ".join((command or "").split())
    for pattern in _BASH_BLOCKLIST:
        if pattern and pattern in normalized:
            log.warning("bash_exec BLOCKED by policy: pattern=%r command=%r", pattern, command[:200])
            return f"Blocked by policy: command matches {pattern!r}"
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=working_dir,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode(errors="replace").strip()
        if len(output) > 4000:
            output = output[:4000] + "\n...(truncated)"
        return output or "(no output)"
    except asyncio.TimeoutError:
        proc.kill()
        return f"Command timed out after {timeout}s"
    except Exception as exc:
        return f"bash error: {exc}"


def _read_file(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_WORKING_DIR) / p
    if not p.is_file():
        return f"File not found: {p}"
    text = p.read_text(errors="replace")
    if len(text) > 8000:
        text = text[:8000] + "\n...(truncated)"
    return text


def _write_file(path: str, content: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_WORKING_DIR) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Wrote {len(content)} bytes to {p}"


def _list_dir(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(_WORKING_DIR) / p
    if not p.is_dir():
        return f"Not a directory: {p}"
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = []
    for e in entries[:100]:
        prefix = "d " if e.is_dir() else "f "
        lines.append(prefix + e.name)
    if len(entries) > 100:
        lines.append(f"... and {len(entries) - 100} more")
    return "\n".join(lines) or "(empty)"


async def tool_loop(
    user_text: str,
    memories: list[dict],
    llm_client,
    job_id: int = 0,
    log_ctx: CallContext | None = None,
) -> list[dict]:
    """ReAct-style loop: LLM may call tools, we execute and feed results back.

    After at most ``_MAX_TOOL_ROUNDS`` rounds of tool calls, or when the LLM
    produces a final answer without tool calls, we return the segment list.

    ``llm_client`` is the bridge's ``LLMClient`` instance.
    """

    from character.context import build_system_prompt
    from bridge.server import (
        conversation_history,
        MAX_HISTORY,
        _parse_llm_response,
        normalize_llm_segments,
        ANIMATION_MODE,
        _ANIMATION_CLIPS,
        _LLM_FALLBACK,
    )

    system_prompt = build_system_prompt(
        animation_mode=ANIMATION_MODE,
        animation_clips=_ANIMATION_CLIPS if ANIMATION_MODE == "llm_select" else None,
        tools_available=True,
    )

    messages = [{"role": "system", "content": system_prompt}]
    if memories:
        mem_lines = [f"- ({m['role']}): {m['text']}" for m in memories]
        messages.append({"role": "system", "content": "[Relevant memories from past conversations]:\n" + "\n".join(mem_lines)})
    for entry in conversation_history[-MAX_HISTORY:]:
        messages.append(entry)
    messages.append({"role": "user", "content": user_text})

    _base_ctx = log_ctx or CallContext(triggered_by="tool_loop", conversation_id=str(job_id))

    def _log_result(result: dict, msgs: list[dict], latency: float,
                    triggered_by: str, tool_round: int | None = None,
                    tools_provided: bool = True) -> None:
        ctx = dataclasses.replace(_base_ctx, triggered_by=triggered_by, tool_round=tool_round)
        _usage = result.get("usage", {})
        asyncio.create_task(call_log.log_call(
            ctx, model=llm_client.model,
            temperature=llm_client.default_temperature,
            max_tokens=llm_client.default_max_tokens,
            stream=False, tools_provided=tools_provided,
            messages=msgs,
            response_content=result.get("content"),
            response_tool_calls=result.get("tool_calls"),
            finish_reason=result.get("finish_reason"),
            error=result.get("_error"), latency_ms=latency,
            prompt_tokens=_usage.get("prompt_tokens"),
            completion_tokens=_usage.get("completion_tokens"),
            total_tokens=_usage.get("total_tokens"),
        ))

    _seen_calls: set[tuple] = set()  # dedup: prevent infinite retry loops

    for round_num in range(_MAX_TOOL_ROUNDS):
        try:
            _t0 = time.monotonic()
            result = await llm_client.chat(messages, tools=TOOL_SCHEMAS)
            _lat = (time.monotonic() - _t0) * 1000
            _log_result(result, messages, _lat, "tool_loop", tool_round=round_num + 1)
        except Exception as exc:
            log.error("Tool-loop LLM request failed: %s", exc)
            return [dict(_LLM_FALLBACK)]

        tool_calls = result.get("tool_calls")

        if not tool_calls:
            content = result.get("content", "")
            if not content:
                return [dict(_LLM_FALLBACK)]
            segments = _parse_llm_response(content)
            return await normalize_llm_segments(user_text, segments, job_id=job_id)

        # Deduplication: if all calls in this round are identical to ones already
        # executed, inject a nudge instead of running them again (breaks retry loops).
        dedup_keys = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            args_str = json.dumps(fn.get("arguments", {}), sort_keys=True) if isinstance(fn.get("arguments"), dict) else str(fn.get("arguments", ""))
            dedup_keys.append((fn.get("name", ""), args_str))
        if all(k in _seen_calls for k in dedup_keys):
            log.warning("Tool loop: all calls are duplicates — injecting give-up nudge")
            messages.append({"role": "user", "content": "The search didn't find useful results. Please give your best answer based on what you know, or acknowledge you couldn't find it."})
            _t0 = time.monotonic()
            result = await llm_client.chat(messages)
            _lat = (time.monotonic() - _t0) * 1000
            _log_result(result, messages, _lat, "tool_loop_nudge",
                        tool_round=round_num + 1, tools_provided=False)
            content = result.get("content", "")
            segments = _parse_llm_response(content) if content else [dict(_LLM_FALLBACK)]
            return await normalize_llm_segments(user_text, segments, job_id=job_id)
        _seen_calls.update(dedup_keys)

        # Process tool calls — re-serialize arguments for the OpenAI API
        api_msg = {
            "role": "assistant",
            "content": result.get("content") or "",
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
                for i, tc in enumerate(tool_calls)
            ],
        }
        messages.append(api_msg)
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            log.info("Tool call [round %d]: %s(%s)", round_num + 1, name, str(args)[:120])
            tool_result = await execute_tool(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{round_num}"),
                "content": tool_result,
            })

    # Exhausted rounds — force a final answer without tools
    try:
        _force_msgs = messages + [{"role": "user", "content": "Please provide your final answer now."}]
        _t0 = time.monotonic()
        result = await llm_client.chat(_force_msgs)
        _lat = (time.monotonic() - _t0) * 1000
        _log_result(result, _force_msgs, _lat, "tool_loop_force", tools_provided=False)
        content = result.get("content", "")
        if content:
            segments = _parse_llm_response(content)
            return await normalize_llm_segments(user_text, segments, job_id=job_id)
    except Exception:
        pass

    return [dict(_LLM_FALLBACK)]
