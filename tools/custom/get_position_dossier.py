"""Custom tool: get_position_dossier — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_position_dossier",
        "description": (
            "Returns a deep dossier on a single position in the user's book: "
            "who put it on, the agent's thesis, the plan (entry/stop/target), "
            "thesis health, and price action since entry. Call this whenever "
            "the user asks about ONE specific position they hold — phrasings "
            "include 'tell me about LMT', 'who long'ed X', 'who put this on', "
            "'what's his rationale on X', 'why are we long X', 'what's the "
            "thesis on X', 'is the X thesis still good', 'why did we close X'. "
            "Read-only — never trades."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "The ticker symbol (e.g. 'LMT', 'NVDA').",
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "closed", "any"],
                    "description": "Limit to currently-open positions, recently-closed, or both. Defaults to 'open'.",
                },
            },
            "required": ["symbol"],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_position_dossier", arguments, "Position Dossier")
