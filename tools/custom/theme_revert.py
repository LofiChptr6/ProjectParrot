"""Custom tool: theme_revert — drop the live theme preview.

Removes the <style id="theme-preview"> element from all connected web clients
and clears the bridge-side mirror. Does NOT touch the active theme on disk.
"""

from __future__ import annotations

import json


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_revert",
        "description": (
            "Remove the live theme preview from all web clients. Use this when "
            "Ika rejects a proposed look, or before starting a completely new "
            "direction. Does NOT touch any saved theme or style.css on disk."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


async def execute(arguments: dict) -> str:
    try:
        from bridge.server import _broadcast_to_unity, unity_clients, _reset_active_preview
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    _reset_active_preview()

    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "revert_theme",
    })

    return json.dumps({
        "status": "preview_cleared",
        "clients": len(unity_clients),
    }, ensure_ascii=False)
