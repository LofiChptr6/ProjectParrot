"""Custom tool: theme_load — load a saved theme into the preview.

Reads the saved CSS from web/static/css/themes/<name>.css plus the sidecar
<name>.json asset metadata (bg image / decor / html_mods), pushes it as a
live preview, and marks the theme as `active` in variants.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent.parent
_THEMES = _ROOT / "web" / "static" / "css" / "themes"
_VARIANTS = _THEMES / "variants.json"


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_load",
        "description": (
            "Load a previously-saved theme by name into the live preview. "
            "Call list of saved themes via theme_get first if you don't know the name. "
            "This previews the theme (colors + bg image + decor); it does NOT "
            "persist as active until Ika confirms and you call theme_apply. "
            "Music is separate — use music_player."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The theme name from variants.json (e.g. 'default', 'warm-cafe').",
                },
            },
            "required": ["name"],
        },
    },
}


_ROOT_BLOCK_RE = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
_VAR_DECL_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")


def _extract_vars(css: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in _ROOT_BLOCK_RE.finditer(css or ""):
        for m in _VAR_DECL_RE.finditer(block.group(1)):
            out[m.group(1)] = m.group(2).strip()
    return out


async def execute(arguments: dict) -> str:
    name = (arguments.get("name") or "").strip()
    if not name:
        return "Missing 'name'."

    variants: dict = {}
    if _VARIANTS.exists():
        try:
            variants = json.loads(_VARIANTS.read_text())
        except Exception as exc:
            return f"Could not read variants.json: {exc}"
    entries = variants.get("themes") or []
    entry = next((e for e in entries if e.get("name") == name), None)
    if entry is None:
        return f"No theme named '{name}'. Try theme_get for the list."

    # Default theme has no CSS file — it's the base style.css state. Loading
    # default means "clear any overrides".
    if entry.get("builtin") and not entry.get("file"):
        try:
            from bridge.server import _broadcast_to_unity, _reset_active_preview
        except Exception as exc:
            return f"bridge unavailable: {exc}"
        _reset_active_preview()
        await _broadcast_to_unity({"type": "ui_command", "action": "clear_theme_preview"})
        return json.dumps({"status": "loaded", "name": name, "note": "default — preview cleared"}, ensure_ascii=False)

    file_name = entry.get("file") or f"{name}.css"
    theme_path = _THEMES / file_name
    if not theme_path.exists():
        return f"Theme file missing on disk: {theme_path}"

    try:
        css = theme_path.read_text()
    except Exception as exc:
        return f"Could not read theme file: {exc}"

    variables = _extract_vars(css)

    # Sidecar JSON carries structured assets — background image URL, music,
    # decor HTML, html_mods. Missing sidecar is fine (older themes or CSS-only).
    assets = entry.get("assets") or {}
    sidecar_path = _THEMES / f"{name}.json"
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text())
            assets = sidecar.get("assets", assets) if isinstance(sidecar, dict) else assets
        except Exception:
            pass
    background_image_url = assets.get("background_image_url") or ""
    background_overlay_rgba = assets.get("background_overlay_rgba") or ""
    html_decor = assets.get("html_decor") or ""
    html_mods = assets.get("html_mods") or []

    try:
        from bridge.server import _broadcast_to_unity, _active_preview, unity_clients
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
    return json.dumps({
        "status": "loaded",
        "name": name,
        "clients": len(unity_clients),
        "variables": variables,
        "css_bytes": len(css),
        "background_image_url": background_image_url or None,
        "has_decor": bool(html_decor),
        "html_mods_count": len(html_mods),
    }, ensure_ascii=False, indent=2)
