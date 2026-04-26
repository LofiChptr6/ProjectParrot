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
from contextvars import ContextVar
from pathlib import Path

import yaml

from bridge.call_log import CallContext
from bridge import call_log
from tools.registry import TOOL_SCHEMAS, TOOL_NAMES
from tools.handle_registry import (
    resolve_handles_in_args,
    substitute_urls_with_handles,
)

# Per-task capture for /admin/eval. When non-None, execute_tool appends one
# record per call (args before/after handle resolution, result preview + size,
# latency, ok, round). Stays None on the live path — zero overhead.
EVAL_CAPTURE: ContextVar[list[dict] | None] = ContextVar("eval_capture", default=None)
# Tool round counter within a single eval. Bumped once per tool_loop round so
# captured records know which round they came from; the live path never reads
# or sets it.
EVAL_TOOL_ROUND: ContextVar[int] = ContextVar("eval_tool_round", default=0)
# ProjectParrot user_id of the user whose turn is currently being processed.
# Set by _run_inline_turn so that cron/diary tools can tag their output.
TOOL_USER_ID: ContextVar[str | None] = ContextVar("tool_user_id", default=None)

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

# Tools whose output is local/structural — skip URL→handle substitution here.
_NO_SUBSTITUTE = {"bash_exec", "read_file", "write_file", "list_dir", "git_status"}


async def execute_tool(name: str, arguments: dict) -> str:
    """Dispatch a single tool call and return the result as a string.

    Two registry hooks wrap the dispatch so tools themselves stay handle-naive:

      pre-dispatch:   resolve any handle in ``arguments`` to its real URL/ID
      post-dispatch:  substitute raw URLs in the output with opaque handles

    The LLM therefore only ever sees handles; real URLs round-trip invisibly
    through the tool layer.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    resolved_args = resolve_handles_in_args(arguments)

    _capture = EVAL_CAPTURE.get()
    _t0 = time.monotonic()
    _ok = True
    try:
        if name == "bash_exec":
            result = await _bash_exec(
                resolved_args["command"],
                working_dir=resolved_args.get("working_dir", _WORKING_DIR),
                timeout=resolved_args.get("timeout", _TOOL_TIMEOUT),
            )
        elif name == "read_file":
            result = _read_file(resolved_args["path"])
        elif name == "write_file":
            result = _write_file(resolved_args["path"], resolved_args["content"])
        elif name == "git_status":
            result = await _bash_exec("git status", working_dir=_WORKING_DIR, timeout=10)
        elif name == "list_dir":
            result = _list_dir(resolved_args.get("path", _WORKING_DIR))
        elif name == "web_search":
            result = _web_search(
                query=resolved_args.get("query", ""),
                max_results=int(resolved_args.get("max_results", 5)),
            )
        elif name in _CUSTOM_EXECUTORS:
            result = await _CUSTOM_EXECUTORS[name](resolved_args)
        else:
            result = f"Unknown tool: {name}"
            _ok = False
    except Exception as exc:
        result = f"Tool error ({name}): {exc}"
        _ok = False

    # Hide raw URLs behind handles before the LLM sees the output. Skip tools
    # whose output is local/structural (bash, file IO, dir listings) — those
    # don't leak URLs to the LLM in practice and substituting there would
    # break any exact-string reasoning.
    if _ok and name not in _NO_SUBSTITUTE:
        try:
            result = substitute_urls_with_handles(result)
        except Exception as exc:
            log.warning("handle substitution failed for %s: %s", name, exc)

    if _capture is not None:
        _result_str = result if isinstance(result, str) else str(result)
        _capture.append({
            "round": EVAL_TOOL_ROUND.get(),
            "name": name,
            "arguments_raw": arguments,
            "arguments_resolved": resolved_args,
            "result_preview": _result_str[:800],
            "result_bytes": len(_result_str),
            "latency_ms": round((time.monotonic() - _t0) * 1000, 1),
            "ok": _ok,
        })

    # Feed every successful tool call into the session scratchpad so Mocha
    # has within-session recall ("play that again") and the diary writer has
    # the day's activity log. Fire-and-forget, never blocking the tool path.
    if _ok:
        try:
            from bridge import session_scratchpad
            session_scratchpad.add(name, arguments, result)
        except Exception:
            pass

    return result


# Brave Search API key — read from env at call time so rotation doesn't need
# a code change. Export BRAVE_API_KEY before launching the bridge.
_BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

def _web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Brave Search API and return top results."""
    log.info("Tool web_search: %r (max_results=%d)", query[:80], max_results)
    max_results = min(max(1, max_results), 10)
    if not _BRAVE_API_KEY:
        return "[web_search disabled: BRAVE_API_KEY env var not set]"
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


