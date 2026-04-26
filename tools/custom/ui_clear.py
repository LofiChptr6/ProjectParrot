"""Custom tool: clear_ui — clear Mocha's UI elements from the web interface."""

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "clear_ui",
        "description": (
            "Clear UI elements that Mocha created (presentations, cards, notifications). "
            "Use 'all' to clear everything, or target a specific type."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["all", "presentation", "cards", "notifications"],
                    "description": "What to clear (default 'all')",
                },
            },
        },
    },
}


async def execute(arguments: dict) -> str:
    """Clear UI elements in the web frontend."""
    from bridge.server import _broadcast_clients, _clear_open_modal

    target = arguments.get("target", "all")

    await _broadcast_clients({
        "type": "ui_command",
        "action": "clear_ui",
        "target": target,
    })
    if target in ("all", "presentation"):
        _clear_open_modal("presentation")

    labels = {
        "all": "Cleared all UI elements.",
        "presentation": "Presentation closed.",
        "cards": "All cards dismissed.",
        "notifications": "Notifications cleared.",
    }
    return labels.get(target, "Cleared.")
