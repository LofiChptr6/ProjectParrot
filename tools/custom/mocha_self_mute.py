"""Custom tool: mocha_self_mute — temporarily silence autonomous check-ins."""

from __future__ import annotations

import time


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "mocha_self_mute",
        "description": (
            "Temporarily silence autonomous check-ins (bored/lonely/drift). Call this "
            "when Ika asks you to be quiet for a while ('hush', 'shut up for a bit', "
            "'give me a minute', 'let me focus'). Idle animations still play. "
            "Replying to direct user input is unaffected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration_s": {
                    "type": "integer",
                    "description": "Seconds to stay muted. Default 600 (10 min). Max 7200 (2 h).",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    from bridge.server import _mocha_state

    raw = arguments.get("duration_s", 600)
    try:
        duration = int(raw)
    except (TypeError, ValueError):
        duration = 600
    duration = max(30, min(duration, 7200))

    _mocha_state["muted_until_monotonic"] = time.monotonic() + duration

    minutes = duration // 60
    if minutes >= 1:
        label = f"{minutes} min"
    else:
        label = f"{duration}s"
    return f"Muted autonomous check-ins for {label}."
