"""Custom tool: polygon_snapshot — live US stock snapshot via Polygon.io."""

import json

from tools.custom._polygon_client import fetch, h_count, h_delta, h_pct, h_price

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_snapshot",
        "description": (
            "Get the full real-time market snapshot for a US ticker: today's "
            "OHLC so far, today's change vs prev close, and previous day's "
            "OHLC. Best for 'what's X trading at right now' / intraday checks. "
            "When the market is closed, today's numbers are zero; read "
            "prev_day instead."
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


def _bar_to_handles(bar: dict | None) -> dict | None:
    if not bar:
        return None
    return {
        "open":   h_price(bar.get("o")),
        "high":   h_price(bar.get("h")),
        "low":    h_price(bar.get("l")),
        "close":  h_price(bar.get("c")),
        "vwap":   h_price(bar.get("vw")),
        "volume": h_count(bar.get("v")),
    }


async def execute(arguments: dict) -> str:
    ticker = (arguments.get("ticker") or arguments.get("symbol") or "").strip().upper()
    if not ticker:
        return "Error: 'ticker' is required."

    try:
        data = await fetch(
            f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
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

    snap = data.get("ticker") or {}
    if not snap or not snap.get("ticker"):
        return f"Error: ticker '{ticker}' not found."

    day_bar = snap.get("day") or {}
    market_open = any((day_bar.get("o"), day_bar.get("c"), day_bar.get("v")))

    return json.dumps({
        "symbol":            ticker,
        "market_open_today": market_open,
        "today_change":      h_delta(snap.get("todaysChange") if market_open else None),
        "today_change_pct":  h_pct(snap.get("todaysChangePerc") if market_open else None),
        "today":             _bar_to_handles(day_bar) if market_open else None,
        "prev_day":          _bar_to_handles(snap.get("prevDay")),
    }, separators=(",", ":"))
