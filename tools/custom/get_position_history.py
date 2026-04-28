"""Custom tool: get_position_history — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_position_history",
        "description": (
            "Returns historical track record for a symbol, an agent, or a "
            "setup type — number of trades, win rate, average holding, P&L "
            "per trade. Call this when the user asks about PAST PERFORMANCE "
            "of something — phrasings include 'has the macro agent traded "
            "TLT before', 'what's our track record on LMT', 'how often does "
            "this setup work', 'has this trade worked before'. At least one "
            "of symbol / agent_id / setup_type must be provided. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol (e.g. 'TLT', 'NVDA').",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "How far back to search for past trades on this symbol. Defaults to 30.",
                },
            },
            "required": ["symbol"],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_position_history", arguments, "Position History")
