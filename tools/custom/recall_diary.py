"""Custom tool: recall_diary — Mocha (or Nori) looks up a past day's diary page.

Two modes:
  - ``date="YYYY-MM-DD"``:     fetch that specific day
  - ``query="some topic"``:    semantic search over all finalised pages

Either returns the matching page's summary + activities so the caller has
concrete material to narrate from. If both are given, ``date`` wins.
"""

from __future__ import annotations

import json
from typing import Any

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "recall_diary",
        "description": (
            "Look up a past day's diary page from Mocha's memory. Use for "
            "\"what did we do yesterday\", \"when did we listen to that song\", "
            "\"remind me about that stock we checked last week\". Provide EITHER "
            "`date` (YYYY-MM-DD) for a specific day OR `query` for semantic search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Exact date in YYYY-MM-DD (user's timezone).",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Semantic search query — e.g. 'lofi music', 'SOXL stock', "
                        "'Tokyo weather'. Matches against each day's summary."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max pages to return on a query (default 3).",
                },
            },
        },
    },
}


def _format_page(page: dict[str, Any]) -> str:
    """Render a diary page as a compact human-readable block."""
    if not page:
        return "(no page)"
    bits = []
    bits.append(f"[{page.get('date')}]  (status: {page.get('status','?')})")
    summary = (page.get("summary") or "").strip()
    if summary:
        bits.append(summary)
    highlights = page.get("highlights") or []
    if highlights:
        bits.append("Highlights:")
        for h in highlights[:4]:
            bits.append(f"  • {h}")
    activities = page.get("activities") or []
    if activities:
        bits.append(f"Activity log ({len(activities)} tool call(s)):")
        for a in activities[:12]:
            bits.append(f"  {a.get('t','')}  {a.get('tool','')} — {a.get('brief','')}")
        if len(activities) > 12:
            bits.append(f"  …(+{len(activities) - 12} more)")
    return "\n".join(bits)


async def execute(arguments: dict) -> str:
    from memory import diary_store

    date = (arguments.get("date") or "").strip()
    query = (arguments.get("query") or "").strip()
    max_results = int(arguments.get("max_results", 3) or 3)
    max_results = max(1, min(max_results, 8))

    if not date and not query:
        return (
            "recall_diary: provide either `date` (YYYY-MM-DD) or `query` "
            "(e.g. 'lofi music'). Without either, there's nothing to look up."
        )

    # Date mode — direct lookup
    if date:
        page = await diary_store.get_page(date)
        if not page:
            return f"No diary page for {date}."
        return _format_page(page)

    # Query mode — semantic search over summaries
    hits = await diary_store.search_pages(query, k=max_results)
    if not hits:
        return f"No diary pages matched '{query}'."

    # For each hit we only have summary + metadata; load the full page to
    # include activities if the caller wants to drill in. Keep the block
    # compact so token cost stays low.
    out = [f"Top {len(hits)} diary matches for '{query}':"]
    for h in hits:
        full = await diary_store.get_page(h["date"])
        block = _format_page(full or h)
        out.append("")
        out.append(block)
    return "\n".join(out)
