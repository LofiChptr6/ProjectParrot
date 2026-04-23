"""Shiro analysis engine — builds meta-analysis prompts and parses LLM output."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from bridge.call_log import CallContext
from bridge import call_log
from bridge.llm_client import LLMClient

log = logging.getLogger("shiro.analyzer")

ROOT = Path(__file__).resolve().parent.parent

_SYSTEM_PROMPT = """\
You are **Shiro**, a coaching and self-improvement agent for an AI assistant named **Mocha**.
You review conversations between Mocha and her user (Ika) to find patterns and improvement opportunities.

## Your Task

Analyze the conversation transcript below and produce a structured analysis.

### Focus Areas

1. **USER PERSONALITY**: Infer Ika's personality, preferences, communication style, current emotional state, and priorities from these conversations.

2. **TRUE INTENT**: For each user message, identify what the user was *actually* trying to achieve — look beyond the literal words. Consider context, subtext, and unstated goals.

3. **SATISFACTION**: Did Mocha's response *actually* satisfy the user's true intent? Consider:
   - Was the answer complete?
   - Was the tone appropriate?
   - Did Mocha miss the point?
   - Was anything the user wanted left unaddressed?

4. **IMPROVEMENTS**: Propose concrete, actionable changes to make Mocha better serve Ika. Types:
   - `behavior_add`: Add a new conditional behavior rule
   - `behavior_modify`: Change an existing behavior rule
   - `soul_edit`: Modify Mocha's personality description
   - `new_tool`: Implement a capability Mocha currently lacks

## Current Mocha Configuration

### Personality (soul.md):
{soul_md}

### Behavior Rules (behaviors.yaml):
{behaviors_yaml}

### Available Tools:
{tool_names}

### Previous Shiro Analyses (for continuity):
{past_analyses}

## Output Format

Reply with valid JSON only. No markdown fences. No extra text.

{{
  "summary": "2-3 sentence overview of conversation patterns and key observations",
  "user_profile_update": "New observations about Ika that Mocha should be aware of (or null if nothing new)",
  "exchanges": [
    {{
      "user_said": "exact or paraphrased user message",
      "true_intent": "what the user actually wanted",
      "mocha_response_quality": "good|okay|poor",
      "issue": "what went wrong (null if good)"
    }}
  ],
  "proposed_changes": [
    {{
      "type": "behavior_add|behavior_modify|soul_edit|new_tool",
      "description": "What to change and why (human-readable summary)",
      "details": {{}}
    }}
  ]
}}

### Details format by type:

**behavior_add**:
{{"condition": "when this applies", "emotion": "emotion_id", "action": "physical action", "speech": "tone guidance", "priority": 5}}

**behavior_modify**:
{{"match_condition": "original condition text to find", "new_fields": {{"speech": "updated guidance", ...}}}}

**soul_edit**:
{{"section": "section heading (e.g. Core Personality)", "action": "append|replace", "content": "text to add or replace with"}}

**new_tool**:
{{"name": "tool_name_snake_case", "description": "what it does", "parameters": {{"param": "description"}}, "implementation_hint": "what the Python code should do"}}

If there are no improvements to propose, return an empty `proposed_changes` list. Only propose changes with clear justification.
"""


def _strip_think_tags(text: str) -> str:
    """Remove Qwen3 <think>...</think> blocks from response."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _load_soul_md() -> str:
    p = ROOT / "character" / "soul.md"
    return p.read_text() if p.is_file() else "(not found)"


def _load_behaviors_yaml() -> str:
    p = ROOT / "character" / "behaviors.yaml"
    return p.read_text() if p.is_file() else "(not found)"


def _load_tool_names() -> str:
    try:
        from tools.registry import TOOL_NAMES
        return ", ".join(TOOL_NAMES) if TOOL_NAMES else "(none)"
    except Exception:
        return "(unavailable)"


def _format_conversations(conversations: list[list[dict]]) -> str:
    """Format conversation groups into a readable transcript."""
    parts: list[str] = []
    for i, group in enumerate(conversations):
        parts.append(f"--- Conversation {i + 1} ---")
        for ex in group:
            # Skip non-primary passes (pass 1 routing, tool rounds)
            if ex.get("pass_number") == 1 or ex.get("tool_round"):
                continue
            ts = ex.get("timestamp", "")[:19]
            src = ex.get("source", "")
            user = ex.get("user", "").strip()
            assistant = ex.get("assistant", "").strip()
            if user:
                parts.append(f"[{ts}] [{src}] Ika: {user}")
            if assistant:
                # Truncate very long responses
                preview = assistant[:500] + "..." if len(assistant) > 500 else assistant
                parts.append(f"  Mocha: {preview}")
        parts.append("")
    return "\n".join(parts)


def _format_past_analyses(analyses: list[dict]) -> str:
    if not analyses:
        return "(no prior analyses)"
    parts: list[str] = []
    for a in analyses[-3:]:
        summary = a.get("summary", "(no summary)")
        parts.append(f"- {summary}")
    return "\n".join(parts)


async def analyze(
    llm: LLMClient,
    conversations: list[list[dict]],
    past_analyses: list[dict],
    *,
    temperature: float = 0.6,
    max_tokens: int = 4096,
    enable_thinking: bool = True,
) -> dict[str, Any] | None:
    """Run Shiro's meta-analysis on recent conversations.

    Returns parsed JSON analysis dict, or None on failure.
    """
    soul_md = _load_soul_md()
    behaviors_yaml = _load_behaviors_yaml()
    tool_names = _load_tool_names()
    transcript = _format_conversations(conversations)

    if not transcript.strip():
        log.info("No conversation content to analyze")
        return None

    system = _SYSTEM_PROMPT.format(
        soul_md=soul_md,
        behaviors_yaml=behaviors_yaml,
        tool_names=tool_names,
        past_analyses=_format_past_analyses(past_analyses),
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Conversation transcript to analyze:\n\n{transcript}"},
    ]

    t0 = time.monotonic()
    result = await llm.chat(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
    )
    latency_ms = (time.monotonic() - t0) * 1000

    # Log to PG
    usage = result.get("usage", {})
    asyncio.create_task(call_log.log_call(
        CallContext(triggered_by="shiro"),
        model=llm.model,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
        enable_thinking=enable_thinking,
        tools_provided=False,
        messages=messages,
        response_content=result.get("content"),
        finish_reason=result.get("finish_reason"),
        error=result.get("_error"),
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    ))

    content = result.get("content", "")
    if not content:
        log.error("Shiro LLM response empty")
        return None

    content = _strip_think_tags(content)

    # Try to parse JSON
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        log.error("Failed to parse Shiro analysis JSON: %s", content[:200])
        return None
