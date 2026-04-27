"""Custom tool: get_upcoming_catalysts — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_upcoming_catalysts",
        "description": (
            "Returns upcoming events that affect the user's positions — "
            "earnings, FOMC, investor days, expirations, etc. — with the "
            "responsible agent's prep notes. Call this when the user asks "
            "about WHAT'S COMING — phrasings include 'anything coming up I "
            "should care about', 'what's on the docket', 'what's the next "
            "catalyst', 'upcoming events'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "Forward window. '1d', '7d', '30d'. Defaults to '7d'.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_upcoming_catalysts", arguments, "Upcoming Catalysts")
