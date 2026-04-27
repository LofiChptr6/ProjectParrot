"""Custom tool: get_agent_disagreement — proxy to opus_trading MCP."""
from tools.custom._opus_proxy import call_opus

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_agent_disagreement",
        "description": (
            "Returns pairs of agents that currently hold conflicting views on "
            "the same symbol or theme — one long, one short, or active "
            "thesis disagreements. Call this when the user asks about "
            "INTERNAL CONFLICT in the book — phrasings include 'are any of "
            "my agents fighting each other', 'what's controversial in the "
            "book', 'where do strategies disagree', 'agents in conflict'. "
            "Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    return await call_opus("get_agent_disagreement", arguments, "Agent Disagreement")
