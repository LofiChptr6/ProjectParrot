"""Custom tool: theme_apply — persist the current preview as a saved theme.

Steps:
1. Read the current server-side preview mirror (_active_preview).
2. Serialize CSS: `:root { ... }` + raw css + auto-generated background-image CSS
   when background_image_url is set. Write web/static/css/themes/<name>.css
   and mirror to active.css (what index.html links).
3. Write a sidecar <name>.json + active.json carrying the structured assets
   (background image URL, html_decor, html_mods). The frontend bootstrapper
   reads active.json on page load to restore non-CSS parts of the theme.
   NOTE: music is NOT part of themes — use the music_player tool instead.
4. Update variants.json: append/update the entry (with palette + assets) and
   set `active = name`.
5. Broadcast clear_theme_preview (active.css now carries the CSS) and force
   a stylesheet reload so the browser re-fetches active.css.

Must only be called on explicit user approval — the behaviors.yaml rule
forbids silent apply.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent.parent
_THEMES = _ROOT / "web" / "static" / "css" / "themes"
_VARIANTS = _THEMES / "variants.json"
_ACTIVE_CSS = _THEMES / "active.css"
_ACTIVE_JSON = _THEMES / "active.json"


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_apply",
        "description": (
            "PERSIST the current theme preview (palette + background image + decor) "
            "to disk as a saved theme and make it the active theme. MUST be "
            "called only after Ika has explicitly confirmed ('yes save it', 'apply "
            "it', 'keep this one'). Never call this silently. Writes "
            "web/static/css/themes/<name>.css, <name>.json, and updates variants.json."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Theme name. Use slug-friendly chars only: a-z, 0-9, dash. "
                        "Examples: 'warm-cafe', 'dusk-mocha', 'terminal-green'."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short human-readable description (1 sentence).",
                },
            },
            "required": ["name", "description"],
        },
    },
}


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,31}$")
_DEFAULT_OVERLAY = "10,12,16,0.55"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _background_image_css(url: str, overlay_rgba: str) -> str:
    rgba = overlay_rgba.strip() or _DEFAULT_OVERLAY
    return (
        "body {\n"
        f"  background-image: url('{url}');\n"
        "  background-size: cover;\n"
        "  background-position: center;\n"
        "  background-attachment: fixed;\n"
        "}\n"
        "body::before {\n"
        "  content: ''; position: fixed; inset: 0;\n"
        f"  background: rgba({rgba}); pointer-events: none; z-index: -1;\n"
        "}\n"
    )


def _serialize_css(
    variables: dict,
    extra_css: str,
    background_image_url: str,
    overlay_rgba: str,
    name: str,
    description: str,
) -> str:
    header = (
        f"/* Theme: {name}\n"
        f" * {description}\n"
        f" * Generated: {_iso_now()}\n"
        f" * Load order: appended AFTER style.css + panel-manager.css.\n"
        f" */\n"
    )
    parts: list[str] = [header]
    if variables:
        lines = []
        for k, v in variables.items():
            key = k if str(k).startswith("--") else f"--{k}"
            lines.append(f"  {key}: {v};")
        parts.append(":root {\n" + "\n".join(lines) + "\n}\n")
    if background_image_url:
        parts.append(_background_image_css(background_image_url, overlay_rgba))
    if extra_css and extra_css.strip():
        parts.append(extra_css.strip() + "\n")
    return "\n".join(parts)


async def execute(arguments: dict) -> str:
    name = (arguments.get("name") or "").strip().lower()
    description = (arguments.get("description") or "").strip()

    if not _SLUG_RE.match(name):
        return (
            f"Invalid name '{name}'. Use lowercase letters, digits, and dashes only "
            "(e.g. 'warm-cafe')."
        )
    if not description:
        return "Missing 'description' (one short sentence)."

    try:
        from bridge.server import get_active_preview, _broadcast_clients, _ws_clients
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    preview = get_active_preview()
    variables = preview.get("variables") or {}
    extra_css = preview.get("css") or ""
    background_image_url = preview.get("background_image_url") or ""
    overlay_rgba = preview.get("background_overlay_rgba") or ""
    html_decor = preview.get("html_decor") or ""
    html_mods = list(preview.get("html_mods") or [])

    if not any([variables, extra_css, background_image_url, html_decor, html_mods]):
        return (
            "No active preview to apply. Call theme_propose first, iterate until "
            "it looks right, then call theme_apply."
        )

    _THEMES.mkdir(parents=True, exist_ok=True)
    theme_file = _THEMES / f"{name}.css"
    sidecar_file = _THEMES / f"{name}.json"
    css = _serialize_css(variables, extra_css, background_image_url, overlay_rgba, name, description)
    theme_file.write_text(css)
    _ACTIVE_CSS.write_text(css)

    assets = {
        "background_image_url": background_image_url,
        "background_overlay_rgba": overlay_rgba,
        "html_decor": html_decor,
        "html_mods": html_mods,
    }
    sidecar_payload = {
        "name": name,
        "description": description,
        "palette": dict(variables),
        "assets": assets,
        "updated_at": _iso_now(),
    }
    sidecar_file.write_text(json.dumps(sidecar_payload, indent=2, ensure_ascii=False))
    _ACTIVE_JSON.write_text(json.dumps(sidecar_payload, indent=2, ensure_ascii=False))

    variants: dict = {"version": 1, "active": "default", "themes": []}
    if _VARIANTS.exists():
        try:
            variants = json.loads(_VARIANTS.read_text())
        except Exception:
            pass
    themes = variants.get("themes") or []
    existing = next((t for t in themes if t.get("name") == name), None)
    record = {
        "name": name,
        "file": f"{name}.css",
        "sidecar": f"{name}.json",
        "builtin": False,
        "description": description,
        "palette": dict(variables),
        "assets": assets,
        "created_at": _iso_now() if existing is None else existing.get("created_at") or _iso_now(),
        "updated_at": _iso_now(),
    }
    if existing is None:
        themes.append(record)
    else:
        existing.update(record)
    variants["themes"] = themes
    variants["active"] = name
    _VARIANTS.write_text(json.dumps(variants, indent=2, ensure_ascii=False))

    # Preview style element can come down now — active.css carries the look
    # going forward. Audio + decor + html_mods stay as DOM state (they match
    # the new active theme we just wrote). Cache-bust active.css so the
    # browser re-fetches it.
    await _broadcast_clients({"type": "ui_command", "action": "clear_theme_preview"})
    await _broadcast_clients({
        "type": "ui_command",
        "action": "reload_stylesheets",
        "v": int(time.time()),
    })

    return json.dumps({
        "status": "applied",
        "name": name,
        "file": str(theme_file.relative_to(_ROOT)),
        "sidecar": str(sidecar_file.relative_to(_ROOT)),
        "active_css_bytes": len(css),
        "has_background_image": bool(background_image_url),
        "has_decor": bool(html_decor),
        "html_mods_count": len(html_mods),
        "clients": len(_ws_clients),
    }, ensure_ascii=False, indent=2)
