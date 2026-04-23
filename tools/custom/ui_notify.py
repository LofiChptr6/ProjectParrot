"""Custom tool: show_notification — flash a toast notification in the web UI."""

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "show_notification",
        "description": (
            "Show a brief toast notification in the web UI. "
            "Auto-dismisses after 5 seconds. Use for status updates or confirmations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Notification text",
                },
                "level": {
                    "type": "string",
                    "enum": ["info", "success", "warning"],
                    "description": "Notification style (default 'info')",
                },
            },
            "required": ["message"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Show a toast notification in the web frontend."""
    from bridge.server import _broadcast_to_unity

    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "show_notification",
        "message": arguments.get("message", ""),
        "level": arguments.get("level", "info"),
    })

    return "Notification displayed."
