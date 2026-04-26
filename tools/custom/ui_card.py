"""Custom tool: show_card — display an info card / widget in the web UI."""

import json
import time

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "show_card",
        "description": (
            "Display an info card or widget in the web UI. Use for quick data displays "
            "like a stock ticker, weather summary, fun fact, or image. "
            "Cards appear in the lower-right area and stack vertically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "card_type": {
                    "type": "string",
                    "enum": ["info", "stat", "quote", "image"],
                    "description": (
                        "Card style: 'info' (title + text), 'stat' (big number + label), "
                        "'quote' (styled quote), 'image' (image with caption)"
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Card title / heading",
                },
                "content": {
                    "type": "string",
                    "description": "Main text content or quote text",
                },
                "value": {
                    "type": "string",
                    "description": "(stat card) The big number or value to display",
                },
                "fields": {
                    "type": "array",
                    "description": "Optional key-value pairs shown as a list",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                    },
                },
                "image_url": {
                    "type": "string",
                    "description": (
                        "(image card) Reference to the image. Prefer an "
                        "'img:XXXXXXXX' HANDLE from a recent web_search — the "
                        "handle resolves to a real URL automatically. Raw URLs "
                        "are accepted for user-supplied images, but never "
                        "fabricate one."
                    ),
                },
                "duration_sec": {
                    "type": "integer",
                    "description": "Auto-dismiss after N seconds (0 = stays until cleared, default 0)",
                },
            },
            "required": ["card_type", "title"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Show an info card in the web frontend."""
    from bridge.server import _broadcast_clients

    card_id = f"card_{int(time.time() * 1000)}"

    await _broadcast_clients({
        "type": "ui_command",
        "action": "show_card",
        "card": {
            "id": card_id,
            "card_type": arguments.get("card_type", "info"),
            "title": arguments.get("title", ""),
            "content": arguments.get("content", ""),
            "value": arguments.get("value", ""),
            "fields": arguments.get("fields", []),
            "image_url": arguments.get("image_url", ""),
            "duration_sec": arguments.get("duration_sec", 0),
        },
    })

    return f"Card '{arguments.get('title', '')}' displayed in the web UI."
