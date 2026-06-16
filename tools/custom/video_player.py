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
import os
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
                        "YouTube video reference. A 'vid:XXXXXXXX' HANDLE from a "
                        "recent web_search / get_news result resolves to the real "
                        "video_id automatically. Raw 11-char IDs are accepted for "
                        "user-supplied URLs. DO NOT invent one from a title. "
                        "OPTIONAL when you pass `query` instead (preferred for "
                        "'play some X' requests)."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "What to play, in plain words — e.g. 'lofi hip hop', "
                        "'ambient music', 'Rick Astley Never Gonna Give You Up'. "
                        "The tool searches YouTube and picks a real, embeddable "
                        "video for you (oEmbed-verified), so for 'play some X' just "
                        "call video_player(action='open', query='X') — you do NOT "
                        "need to web_search first or know any video_id. Used only "
                        "when no valid video_id/handle is given."
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

_YT_WATCH_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def _yt_ids_from_brave(data: dict) -> list[str]:
    """Extract YouTube video IDs from a Brave web-search JSON payload, in order,
    de-duplicated. Pure (no network) so it's unit-testable."""
    out: list[str] = []
    for res in (data.get("web", {}).get("results") or []):
        for field in (res.get("url", ""), res.get("title", ""), res.get("description", "")):
            m = _YT_WATCH_RE.search(field or "")
            if m and m.group(1) not in out:
                out.append(m.group(1))
    return out


async def _resolve_youtube_id(query: str) -> dict:
    """Find a real, embeddable YouTube video for a plain-language query.

    Searches Brave scoped to YouTube, then oEmbed-verifies candidates in order and
    returns the first that exists AND embeds. This is what lets Mocha satisfy
    'play some ambient music' without the model having to extract an ID from
    handle-redacted web results. Returns {"ok", "video_id", "title"} or
    {"ok": False, "reason"}.
    """
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return {"ok": False, "reason": "BRAVE_API_KEY not set — can't search YouTube"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": f"{query} site:youtube.com", "count": 10},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"ok": False, "reason": f"YouTube search failed: {exc}"}

    candidates = _yt_ids_from_brave(data)
    if not candidates:
        return {"ok": False, "reason": f"no YouTube videos found for '{query}'"}
    for vid in candidates[:6]:
        verdict = await _verify_embeddable(vid)
        if verdict.get("ok"):
            return {"ok": True, "video_id": vid, "title": verdict.get("title", "")}
    return {"ok": False, "reason": f"found videos for '{query}' but none were embeddable"}


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
        resolved_from_query = False
        if not _VIDEO_ID_RE.match(video_id):
            # No usable id/handle — resolve a real video from a plain query.
            # This is the robust path for "play some ambient music": the tool
            # searches + verifies a YouTube video itself instead of relying on the
            # model to extract an ID from handle-redacted web_search output.
            query = (arguments.get("query") or "").strip()
            if not query:
                return (
                    f"Invalid/empty video_id '{video_id}' and no query. For "
                    "'play some X' just call video_player(action='open', "
                    "query='X') — I'll find a playable video. A 'vid:XXXXXXXX' "
                    "handle from a recent search also works; never fabricate an ID."
                )
            found = await _resolve_youtube_id(query)
            if not found.get("ok"):
                return json.dumps({
                    "status": "error", "op": "open", "query": query,
                    "reason": found.get("reason"),
                    "hint": "Try a more specific query (artist + track), or pass a vid: handle.",
                }, ensure_ascii=False)
            video_id = found["video_id"]
            resolved_from_query = True
            if not title:
                title = found.get("title") or "Now playing"

        if not resolved_from_query:
            # An id/handle was supplied directly — verify it embeds.
            verdict = await _verify_embeddable(video_id)
            if not verdict.get("ok"):
                return json.dumps({
                    "status": "error",
                    "op": "open",
                    "video_id": video_id,
                    "reason": verdict.get("reason"),
                    "status_code": verdict.get("status_code"),
                    "hint": (
                        "Pick a different candidate from your last web_search "
                        "(use the next vid: handle), or pass a `query` and I'll "
                        "find one myself."
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
