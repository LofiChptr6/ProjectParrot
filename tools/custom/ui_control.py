"""Custom tool: control_slides — navigate or close the slide panel."""

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "control_slides",
        "description": (
            "Navigate or close the slide panel. "
            "Actions: 'next', 'prev', 'goto' (by index), 'clear' (close)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["next", "prev", "goto", "clear"],
                    "description": "Navigation action",
                },
                "slide_index": {
                    "type": "integer",
                    "description": "Slide index for 'goto' action (0-based)",
                },
            },
            "required": ["action"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Send slide navigation command to frontend."""
    from bridge.server import _broadcast_to_unity

    action = arguments.get("action", "next")
    msg = {"type": "ui_command", "action": f"presentation_{action}"}

    if action == "goto":
        msg["slide_index"] = arguments.get("slide_index", 0)

    await _broadcast_to_unity(msg)

    labels = {
        "next": "Advanced to next slide.",
        "prev": "Went back one slide.",
        "goto": f"Jumped to slide {arguments.get('slide_index', 0) + 1}.",
        "clear": "Slides closed.",
    }
    return labels.get(action, "Done.")
