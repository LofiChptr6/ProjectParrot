"""Custom tool: polygon_prev_close — yesterday's OHLC via Polygon.io."""

import json
from datetime import datetime, timezone

from tools.custom._polygon_client import fetch, h_count, h_price

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_prev_close",
        "description": (
            "Get the previous trading day's OHLC (open/high/low/close), volume, "
            "and VWAP for a US ticker via Polygon. Cheapest, fastest way to "
            "answer 'how did X do yesterday'. Use this before polygon_bars "
            "unless the user asks for a range."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "US stock ticker (e.g. 'AAPL', 'TSLA').",
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
        data = await fetch(
            f"/v2/aggs/ticker/{ticker}/prev",
            {"adjusted": "true"},
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "POLYGON_API_KEY" in msg:
            return "Error: POLYGON_API_KEY not configured."
        if "rate-limited" in msg:
            return "Error: rate limit exceeded, try again in ~60 seconds."
        return f"Error: {msg}"
    except Exception as exc:
        return f"Error: polygon API unreachable ({type(exc).__name__})."

    results = data.get("results") or []
    if not results:
        return f"Error: ticker '{ticker}' not found or no data."

    bar = results[0]
    date_iso = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    return json.dumps({
        "symbol": ticker,
        "date": date_iso,
        "open":   h_price(bar.get("o")),
        "high":   h_price(bar.get("h")),
        "low":    h_price(bar.get("l")),
        "close":  h_price(bar.get("c")),
        "vwap":   h_price(bar.get("vw")),
        "volume": h_count(bar.get("v")),
    }, separators=(",", ":"))
