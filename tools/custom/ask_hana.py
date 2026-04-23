"""Custom tool: ask_hana — delegate visual design work to Hana.

Mocha calls this in two modes:
  - task="inspire": give Hana a reference image (URL or base64) and get a
    palette + mood back. Call this BEFORE proposing a theme when Ika provided
    a reference.
  - task="critique": send Hana the current theme preview screenshot for review.
    Best flow: theme_propose(...) → theme_screenshot() → ask_hana(task="critique",
    screenshot_b64=<b64>, notes="what I tried").

Hana returns strict JSON; we serialize it to a string so the tool loop can
thread it back to Mocha.
"""

from __future__ import annotations

import json


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "ask_hana",
        "description": (
            "Ask Hana — your design critic with vision — for design help. Tasks: "
            "task='design_scene' (plan a FULL theme package for a vibe — palette + "
            "image/music search queries + decor brief; hand this to Nori to fetch and "
            "assemble), task='inspire' (palette-only suggestion; legacy, prefer design_scene), "
            "task='critique' (review a UI screenshot; requires screenshot_b64). Returns "
            "JSON. Hana has vision but cannot reach the live web, so she returns search "
            "queries instead of URLs — Nori does the searching + validating."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "enum": ["design_scene", "inspire", "critique"],
                    "description": (
                        "design_scene = full theme package (palette + bg/music search queries + decor brief). "
                        "inspire = palette-only (legacy). "
                        "critique = review a UI screenshot."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "REQUIRED for inspire: the vibe ('warm cafe', 'cyberpunk', "
                        "'minimalist brutalism'). For critique: what you tried "
                        "('softened accent, added glass on panels')."
                    ),
                },
                "images": {
                    "type": "array",
                    "description": (
                        "OPTIONAL for inspire. Reference images if Ika gave a URL. "
                        "Each item: {type:'url'|'base64', data:'<url or base64>'}. "
                        "If you don't have a URL, DON'T pass an empty array — just "
                        "omit this field and Hana works from the vibe text."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["url", "base64"]},
                            "data": {"type": "string"},
                        },
                        "required": ["type", "data"],
                    },
                },
                "screenshot_b64": {
                    "type": "string",
                    "description": (
                        "REQUIRED for task='critique'. Base64-encoded PNG from "
                        "theme_screenshot."
                    ),
                },
                "variables_used": {
                    "type": "object",
                    "description": (
                        "Optional (critique only). The CSS variables your preview uses. "
                        "Helps Hana name fixes in the same variable namespace."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["standard", "deep"],
                    "description": "standard (Haiku, default) or deep (Sonnet, big redesigns).",
                },
            },
            "required": ["task"],
        },
    },
}


async def execute(arguments: dict) -> str:
    task = (arguments.get("task") or "").strip().lower()
    mode = (arguments.get("mode") or "standard").strip().lower()
    notes = arguments.get("notes") or ""

    if task not in ("design_scene", "inspire", "critique"):
        return f"Unknown task '{task}'. Use 'design_scene', 'inspire', or 'critique'."

    try:
        if task == "design_scene":
            from hana.agent import design_scene
            if not notes.strip():
                return "Missing 'notes' for design_scene. Provide the vibe — e.g. notes='90s anime cafe'."
            result = await design_scene(notes=notes, mode=mode)
        elif task == "inspire":
            from hana.agent import inspire
            images = arguments.get("images")
            # Treat empty list / wrong type as "no images". Vibe-only is fine.
            if not isinstance(images, list) or not images:
                images = None
            if not notes.strip() and not images:
                return (
                    "Missing both 'notes' and 'images' for inspire. Provide at least a "
                    "vibe description like notes='warm cafe'."
                )
            result = await inspire(images=images, notes=notes, mode=mode)
        else:
            from hana.agent import critique
            screenshot = arguments.get("screenshot_b64") or ""
            if not screenshot:
                return (
                    "Missing 'screenshot_b64' for critique. Call theme_screenshot first "
                    "to capture the current preview, then pass the returned base64 here."
                )
            result = await critique(
                screenshot_b64=screenshot,
                context=notes,
                variables_used=arguments.get("variables_used") or None,
                mode=mode,
            )
    except Exception as exc:
        return f"Hana error: {exc}"

    # Always return a string; Mocha's tool loop expects that.
    return json.dumps(result, ensure_ascii=False, indent=2)
