"""Custom tool: theme_get — read the current theme state.

Returns the base palette variables from style.css, the currently-active saved
theme's overrides (if any), and the list of saved themes Mocha can load.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent.parent
_STYLE_CSS = _ROOT / "web" / "static" / "css" / "style.css"
_ACTIVE_CSS = _ROOT / "web" / "static" / "css" / "themes" / "active.css"
_VARIANTS = _ROOT / "web" / "static" / "css" / "themes" / "variants.json"


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_get",
        "description": (
            "Read the current theme state. Returns the base palette variables "
            "(--bg, --accent, --text, etc.), the active-theme overrides currently "
            "loaded from disk, and the list of saved themes available to load. "
            "Call this before proposing changes so you know what you're starting from."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


_ROOT_BLOCK_RE = re.compile(r":root\s*\{([^}]*)\}", re.DOTALL)
_VAR_DECL_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+);")


def _extract_root_vars(css: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in _ROOT_BLOCK_RE.finditer(css or ""):
        for m in _VAR_DECL_RE.finditer(block.group(1)):
            out[m.group(1)] = m.group(2).strip()
    return out


async def execute(arguments: dict) -> str:
    try:
        base_css = _STYLE_CSS.read_text() if _STYLE_CSS.exists() else ""
    except Exception as exc:
        return f"Could not read style.css: {exc}"

    base_vars = _extract_root_vars(base_css)

    active_css = ""
    if _ACTIVE_CSS.exists():
        try:
            active_css = _ACTIVE_CSS.read_text()
        except Exception:
            active_css = ""
    active_vars = _extract_root_vars(active_css)

    variants: dict = {}
    if _VARIANTS.exists():
        try:
            variants = json.loads(_VARIANTS.read_text())
        except Exception:
            variants = {}

    # Preview state (may be in-memory on the running bridge).
    preview_state: dict = {}
    try:
        from bridge.server import get_active_preview
        preview_state = get_active_preview()
    except Exception:
        preview_state = {}

    resolved = {**base_vars, **active_vars, **(preview_state.get("variables") or {})}

    payload = {
        "base_variables": base_vars,
        "active_theme_overrides": active_vars,
        "preview": preview_state,
        "resolved_variables": resolved,
        "active": variants.get("active", "default"),
        "saved_themes": [
            {"name": t.get("name"),
             "description": t.get("description", ""),
             "builtin": bool(t.get("builtin", False)),
             "palette": t.get("palette") or {}}
            for t in variants.get("themes", [])
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
