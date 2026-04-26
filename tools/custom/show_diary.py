"""Custom tool: show_diary — open the diary flip-book modal in the UI.

Mocha fires this when the user says "show me my diary", "flip through the
pages", or similar. Takes an optional date to open at a specific day.
"""

from __future__ import annotations

from typing import Any

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "show_diary",
        "description": (
            "Open Mocha's diary modal on the user's screen. Each page is one "
            "day's summary + activity log, browsable like a book. Use when the "
            "user asks to see the diary, read past days, or flip through "
            "memories. Leave `date` blank to open at the most recent page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Optional YYYY-MM-DD to open at a specific page.",
                },
            },
        },
    },
}


async def execute(arguments: dict[str, Any]) -> str:
    date = (arguments.get("date") or "").strip() or None

    # Broadcast to the frontend via the same ui_command channel show_slides /
    # show_card use. Handled by web/static/js/diary.js.
    #
    # Deliberately NOT registering with _OPEN_MODALS — the diary modal is
    # user-dismissable (X button / Escape) and the backend can't know when
    # that happens, so a stuck "diary on screen" state would make Mocha skip
    # future re-open requests with "it's already open". Re-opening is cheap
    # and idempotent on the frontend, so we just always dispatch.
    from bridge.server import _broadcast_clients

    msg = {
        "type": "ui_command",
        "action": "show_diary",
        "date": date,
    }
    try:
        await _broadcast_clients(msg)
    except Exception as exc:
        return f"show_diary: broadcast failed: {exc}"

    return f"Diary opened{' at ' + date if date else ''}."
