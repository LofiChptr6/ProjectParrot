"""Custom tool: get_trade_activity — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_trade_activity",
        "description": (
            "Returns the user's trade tickets over a window: timestamp, "
            "symbol, side, qty, price, agent, action (open/add/trim/close), "
            "and a brief reason. Call this when the user asks WHAT was traded "
            "— phrasings include 'what did we trade today', 'show me today's "
            "tickets', 'any new positions', 'did anything close', 'recent "
            "trades'. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "Window to report. '1d' (today), '5d', '30d'. Defaults to '1d'.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_trade_activity", arguments, "Trade Activity")
