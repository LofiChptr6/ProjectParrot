"""Custom tool: get_manual_overrides — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_manual_overrides",
        "description": (
            "Returns the user's recent manual overrides of agent decisions — "
            "the agent's call, the override, and P&L impact. Call this when "
            "the user asks if they DID SOMETHING DUMB or wants to evaluate "
            "their own discretion — phrasings include 'did I override "
            "anything recently', 'what did I touch manually', 'did I do "
            "anything dumb', 'were my manual trades right'. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "Window to report. '7d', '30d', 'ytd'. Defaults to '30d'.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_manual_overrides", arguments, "Manual Overrides")
