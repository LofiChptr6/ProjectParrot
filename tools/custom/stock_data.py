"""Custom tool: get_stock_data — fetch stock market data via yfinance."""

import json

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_stock_data",
        "description": (
            "Fetch stock market data for one or more ticker symbols. "
            "Returns current price, daily change, and recent historical data points."
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
    """Fetch stock data and return compact JSON."""
    import yfinance as yf

    # Accept common LLM parameter name variations
    raw = arguments.get("symbols", "") or arguments.get("ticker", "") or arguments.get("symbol", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()][:10]
    period = arguments.get("period", "") or arguments.get("range", "") or "5d"

    if not symbols:
        return "Error: no symbols provided."

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

            # OHLCV history (last 10 points, compact)
            history_pts = []
            if not hist.empty:
                for date, row in hist.tail(10).iterrows():
                    history_pts.append({
                        "date": date.strftime("%Y-%m-%d"),
                        "open": round(row["Open"], 2),
                        "high": round(row["High"], 2),
                        "low": round(row["Low"], 2),
                        "close": round(row["Close"], 2),
                    })

            results.append({
                "symbol": sym,
                "price": price,
                "prev_close": prev,
                "change": change,
                "change_pct": change_pct,
                "history": history_pts,
            })
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})

    return json.dumps(results, separators=(",", ":"))
