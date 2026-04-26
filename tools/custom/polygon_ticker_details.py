"""Custom tool: polygon_ticker_details — company profile via Polygon.io."""

import json

from tools.custom._polygon_client import fetch, h_big_usd, h_count

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_ticker_details",
        "description": (
            "Get company / instrument details for a US ticker: name, industry "
            "description, market cap, exchange, shares outstanding, employees, "
            "and homepage. Use for 'what company is X' and fundamental "
            "context questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "US stock ticker (e.g. 'AAPL').",
                },
            },
            "required": ["ticker"],
        },
    },
}


async def execute(arguments: dict) -> str:
    ticker = (arguments.get("ticker") or arguments.get("symbol") or "").strip().upper()
    if not ticker:
        return "Error: 'ticker' is required."

    try:
        data = await fetch(f"/v3/reference/tickers/{ticker}")
    except RuntimeError as exc:
        msg = str(exc)
        if "POLYGON_API_KEY" in msg:
            return "Error: POLYGON_API_KEY not configured."
        if "rate-limited" in msg:
            return "Error: rate limit exceeded, try again in ~60 seconds."
        return f"Error: {msg}"
    except Exception as exc:
        return f"Error: polygon API unreachable ({type(exc).__name__})."

    r = data.get("results") or {}
    if not r:
        return f"Error: ticker '{ticker}' not found."

    return json.dumps({
        "symbol":             ticker,
        "name":               r.get("name"),
        "industry":           r.get("sic_description"),
        "exchange":           r.get("primary_exchange"),
        "active":             r.get("active"),
        "market_cap":         h_big_usd(r.get("market_cap")),
        "shares_outstanding": h_count(r.get("weighted_shares_outstanding")),
        "employees":          h_count(r.get("total_employees")),
        "homepage":           r.get("homepage_url"),
        "list_date":          r.get("list_date"),
        "description":        r.get("description"),
    }, separators=(",", ":"))
