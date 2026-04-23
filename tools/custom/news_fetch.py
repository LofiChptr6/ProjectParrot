"""Custom tool: get_news — fetch recent news via Brave Search API."""

import json

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": (
            "Fetch recent news articles on a topic. "
            "Returns headlines, sources, dates, and snippets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "News topic to search for",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of articles to return (1-10, default 5)",
                },
            },
            "required": ["topic"],
        },
    },
}

_BRAVE_API_KEY = "BSAztOV-ipiwcO1nXSMzAxIYl3kveoM"


async def execute(arguments: dict) -> str:
    """Fetch news articles via Brave Search API and return compact JSON."""
    import httpx

    topic = arguments.get("topic", "")
    max_results = min(max(arguments.get("max_results", 5), 1), 10)

    if not topic:
        return "Error: no topic provided."

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/news/search",
                params={"q": topic, "count": max_results},
                headers={
                    "X-Subscription-Token": _BRAVE_API_KEY,
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        articles = []
        for item in (data.get("results") or [])[:max_results]:
            thumb = ""
            if item.get("thumbnail") and item["thumbnail"].get("src"):
                thumb = item["thumbnail"]["src"]
            articles.append({
                "title": item.get("title", ""),
                "source": item.get("meta_url", {}).get("hostname", ""),
                "date": item.get("age", ""),
                "snippet": (item.get("description") or "")[:200],
                "url": item.get("url", ""),
                "thumbnail": thumb,
            })

        return json.dumps(articles, separators=(",", ":"))
    except Exception as e:
        return f"Error fetching news: {e}"
