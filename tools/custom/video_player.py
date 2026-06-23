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

import asyncio
import json
import os
import re
import uuid


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


# Continuous-music intent ("play some lofi", "ambient", "study beats"…). These
# queries are the ones that surface 24/7 LIVE streams, which pass oEmbed but then
# die in the embed with "This live stream recording is not available" the moment
# the stream rotates its video id or goes offline. We bias such queries toward
# long VOD uploads (mixes) instead.
_MUSIC_LOOP_RE = re.compile(
    r"\b(lofi|lo-fi|ambient|chill\w*|study|relax\w*|beats|white ?noise|"
    r"rain|jazz|focus|sleep|background|radio|loop|playlist|mix)\b",
    re.IGNORECASE,
)
# Title markers that scream "this is a live stream" (deprioritized) vs "this is a
# finite recording that plays reliably in an embed" (preferred). "radio" is the
# real-world tell for the 24/7 lofi streams ("lofi hip hop radio 📚 …"); it's only
# applied AFTER the VOD check below, so "lofi radio MIX" still ranks as a VOD.
_LIVE_TITLE_RE = re.compile(r"(🔴|\blive\b|live ?now|live ?stream|24/?7|24 ?hours?|\bradio\b)", re.IGNORECASE)
_VOD_TITLE_RE = re.compile(r"\b(\d+\s*(?:hours?|hrs?|min)|mix|compilation|playlist|full album)\b", re.IGNORECASE)


def _yt_candidates_from_brave(data: dict) -> list[tuple[str, str]]:
    """Extract (video_id, title) pairs from a Brave web-search JSON payload, in
    order, de-duplicated by id. Pure (no network) so it's unit-testable."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for res in (data.get("web", {}).get("results") or []):
        title = res.get("title", "") or ""
        for field in (res.get("url", ""), title, res.get("description", "")):
            m = _YT_WATCH_RE.search(field or "")
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                out.append((m.group(1), title))
    return out


def _yt_ids_from_brave(data: dict) -> list[str]:
    """Extract YouTube video IDs from a Brave web-search JSON payload, in order,
    de-duplicated. Pure (no network) so it's unit-testable."""
    return [vid for vid, _title in _yt_candidates_from_brave(data)]


def _rank_candidates_avoiding_live(candidates: list[tuple[str, str]]) -> list[str]:
    """Stable-reorder (id, title) candidates so finite recordings come first and
    live streams come last, then drop titles. Stable sort preserves Brave's own
    relevance order within each bucket."""
    def _bucket(c: tuple[str, str]) -> int:
        _id, title = c
        # VOD check wins first, so "lofi radio MIX" ranks as a finite recording
        # even though it also contains the live-ish word "radio".
        if _VOD_TITLE_RE.search(title):
            return 0
        if _LIVE_TITLE_RE.search(title):
            return 2
        return 1
    return [vid for vid, _t in sorted(candidates, key=_bucket)]


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
    # For continuous-music requests, steer Brave toward finite VOD mixes rather
    # than the 24/7 live streams that "lofi … radio" otherwise returns — those
    # pass oEmbed but die in the embed once the stream rotates id / goes offline.
    is_music_loop = bool(_MUSIC_LOOP_RE.search(query))
    search_q = f"{query} mix 1 hour site:youtube.com" if is_music_loop else f"{query} site:youtube.com"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": search_q, "count": 10},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"ok": False, "reason": f"YouTube search failed: {exc}"}

    found = _yt_candidates_from_brave(data)
    if not found:
        return {"ok": False, "reason": f"no YouTube videos found for '{query}'"}
    # Music loops: deprioritize live-titled results so a finite recording wins.
    candidates = _rank_candidates_avoiding_live(found) if is_music_loop else [v for v, _t in found]
    for vid in candidates[:6]:
        verdict = await _verify_embeddable(vid)
        if verdict.get("ok"):
            return {"ok": True, "video_id": vid, "title": verdict.get("title", "")}
    return {"ok": False, "reason": f"found videos for '{query}' but none were embeddable"}


def _order_candidates(found: list[tuple[str, str]], is_music: bool) -> list[tuple[str, str]]:
    """Order (id, title) candidate pairs for play. Music loops get the VOD-first /
    live-last ranking; everything else keeps Brave's relevance order. Pure."""
    if not is_music:
        return list(found)
    order_ids = _rank_candidates_avoiding_live(found)
    title_by = {vid: title for vid, title in found}
    return [(vid, title_by.get(vid, "")) for vid in order_ids]


async def _resolve_candidates(query: str) -> dict:
    """Resolve an ORDERED candidate list for a plain-language query: a first
    oEmbed-verified video as primary, plus the remaining search hits as fallbacks
    the web player can cycle through.

    Why a list and not one id: oEmbed 200 does NOT guarantee the IFrame will play
    (classical/label uploads routinely pass oEmbed yet throw embed error 101/150 at
    play time). The browser is the only authoritative judge, so we hand it several
    real candidates and let it land on one that actually plays. Returns
    ``{"ok": True, "candidates": [{"id","title"}, …]}`` or ``{"ok": False, "reason"}``.
    """
    key = os.environ.get("BRAVE_API_KEY", "")
    if not key:
        return {"ok": False, "reason": "BRAVE_API_KEY not set — can't search YouTube"}
    is_music_loop = bool(_MUSIC_LOOP_RE.search(query))
    search_q = f"{query} mix 1 hour site:youtube.com" if is_music_loop else f"{query} site:youtube.com"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": search_q, "count": 10},
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"ok": False, "reason": f"YouTube search failed: {exc}"}

    found = _yt_candidates_from_brave(data)
    if not found:
        return {"ok": False, "reason": f"no YouTube videos found for '{query}'"}

    ordered = _order_candidates(found, is_music_loop)
    # Pick the first oEmbed-verified video as primary (a cheap pre-filter that
    # drops removed/private picks), keep the rest as fallbacks for the player to
    # cycle. oEmbed-failing ones still ride along as low-priority fallbacks — the
    # IFrame is the real test, and a 401 here sometimes still plays there.
    primary: dict | None = None
    fallbacks: list[dict] = []
    for vid, title in ordered[:8]:
        if primary is None:
            verdict = await _verify_embeddable(vid)
            if verdict.get("ok"):
                primary = {"id": vid, "title": verdict.get("title") or title}
                continue
        fallbacks.append({"id": vid, "title": title})

    if primary is None:
        if not fallbacks:
            return {"ok": False, "reason": f"no candidates for '{query}'"}
        return {"ok": True, "candidates": fallbacks[:6]}
    rest = [f for f in fallbacks if f["id"] != primary["id"]]
    return {"ok": True, "candidates": [primary] + rest[:5]}


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

    candidates: list[dict] = []
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
            res = await _resolve_candidates(query)
            if not res.get("ok"):
                return json.dumps({
                    "status": "error", "op": "open", "query": query,
                    "reason": res.get("reason"),
                    "hint": "Try a more specific query (artist + track), or pass a vid: handle.",
                }, ensure_ascii=False)
            candidates = res["candidates"]
            video_id = candidates[0]["id"]
            resolved_from_query = True
            if not title:
                title = candidates[0].get("title") or "Now playing"

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
            candidates = [{"id": video_id, "title": title}]

    if action == "set_title" and not title:
        return "set_title requires a title."

    try:
        from bridge.server import (
            _broadcast_clients, _ws_clients, _set_open_modal, _clear_open_modal,
            register_play_waiter, discard_play_waiter,
        )
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    play_id = uuid.uuid4().hex[:10] if action == "open" else ""
    payload = {
        "type": "ui_command",
        "action": "video_player",
        "op": action,
    }
    if action == "open":
        payload["video_id"] = video_id
        payload["title"] = title
        payload["candidates"] = candidates   # web player cycles these on embed failure
        payload["play_id"] = play_id
        _set_open_modal("video_player", {"video_id": video_id, "title": title})
    elif action == "set_title":
        payload["title"] = title
        _set_open_modal("video_player", {"title": title})
    elif action == "close":
        _clear_open_modal("video_player")

    # Register the playback waiter BEFORE broadcasting so the client's report can't
    # race ahead. Only wait when a web client is actually present to play it.
    waiter = register_play_waiter(play_id) if (action == "open" and _ws_clients) else None

    await _broadcast_clients(payload)

    if waiter is not None:
        # Block until the web player confirms a candidate ACTUALLY played, or that
        # every candidate was refused at play time. This is what makes the tool's
        # "done" mean "played" rather than merely "sent".
        try:
            result = await asyncio.wait_for(waiter, timeout=12.0)
        except asyncio.TimeoutError:
            result = None
        finally:
            discard_play_waiter(play_id)
        if result is not None:
            if result.get("ok"):
                played_id = result.get("video_id") or video_id
                played_title = result.get("title") or title
                _set_open_modal("video_player", {"video_id": played_id, "title": played_title})
                return json.dumps({
                    "status": "ok", "op": "open", "video_id": played_id,
                    "title": played_title, "played": True,
                    "candidates_tried": len(candidates),
                }, ensure_ascii=False)
            # Every candidate was refused — report a real failure so the caller
            # (or the task runtime's ask path) can offer a different recording.
            _clear_open_modal("video_player")
            return json.dumps({
                "status": "error", "op": "open", "video_id": video_id,
                "reason": ("none of the candidates actually played in the browser — "
                           "embedding is blocked or the videos are unavailable"),
                "candidates_tried": len(candidates),
                "hint": "Offer a different recording/version, or ask which one they want.",
            }, ensure_ascii=False)
        # Timeout (slow/no report) → don't hang or falsely fail; report optimistically.

    return json.dumps({
        "status": "ok",
        "op": action,
        "video_id": video_id if action == "open" else None,
        "title": title if action in ("open", "set_title") else None,
        "clients": len(_ws_clients),
    }, ensure_ascii=False)
