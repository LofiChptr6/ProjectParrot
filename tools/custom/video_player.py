"""Custom tool: video_player — floating YouTube player.

Opens any YouTube video (music, clip, lecture) in a floating, draggable,
resizable modal. Only one video plays at a time — opening a new ``video_id``
replaces the current one.

The tool is deliberately self-validating: before broadcasting to the web
client, it hits YouTube's oEmbed endpoint. oEmbed returns 200 only when the
video exists AND is embeddable; 404 (removed/private) and 401 (embedding
disabled) both cause the tool to return an error instead of showing a "Video
unavailable" frame to the user.

Mocha's job is just: open a video, or close the player on request. She does
NOT manage windowed vs minimized state — that is a user UI choice, handled
locally by button clicks.
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
            "(lofi loops, ambient tracks) and regular video clips — same modal "
            "for everything. Opening a new video replaces the current one; the "
            "modal persists across other UI changes (presentations, themes) and "
            "only closes via action='close' or the user clicking X. The tool "
            "verifies the video is embeddable via YouTube oEmbed before showing "
            "it — a video_id that fails this check returns an error."
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
                        "set_title = rename without reloading."
                    ),
                },
                "video_id": {
                    "type": "string",
                    "description": (
                        "YouTube video reference. Prefer a 'vid:XXXXXXXX' HANDLE "
                        "that appeared in a recent web_search / get_news result — "
                        "the handle resolves to the real 11-char video_id "
                        "automatically, so you never need to know or type the ID. "
                        "Raw 11-char IDs are still accepted for user-supplied "
                        "URLs, but DO NOT invent one from a video title — that "
                        "produces a 'Video unavailable' error. Required for "
                        "action=open."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Human-readable title shown in the modal header. If you "
                        "leave this blank, the tool uses YouTube's canonical "
                        "title from oEmbed. Required for action=set_title."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


async def _verify_embeddable(video_id: str) -> dict:
    """Ask YouTube's oEmbed endpoint whether this video exists and is embeddable.

    Returns ``{"ok": True, "title", "author"}`` on success, or
    ``{"ok": False, "reason", "status_code"}`` when the video is unavailable,
    embedding is disabled, or the network call fails.
    """
    import httpx

    oembed_url = (
        f"https://www.youtube.com/oembed?"
        f"url=https://www.youtube.com/watch?v={video_id}&format=json"
    )
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(oembed_url)
    except Exception as exc:
        return {"ok": False, "reason": f"oembed request failed: {exc}", "status_code": None}

    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
            return {
                "ok": True,
                "status_code": 200,
                "title": data.get("title", ""),
                "author": data.get("author_name", ""),
            }
        except Exception:
            return {"ok": False, "reason": "oembed returned non-JSON", "status_code": 200}
    if status == 401:
        reason = "embedding disabled by uploader — pick a different video"
    elif status == 404:
        reason = "video unavailable, private, removed, or fabricated"
    elif status == 403:
        reason = "oembed forbidden (usually region lock)"
    else:
        reason = f"oembed HTTP {status}"
    return {"ok": False, "status_code": status, "reason": reason}


async def execute(arguments: dict) -> str:
    action = (arguments.get("action") or "").strip().lower()
    if action not in ("open", "close", "set_title"):
        return f"Unknown action '{action}'. Use open / close / set_title."

    video_id = (arguments.get("video_id") or "").strip()
    title = (arguments.get("title") or "").strip()

    if action == "open":
        if not _VIDEO_ID_RE.match(video_id):
            return (
                f"Invalid video_id '{video_id}'. Expected an 11-char YouTube ID "
                "(letters/digits/_/-) or a 'vid:XXXXXXXX' handle from a recent "
                "search. Did you fabricate it? Re-run web_search and copy a "
                "handle from the result."
            )
        verdict = await _verify_embeddable(video_id)
        if not verdict.get("ok"):
            return json.dumps({
                "status": "error",
                "op": "open",
                "video_id": video_id,
                "reason": verdict.get("reason"),
                "status_code": verdict.get("status_code"),
                "hint": (
                    "Pick a different candidate from your last web_search — "
                    "the handle system guarantees you have real IDs, so just "
                    "use the next vid: handle."
                ),
            }, ensure_ascii=False)
        if not title:
            title = verdict.get("title") or "Now playing"

    if action == "set_title" and not title:
        return "set_title requires a title."

    try:
        from bridge.server import _broadcast_clients, _ws_clients, _set_open_modal, _clear_open_modal
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

    await _broadcast_clients(payload)
    return json.dumps({
        "status": "ok",
        "op": action,
        "video_id": video_id if action == "open" else None,
        "title": title if action in ("open", "set_title") else None,
        "clients": len(_ws_clients),
    }, ensure_ascii=False)
