"""Custom tool: polygon_ticker_news — recent news articles for a ticker."""

import json

from tools.custom._polygon_client import fetch

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "polygon_ticker_news",
        "description": (
            "Get recent news articles for a specific US ticker via Polygon. "
            "Returns titles, publishers, URLs, and short descriptions. Use "
            "this (not the generic get_news) when the question is about a "
            "specific stock."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "US stock ticker (e.g. 'NVDA').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max articles to return (1-20). Default 10.",
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
        limit = int(arguments.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(20, limit))

    try:
        data = await fetch(
            "/v2/reference/news",
            {"ticker": ticker, "limit": limit, "order": "desc", "sort": "published_utc"},
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

    items = data.get("results") or []
    if not items:
        return f"Error: no news found for '{ticker}'."

    articles = [
        {
            "title":       it.get("title"),
            "publisher":   (it.get("publisher") or {}).get("name"),
            "published":   (it.get("published_utc") or "")[:10],  # YYYY-MM-DD
            "url":         it.get("article_url"),
            "description": (it.get("description") or "")[:240],
        }
        for it in items
    ]

    return json.dumps({
        "symbol":   ticker,
        "count":    len(articles),
        "articles": articles,
    }, separators=(",", ":"))
