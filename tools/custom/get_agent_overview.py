"""Custom tool: get_agent_overview — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_agent_overview",
        "description": (
            "Returns an overview of one of the user's trading agents: P&L "
            "across windows, hit rate, current allocation, all open positions, "
            "and the agent's current playbook / sector or macro view. Call "
            "this when the user asks about a SPECIFIC agent — phrasings "
            "include 'how is the macro agent doing', 'what's the semis "
            "specialist running', 'show me agent X's book', 'who's my best/"
            "worst agent', 'has agent X been right lately'. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Agent identifier (e.g. 'atlas', 'maya', 'iron', 'volt'). Use the user's phrasing if unsure.",
                },
            },
            "required": ["agent_id"],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_agent_overview", arguments, "Agent Overview")
