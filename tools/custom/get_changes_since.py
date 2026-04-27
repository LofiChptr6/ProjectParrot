"""Custom tool: get_changes_since — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_changes_since",
        "description": (
            "Returns a digest of what changed in the user's book since a "
            "given timestamp — new positions, closed positions, large moves, "
            "broken theses, news the agents flagged. Call this when the user "
            "wants to CATCH UP — phrasings include 'anything change "
            "overnight', 'what's new this morning', 'catch me up', 'what "
            "did I miss', 'anything happen since I checked'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "timestamp_iso": {
                    "type": "string",
                    "description": "ISO timestamp to compare against (e.g. '2026-04-27T08:00:00'). Omit to use the user's last interaction time.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_changes_since", arguments, "Changes Since Last Check")
