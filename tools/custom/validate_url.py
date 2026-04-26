"""Custom tool: validate_url — HEAD-check a URL's reachability + content-type.

Nori uses this before handing any asset URL to theme_propose. Catches dead
links, hotlink-blocked CDNs, and page URLs masquerading as image/audio.

YouTube URLs auto-route to oEmbed (a plain HEAD/GET returns 200 even for
unavailable videos because the page renders). oEmbed returns 404 for
removed/private videos and 401 for embedding-disabled — both caught here.
"""

from __future__ import annotations

import json
import re

import httpx


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "validate_url",
        "description": (
            "HEAD-check a URL and return {status_code, content_type, final_url, ok, reason}. "
            "Use BEFORE passing any image/audio/video URL to theme_propose, video_player, "
            "or to the user. A URL is OK iff status_code is 200-299 AND content_type "
            "starts with the expected prefix for its kind (image/* for images, audio/* "
            "for audio). 404 and 403 (hotlink blocked) both mean the URL is unusable. "
            "For YouTube URLs (youtube.com/watch, youtu.be, youtube.com/embed, "
            "youtube.com/shorts), the tool auto-switches to YouTube's oEmbed endpoint "
            "which actually verifies the video exists AND is embeddable — a plain "
            "HEAD would return 200 even for an unavailable video."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL to check (http:// or https://).",
                },
                "expected_kind": {
                    "type": "string",
                    "enum": ["image", "audio", "video", "any"],
                    "description": (
                        "'image' requires content-type image/*, 'audio' requires audio/* or "
                        "application/octet-stream (some CDNs mis-label mp3), 'video' forces "
                        "YouTube oEmbed check (rejects YouTube URLs where the video is "
                        "unavailable/private/embed-disabled), 'any' accepts any 2xx response "
                        "(YouTube URLs are ALWAYS auto-routed through oEmbed regardless). "
                        "Default 'any'."
                    ),
                },
            },
            "required": ["url"],
        },
    },
}


_VALID_AUDIO_PREFIXES = ("audio/", "application/ogg", "application/octet-stream")
_VALID_IMAGE_PREFIXES = ("image/",)

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


async def _check_youtube(yt_id: str, original_url: str) -> str:
    """Hit YouTube's oEmbed endpoint — the only cheap way to know a video is
    actually playable + embeddable. Returns the standard validate_url JSON
    shape with extra video_id/title/author fields on success."""
    oembed_url = (
        f"https://www.youtube.com/oembed?"
        f"url=https://www.youtube.com/watch?v={yt_id}&format=json"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(oembed_url)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "reason": f"oembed request failed: {exc}",
            "video_id": yt_id,
            "url": original_url,
        })

    status = resp.status_code
    if status == 200:
        try:
            data = resp.json()
            return json.dumps({
                "ok": True,
                "status_code": 200,
                "video_id": yt_id,
                "title": data.get("title", ""),
                "author": data.get("author_name", ""),
                "embeddable": True,
                "url": original_url,
            }, ensure_ascii=False)
        except Exception:
            return json.dumps({
                "ok": False,
                "status_code": 200,
                "video_id": yt_id,
                "reason": "oembed returned non-JSON (unexpected)",
                "url": original_url,
            })
    if status == 401:
        reason = "embedding disabled by uploader — pick a different video"
    elif status == 404:
        reason = "video unavailable, private, or removed (hallucinated or dead ID)"
    elif status == 403:
        reason = "oembed forbidden (rare — likely region lock)"
    else:
        reason = f"oembed HTTP {status}"
    return json.dumps({
        "ok": False,
        "status_code": status,
        "video_id": yt_id,
        "reason": reason,
        "url": original_url,
    })


async def execute(arguments: dict) -> str:
    url = (arguments.get("url") or "").strip()
    kind = (arguments.get("expected_kind") or "any").strip().lower()

    if not url:
        return json.dumps({"ok": False, "reason": "empty url"})
    if not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "reason": "url must start with http:// or https://", "url": url})

    # YouTube auto-route: a HEAD on a watch URL returns 200 even for
    # unavailable videos because the page renders. oEmbed is the only cheap
    # check that actually detects unavailable/private/embed-disabled.
    yt_id = _YT_ID_RE.search(url)
    if yt_id:
        return await _check_youtube(yt_id.group(1), url)
    if kind == "video":
        return json.dumps({
            "ok": False,
            "reason": "expected_kind='video' requires a YouTube URL (watch, youtu.be, embed, or shorts)",
            "url": url,
        })

    status_code = None
    content_type = ""
    final_url = url
    reason = ""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                resp = await client.head(url)
            except httpx.HTTPError:
                # Some CDNs reject HEAD; fall back to a 1-byte range GET.
                resp = await client.get(url, headers={"Range": "bytes=0-0"})
            status_code = resp.status_code
            content_type = (resp.headers.get("content-type") or "").lower().split(";", 1)[0].strip()
            final_url = str(resp.url)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "reason": f"request failed: {exc}",
            "url": url,
        })

    ok = 200 <= status_code < 300
    if ok and kind == "image":
        if not any(content_type.startswith(p) for p in _VALID_IMAGE_PREFIXES):
            ok = False
            reason = f"expected image/*, got '{content_type}' (often a 404 HTML error page served with 200)"
    elif ok and kind == "audio":
        if not any(content_type.startswith(p) for p in _VALID_AUDIO_PREFIXES):
            ok = False
            reason = f"expected audio/*, got '{content_type}'"
    elif not ok:
        if status_code == 403:
            reason = "403 forbidden (usually hotlink-blocked — try a different CDN)"
        elif status_code == 404:
            reason = "404 not found — URL is dead or fabricated"
        else:
            reason = f"HTTP {status_code}"

    return json.dumps({
        "ok": ok,
        "status_code": status_code,
        "content_type": content_type,
        "final_url": final_url,
        "url": url,
        "reason": reason,
    }, ensure_ascii=False)
