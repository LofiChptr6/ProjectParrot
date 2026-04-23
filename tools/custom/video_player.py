"""Custom tool: video_player — floating YouTube player.

Unified replacement for the old music_player + show_video tools. Any YouTube
video (music, clip, lecture) opens in a floating, draggable, resizable modal
that the user can minimize/expand themselves via the modal's own controls.

Mocha's job is just: open a video_id, or close the player on request. She
does NOT manage windowed vs minimized state — that's the user's UI choice,
handled locally by button clicks.
"""

from __future__ import annotations

import json
import re


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "video_player",
        "description": (
            "Open or close the floating YouTube video player. Covers both music "
            "(lofi loops, ambient tracks) and regular video clips — it's the same "
            "modal for everything. Only ONE video plays at a time; opening a new "
            "video_id replaces the current one. The modal persists across other UI "
            "changes (presentations opening, theme changes) — it only closes when "
            "you call action='close' or the user clicks the X. The user drags/"
            "resizes/minimizes the modal themselves; do NOT call minimize/expand."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "close", "set_title"],
                    "description": (
                        "open = start a new video (requires video_id); "
                        "close = stop and remove the player; "
                        "set_title = rename without reloading (e.g. when you learn the title after opening)."
                    ),
                },
                "video_id": {
                    "type": "string",
                    "description": (
                        "YouTube video ID — EXACTLY 11 chars (letters/digits/_/-). "
                        "The part after v= in youtube.com/watch?v=... or after youtu.be/. "
                        "CRITICAL: use an ID that appeared verbatim in a web_search "
                        "result — do NOT fabricate IDs from memory. Required for action=open."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title shown in the modal header. Required for action=open and action=set_title.",
                },
            },
            "required": ["action"],
        },
    },
}


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


async def execute(arguments: dict) -> str:
    action = (arguments.get("action") or "").strip().lower()
    if action not in ("open", "close", "set_title"):
        return f"Unknown action '{action}'. Use open / close / set_title."

    video_id = (arguments.get("video_id") or "").strip()
    title = (arguments.get("title") or "").strip()

    if action == "open":
        if not _VIDEO_ID_RE.match(video_id):
            return (
                f"Invalid video_id '{video_id}'. YouTube IDs are 11 chars "
                "(letters/digits/_/-). Did you fabricate it? Re-check your web_search result."
            )
        if not title:
            title = "Now playing"
    if action == "set_title" and not title:
        return "set_title requires a title."

    try:
        from bridge.server import _broadcast_to_unity, unity_clients, _set_open_modal, _clear_open_modal
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    payload = {
        "type": "ui_command",
        "action": "video_player",
        "op": action,
    }
    if action == "open":
        payload["video_id"] = video_id
        payload["title"] = title
        _set_open_modal("video_player", {"video_id": video_id, "title": title})
    elif action == "set_title":
        payload["title"] = title
        _set_open_modal("video_player", {"title": title})
    elif action == "close":
        _clear_open_modal("video_player")

    await _broadcast_to_unity(payload)
    return json.dumps({
        "status": "ok",
        "op": action,
        "video_id": video_id if action == "open" else None,
        "title": title if action in ("open", "set_title") else None,
        "clients": len(unity_clients),
    }, ensure_ascii=False)
