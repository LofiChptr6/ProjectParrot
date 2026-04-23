"""Custom tool: theme_propose — push a live theme preview via ui_command.

Pure staging layer — the preview lives as a <style id="theme-preview"> element
appended to <head> on every connected web client, plus an optional HTML decor
slot (<div id="theme-decor">). Does NOT touch disk. Mocha calls this freely,
iterates, and only commits via theme_apply when the user says yes.

Ambient music is a SEPARATE concern handled by the music_player tool — themes
are purely visual (palette + bg image + decor HTML + element mods).
"""

from __future__ import annotations

import json


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_propose",
        "description": (
            "Push a FULL live theme preview to all connected web clients. A theme is a "
            "coordinated VISUAL vision — palette + optional background image + optional "
            "decorative HTML + optional element mods — propose ALL parts TOGETHER in ONE "
            "call, not piecemeal. For ambient music, use the SEPARATE music_player tool. "
            "Undoable; does NOT touch disk. Call theme_revert to remove. Always call "
            "ask_hana(task='critique') with a screenshot before asking Ika for approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "variables": {
                    "type": "object",
                    "description": (
                        "CSS variable overrides keyed by name without the leading '--'. "
                        "Common keys: bg, surface, card, border, accent, accent-dim, "
                        "text, muted, green, red, yellow, radius. Values are any valid "
                        "CSS color / length."
                    ),
                },
                "css": {
                    "type": "string",
                    "description": (
                        "Optional raw CSS appended after the :root overrides. Use "
                        "sparingly — prefer variables when possible. Auto-generated "
                        "background-image CSS is added separately if background_image_url "
                        "is supplied, so do NOT hand-write body { background-image } here."
                    ),
                },
                "background_image_url": {
                    "type": "string",
                    "description": (
                        "URL to a full-page background image (landscape, high-res preferred). "
                        "When set, the frontend auto-generates 'body { background-image: url(...); "
                        "background-size: cover; }' plus a 'body::before' dim overlay — you do not "
                        "write that CSS. Use web_search to find one (pixabay.com / unsplash.com) "
                        "when the vibe implies an environment (cafe, forest, NYC, space station)."
                    ),
                },
                "background_overlay_rgba": {
                    "type": "string",
                    "description": (
                        "RGBA channels (comma-separated, no parens) for the overlay that keeps "
                        "UI readable over the background image. Example: '10,12,16,0.55'. "
                        "Defaults to a 0.55-opacity tint of the 'bg' variable when omitted."
                    ),
                },
                "html_decor": {
                    "type": "string",
                    "description": (
                        "Optional raw HTML injected into <div id='theme-decor'>. Use for "
                        "purely decorative elements — floating petals, corner ornaments, "
                        "ambient SVG, particle containers, fake terminal CRT scanlines. "
                        "DO NOT include <script> tags (they're stripped). DO NOT try to "
                        "modify the chat/panel/monitor DOM — those belong to the app. "
                        "Style the decor via your css field or a <style> block inside the "
                        "decor HTML itself. The decor container is pointer-events:none by "
                        "default so it never blocks user clicks."
                    ),
                },
                "html_mods": {
                    "type": "array",
                    "description": (
                        "Optional list of bounded class/attribute tweaks to existing "
                        "elements. Each entry: {selector, add_class?, remove_class?, "
                        "set_attr? (obj of name→value)}. Example: "
                        "[{'selector':'body','add_class':'retro-scanlines'}, "
                        "{'selector':'#chat-wrap','set_attr':{'data-mood':'dusk'}}]. "
                        "Use this to toggle theme-specific modifier classes whose "
                        "styles you wrote into the css field."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "add_class": {"type": "string"},
                            "remove_class": {"type": "string"},
                            "set_attr": {"type": "object"},
                        },
                        "required": ["selector"],
                    },
                },
                "notes": {
                    "type": "string",
                    "description": "One-sentence record of the vibe / what you changed and why.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    variables = arguments.get("variables") or {}
    css = arguments.get("css") or ""
    background_image_url = (arguments.get("background_image_url") or "").strip()
    background_overlay_rgba = (arguments.get("background_overlay_rgba") or "").strip()
    html_decor = arguments.get("html_decor") or ""
    html_mods = arguments.get("html_mods") or []
    notes = (arguments.get("notes") or "").strip()

    if not isinstance(variables, dict):
        return "variables must be an object ({name: value}); got " + type(variables).__name__
    # Accept underscore-form keys (accent_dim) as aliases for dash-form (accent-dim),
    # which is what CSS expects. Hana's soul historically used underscores; this keeps
    # everything wired correctly regardless of which form the caller used.
    _underscore_aliases = {"accent_dim", "bg_color", "text_color"}
    variables = {
        (k.replace("_", "-") if k in _underscore_aliases else k): v
        for k, v in variables.items()
    }
    if not isinstance(css, str):
        return "css must be a string"
    if not isinstance(html_decor, str):
        return "html_decor must be a string"
    if not isinstance(html_mods, list):
        return "html_mods must be a list of {selector, add_class?, remove_class?, set_attr?} objects"
    for i, m in enumerate(html_mods):
        if not isinstance(m, dict) or not m.get("selector"):
            return f"html_mods[{i}] needs a 'selector' field"

    if not any([variables, css, background_image_url, html_decor, html_mods]):
        return "Nothing to preview — pass at least one of variables, css, background_image_url, html_decor, or html_mods."

    try:
        from bridge.server import _broadcast_to_unity, unity_clients, _active_preview
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    _active_preview["variables"] = dict(variables)
    _active_preview["css"] = css
    _active_preview["background_image_url"] = background_image_url
    _active_preview["background_overlay_rgba"] = background_overlay_rgba
    _active_preview["html_decor"] = html_decor
    _active_preview["html_mods"] = list(html_mods)

    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "apply_theme_preview",
        "variables": variables,
        "css": css,
        "background_image_url": background_image_url,
        "background_overlay_rgba": background_overlay_rgba,
        "html_decor": html_decor,
        "html_mods": html_mods,
    })

    clients = len(unity_clients)
    summary = {
        "status": "preview_sent",
        "clients": clients,
        "variables": variables,
        "css_bytes": len(css),
        "background_image_url": background_image_url or None,
        "html_decor_bytes": len(html_decor),
        "html_mods_count": len(html_mods),
        "notes": notes,
    }
    if clients == 0:
        summary["warning"] = "No web client connected; preview will apply when one reconnects."
    return json.dumps(summary, ensure_ascii=False, indent=2)
