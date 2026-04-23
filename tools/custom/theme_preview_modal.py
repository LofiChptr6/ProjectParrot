"""Custom tool: theme_preview_modal — show a visual preview + approval modal.

Opens a modal in the web UI showing the proposed palette (color swatches),
a mood line, optional Hana critique + screenshot, and two buttons: "Save"
and "Cancel". The buttons send synthetic user_input messages back to the
bridge so Mocha's normal conversation flow handles the actual apply/revert.

Typical flow:
  theme_propose(...)        # preview goes live on the page
  theme_screenshot()        # capture base64
  ask_hana(task="critique") # grade it
  theme_preview_modal(...)  # show Ika a nice decision UI
  # — wait for user click, Mocha sees the follow-up user_input —
"""

from __future__ import annotations

import json


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "theme_preview_modal",
        "description": (
            "Open a visual preview modal in the web UI showing the proposed theme "
            "with palette swatches, mood line, optional screenshot, and Save/Cancel "
            "buttons. Call this ONCE when you're ready to ask Ika for approval — "
            "do NOT call it in the middle of iteration. Clicking Save in the modal "
            "sends 'yes apply as <name>' back as a user message; clicking Cancel "
            "sends 'nah, revert'. Do NOT call theme_apply yourself — wait for "
            "Ika's click to come in as a user message, THEN handle it normally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Slug for the theme (a-z, 0-9, dashes). E.g. 'warm-cafe'.",
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence human description of the look.",
                },
                "palette": {
                    "type": "object",
                    "description": (
                        "The CSS variables you proposed. Keys without '--'. Must match what "
                        "you passed to theme_propose."
                    ),
                },
                "mood": {
                    "type": "string",
                    "description": "Optional: Hana's mood line (2-3 sentences) if available.",
                },
                "critique_score": {
                    "type": "integer",
                    "description": "Optional: Hana's 0-10 critique score if available.",
                },
                "screenshot_b64": {
                    "type": "string",
                    "description": (
                        "Optional: base64 PNG from theme_screenshot. If present, shown as a "
                        "hero thumbnail in the modal so Ika sees the real page."
                    ),
                },
            },
            "required": ["name", "description", "palette"],
        },
    },
}


import re
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,31}$")


async def execute(arguments: dict) -> str:
    name = (arguments.get("name") or "").strip().lower()
    if not _SLUG_RE.match(name):
        return (
            f"Invalid name '{name}'. Use lowercase letters, digits, and dashes "
            "(e.g. 'warm-cafe')."
        )
    description = (arguments.get("description") or "").strip()
    palette = arguments.get("palette") or {}
    if not isinstance(palette, dict) or not palette:
        return "Missing 'palette' — pass the same variables dict you used in theme_propose."

    try:
        from bridge.server import _broadcast_to_unity, unity_clients, get_active_preview
    except Exception as exc:
        return f"bridge unavailable: {exc}"

    # Pull the non-palette parts of the full-package proposal straight from
    # the server-side mirror — no drift between what's live on the page and
    # what the modal advertises.
    preview = get_active_preview()

    msg = {
        "type": "ui_command",
        "action": "show_theme_preview_modal",
        "name": name,
        "description": description,
        "palette": palette,
        "background_image_url": preview.get("background_image_url") or "",
        "has_decor": bool(preview.get("html_decor")),
        "html_mods_count": len(preview.get("html_mods") or []),
    }
    mood = (arguments.get("mood") or "").strip()
    if mood:
        msg["mood"] = mood
    if "critique_score" in arguments and arguments["critique_score"] is not None:
        try:
            msg["critique_score"] = int(arguments["critique_score"])
        except (TypeError, ValueError):
            pass
    screenshot = arguments.get("screenshot_b64") or ""
    if screenshot and isinstance(screenshot, str):
        # Frontend renders via `data:image/png;base64,...`. Cap at ~2MB to avoid
        # absurdly heavy WS frames.
        if len(screenshot) < 2_800_000:
            msg["screenshot_b64"] = screenshot

    await _broadcast_to_unity(msg)
    return json.dumps({
        "status": "modal_opened",
        "clients": len(unity_clients),
        "name": name,
        "note": "Modal is visible. WAIT for Ika to click — do not call theme_apply yourself.",
    }, ensure_ascii=False)
