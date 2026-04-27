"""Custom tool: get_pnl_attribution — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_pnl_attribution",
        "description": (
            "Breaks down what drove the user's P&L over a window — by agent, "
            "by position, with a short narrative on the rotation/theme. Call "
            "this when the user asks WHY they made or lost money — phrasings "
            "include 'why did I make money today', 'what drove the loss', "
            "'which agent crushed it', 'where did the alpha come from this "
            "week', 'who lost me money'. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "Window to attribute. '1d' (today), '5d', '30d', 'mtd', 'ytd'. Defaults to '1d'.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_pnl_attribution", arguments, "P&L Attribution")
