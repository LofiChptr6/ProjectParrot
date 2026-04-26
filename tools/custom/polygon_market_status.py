"""Custom tool: polygon_market_status — is the US market open right now?"""

import json

from tools.custom._polygon_client import fetch

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_market_status",
        "description": (
            "Check whether the US equity market is currently open, plus the "
            "per-exchange status (NYSE/NASDAQ/OTC) and pre/post-market flags. "
            "Use before quoting 'live' prices so you can tell the user "
            "whether numbers are live or prev-close."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


async def execute(arguments: dict) -> str:
    try:
        data = await fetch("/v1/marketstatus/now")
    except RuntimeError as exc:
        msg = str(exc)
        if "POLYGON_API_KEY" in msg:
            return "Error: POLYGON_API_KEY not configured."
        if "rate-limited" in msg:
            return "Error: rate limit exceeded, try again in ~60 seconds."
        return f"Error: {msg}"
    except Exception as exc:
        return f"Error: polygon API unreachable ({type(exc).__name__})."

    exchanges = data.get("exchanges") or {}

    return json.dumps({
        "market":       data.get("market"),     # 'open' / 'closed' / 'extended-hours'
        "after_hours":  bool(data.get("afterHours")),
        "early_hours":  bool(data.get("earlyHours")),
        "exchanges": {
            "nyse":   exchanges.get("nyse"),
            "nasdaq": exchanges.get("nasdaq"),
            "otc":    exchanges.get("otc"),
        },
        "server_time":  data.get("serverTime"),
    }, separators=(",", ":"))
