"""Custom tool: get_risk_overview — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_risk_overview",
        "description": (
            "Returns the user's current risk posture: gross/net/leverage/beta, "
            "concentration, sector or factor exposure, and scenario P&L for "
            "small market moves. Call this when the user asks about RISK or "
            "EXPOSURE — phrasings include 'what's my biggest risk', 'how "
            "concentrated am I', 'what's my net beta', 'what's my exposure "
            "to semis', 'what if the market drops 5%', 'factor exposure'. "
            "Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "enum": ["sector", "factor", "single_name", "scenario"],
                    "description": "Optional focus. Omit for a general overview. 'sector' or 'factor' for exposure breakdowns; 'single_name' for concentration; 'scenario' for market-shock P&L.",
                },
            },
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_risk_overview", arguments, "Risk Overview")
