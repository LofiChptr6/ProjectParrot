"""Custom tool: show_slides — display visual slides in the web UI."""

import json
import time

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "show_slides",
        "description": (
            "Show slides in the web UI panel. Use for presenting data visually. "
            "Slide types: 'title' (heading + subtitle), 'bullets' (title + list), "
            "'table' (headers + rows), 'chart' (Chart.js chart), "
            "'candlestick' (single stock chart — provide symbol), "
            "'multi_chart' (multiple stock charts on one scrollable slide — provide symbols array), "
            "'news_feed' (scrollable news cards with thumbnails — provide articles array), "
            "'image' (url + caption)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title shown in the slide panel header",
                },
                "slides": {
                    "type": "array",
                    "description": "Array of slide objects",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["title", "bullets", "table", "chart", "candlestick", "multi_chart", "news_feed", "image"],
                                "description": "Slide layout type",
                            },
                            "title": {
                                "type": "string",
                                "description": "Slide title",
                            },
                            "subtitle": {
                                "type": "string",
                                "description": "(title slide) Subtitle text",
                            },
                            "items": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "(bullets slide) Bullet point strings",
                            },
                            "headers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "(table slide) Column headers",
                            },
                            "rows": {
                                "type": "array",
                                "description": "(table slide) Array of row arrays",
                            },
                            "chart_type": {
                                "type": "string",
                                "enum": ["line", "bar", "pie", "doughnut"],
                                "description": "(chart slide) Chart.js chart type",
                            },
                            "labels": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "(chart slide) X-axis / category labels",
                            },
                            "datasets": {
                                "type": "array",
                                "description": (
                                    "(chart slide) Array of dataset objects with "
                                    "'label' (string) and 'data' (number array)"
                                ),
                            },
                            "url": {
                                "type": "string",
                                "description": "(image slide) Image URL",
                            },
                            "caption": {
                                "type": "string",
                                "description": "(image slide) Caption text",
                            },
                            "symbol": {
                                "type": "string",
                                "description": "(candlestick slide) Ticker symbol (e.g. 'NVDA'). Chart data is fetched automatically.",
                            },
                            "price": {
                                "type": "number",
                                "description": "(candlestick slide) Current price to display in header",
                            },
                            "change": {
                                "type": "number",
                                "description": "(candlestick slide) Percent change to display in header",
                            },
                            "symbols": {
                                "type": "array",
                                "description": "(multi_chart slide) Array of ticker objects: [{symbol, price, change}, ...] or just strings ['AAPL','TSLA']",
                            },
                            "articles": {
                                "type": "array",
                                "description": "(news_feed slide) Array of article objects: [{title, snippet, source, date, url, thumbnail}, ...]",
                            },
                        },
                        "required": ["type"],
                    },
                },
                "auto_advance_sec": {
                    "type": "integer",
                    "description": "Auto-advance every N seconds (0 = manual, default 0)",
                },
            },
            "required": ["title", "slides"],
        },
    },
}


async def execute(arguments: dict) -> str:
    """Show slides in the web frontend."""
    from bridge.server import _broadcast_to_unity, _set_open_modal

    title = arguments.get("title", "Slides")
    slides = arguments.get("slides", [])
    auto_advance = arguments.get("auto_advance_sec", 0)

    if not slides:
        return "Error: no slides provided."

    pres_id = f"pres_{int(time.time() * 1000)}"

    await _broadcast_to_unity({
        "type": "ui_command",
        "action": "create_presentation",
        "presentation": {
            "id": pres_id,
            "title": title,
            "slides": slides,
            "auto_advance_sec": auto_advance,
        },
    })
    # Presentations are exclusive — a new one replaces the old, so we set,
    # not append.
    _set_open_modal("presentation", {"id": pres_id, "title": title})

    slide_types = [s.get("type", "?") for s in slides]
    return (
        f"Slides '{title}' shown ({len(slides)} slides: "
        f"{', '.join(slide_types)})."
    )
