"""Custom tool: theme_screenshot — capture the current UI as a base64 PNG.

Fires a ui_command to any connected web client, waits for the html2canvas
reply, and returns the base64 PNG so Mocha can immediately pipe it into
ask_hana(task='critique', screenshot_b64=...).
"""

from __future__ import annotations

import json


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_screenshot",
        "description": (
            "Capture the current web UI (including any active theme preview) as a "
            "base64 PNG. Returns { image_b64, bytes } or { error }. Typical flow: "
            "theme_propose → theme_screenshot → ask_hana(task='critique', "
            "screenshot_b64=<value>, notes='...'). Capture can take 5-12s because "
            "of the 3D VRM canvas; the default wait is plenty — DO NOT pass a "
            "smaller value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wait_ms": {
                    "type": "integer",
                    "description": (
                        "Milliseconds to wait for the capture reply (default 18000; "
                        "min 6000; max 30000). The 3D canvas makes captures slow — "
                        "do NOT set this below 10000 unless you have a reason."
                    ),
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    timeout_ms = arguments.get("wait_ms", 18000)
    try:
        timeout_s = float(timeout_ms) / 1000.0
    except (TypeError, ValueError):
        timeout_s = 18.0
    # Floor at 6s — anything lower is useless given html2canvas latency.
    timeout_s = max(6.0, min(timeout_s, 30.0))

    try:
        from bridge.server import request_screenshot, _ws_clients
    except Exception as exc:
        return json.dumps({"error": f"bridge unavailable: {exc}"}, ensure_ascii=False)

    if not _ws_clients:
        return json.dumps(
            {"error": "No web UI connected to capture from. Ask Ika to open the page."},
            ensure_ascii=False,
        )

    try:
        image_b64 = await request_screenshot(timeout=timeout_s)
    except Exception as exc:
        return json.dumps({"error": f"capture failed: {exc}"}, ensure_ascii=False)

    if not image_b64:
        return json.dumps(
            {"error": f"Screenshot did not arrive within {timeout_s:.1f}s."},
            ensure_ascii=False,
        )

    # Truncated summary for Mocha's log; the full b64 goes into the payload for
    # downstream tool calls.
    return json.dumps({
        "status": "ok",
        "bytes": len(image_b64),
        "image_b64": image_b64,
    }, ensure_ascii=False)
