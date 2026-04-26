"""Custom tool: polygon_bars — OHLC bars over a custom range via Polygon.io."""

import json
import re
from datetime import datetime, timezone

from tools.custom._polygon_client import fetch, h_count, h_price

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED_SPANS = {"minute", "hour", "day", "week", "month", "quarter", "year"}

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_bars",
        "description": (
            "Get OHLC price bars for a US ticker over a custom date range and "
            "interval (via Polygon). Use for 'how did X do this week/month', "
            "trend questions, and anything that needs a chart. Returns one bar "
            "per interval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "US stock ticker (e.g. 'AAPL').",
                },
                "multiplier": {
                    "type": "integer",
                    "description": "Size of the timespan (e.g. 1 for daily, 5 for 5-minute bars). Default 1.",
                },
                "timespan": {
                    "type": "string",
                    "enum": ["minute", "hour", "day", "week", "month", "quarter", "year"],
                    "description": "Bar interval unit. Default 'day'.",
                },
                "from": {
                    "type": "string",
                    "description": "Start date, YYYY-MM-DD.",
                },
                "to": {
                    "type": "string",
                    "description": "End date, YYYY-MM-DD.",
                },
            },
            "required": ["ticker", "from", "to"],
        },
    },
}


async def execute(arguments: dict) -> str:
    ticker = (arguments.get("ticker") or arguments.get("symbol") or "").strip().upper()
    if not ticker:
        return "Error: 'ticker' is required."

    date_from = (arguments.get("from") or "").strip()
    date_to = (arguments.get("to") or "").strip()
    if not _DATE_RE.match(date_from) or not _DATE_RE.match(date_to):
        return "Error: 'from' and 'to' dates required (YYYY-MM-DD)."

    multiplier = arguments.get("multiplier") or 1
    try:
        multiplier = int(multiplier)
    except (TypeError, ValueError):
        return "Error: 'multiplier' must be an integer."

    timespan = (arguments.get("timespan") or "day").strip().lower()
    if timespan not in _ALLOWED_SPANS:
        return f"Error: 'timespan' must be one of {sorted(_ALLOWED_SPANS)}."

    try:
        data = await fetch(
            f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{date_from}/{date_to}",
            {"adjusted": "true", "sort": "asc", "limit": 5000},
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
        return f"Error: no bars for '{ticker}' in {date_from}..{date_to}."

    bars = [
        {
            "date":   datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open":   h_price(b.get("o")),
            "high":   h_price(b.get("h")),
            "low":    h_price(b.get("l")),
            "close":  h_price(b.get("c")),
            "vwap":   h_price(b.get("vw")),
            "volume": h_count(b.get("v")),
        }
        for b in results
    ]

    return json.dumps({
        "symbol":    ticker,
        "timespan":  f"{multiplier}{timespan[0]}",
        "bar_count": len(bars),
        "bars":      bars,
    }, separators=(",", ":"))
