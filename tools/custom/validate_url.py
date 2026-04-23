"""Custom tool: validate_url — HEAD-check a URL's reachability + content-type.

Nori uses this before handing any asset URL to theme_propose. Catches dead
links, hotlink-blocked CDNs, and page URLs masquerading as image/audio.
"""

from __future__ import annotations

import json

import httpx


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "validate_url",
        "description": (
            "HEAD-check a URL and return {status_code, content_type, final_url, ok, reason}. "
            "Use BEFORE passing any image/audio URL to theme_propose (or to the user). "
            "A URL is OK iff status_code is 200-299 AND content_type starts with the "
            "expected prefix for its kind (image/* for images, audio/* for audio). "
            "404 and 403 (hotlink blocked) both mean the URL is unusable."
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
                    "enum": ["image", "audio", "any"],
                    "description": (
                        "'image' requires content-type image/*, 'audio' requires audio/* or "
                        "application/octet-stream (some CDNs mis-label mp3), 'any' accepts any "
                        "2xx response. Default 'any'."
                    ),
                },
            },
            "required": ["url"],
        },
    },
}


_VALID_AUDIO_PREFIXES = ("audio/", "application/ogg", "application/octet-stream")
_VALID_IMAGE_PREFIXES = ("image/",)


async def execute(arguments: dict) -> str:
    url = (arguments.get("url") or "").strip()
    kind = (arguments.get("expected_kind") or "any").strip().lower()

    if not url:
        return json.dumps({"ok": False, "reason": "empty url"})
    if not url.startswith(("http://", "https://")):
        return json.dumps({"ok": False, "reason": "url must start with http:// or https://", "url": url})

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
