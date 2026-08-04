"""Custom tool: get_stock_data — fetch stock market data via yfinance.

Numeric values (price, change, change_pct, OHLC) are wrapped with the
``num:xxxxxxxx`` handle system before returning, so the LLM never sees the
raw number and cannot hallucinate it. The bridge resolves the handle back to
the formatted display value just before TTS.
"""

import json

from tools.custom._polygon_client import h_delta, h_pct, h_price

# Backwards-compatible local aliases in case anything imports these names.
_h_price = h_price
_h_pct = h_pct
_h_delta = h_delta

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_stock_data",
        "description": (
            "Fetch delayed stock summary (price / change / recent OHLC history) "
            "via yfinance — free, no rate limit, good for fan-out across many "
            "tickers or longer history (1mo / 3mo). For precision on a single "
            "ticker (real-time snapshot, ticker-scoped news, company "
            "fundamentals, market status) prefer the polygon_* tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "string",
                    "description": (
                        "Comma-separated ticker symbols (e.g. 'AAPL,TSLA,NVDA'). "
                        "Max 10 symbols."
                    ),
                },
                "period": {
                    "type": "string",
                    "enum": ["1d", "5d", "1mo", "3mo"],
                    "description": "Historical data period (default '5d')",
                },
            },
            "required": ["symbols"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Fetch stock data and return compact JSON with numeric values handle-wrapped."""
    import asyncio

    raw = arguments.get("symbols", "") or arguments.get("ticker", "") or arguments.get("symbol", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:10]
    period = arguments.get("period", "") or arguments.get("range", "") or "5d"

    if not symbols:
        return "Error: no symbols provided."

    # yfinance is fully synchronous (network + pandas) — run it off the event
    # loop so a slow Yahoo response can't freeze every concurrent turn.
    return await asyncio.to_thread(_fetch_sync, symbols, period)


def _fetch_sync(symbols: list, period: str) -> str:
    import yfinance as yf

    results = []
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            info = ticker.fast_info
            hist = ticker.history(period=period)

            price = round(info.last_price, 2) if info.last_price else None
            prev = round(info.previous_close, 2) if info.previous_close else None
            change = round(price - prev, 2) if price and prev else None
            change_pct = round((change / prev) * 100, 2) if change and prev else None

            history_pts = []
            if not hist.empty:
                for date, row in hist.tail(10).iterrows():
                    history_pts.append({
                        "date": date.strftime("%Y-%m-%d"),
                        "open": _h_price(round(row["Open"], 2)),
                        "high": _h_price(round(row["High"], 2)),
                        "low": _h_price(round(row["Low"], 2)),
                        "close": _h_price(round(row["Close"], 2)),
                    })

            results.append({
                "symbol": sym,
                "price": _h_price(price),
                "prev_close": _h_price(prev),
                "change": _h_delta(change),
                "change_pct": _h_pct(change_pct),
                "history": history_pts,
            })
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})

    return json.dumps(results, separators=(",", ":"))
