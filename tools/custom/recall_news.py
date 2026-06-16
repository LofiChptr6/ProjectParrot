"""Custom tool: recall_news — Mocha searches the news she has SHARED before.

When Ika replies to (or asks about) a news item Mocha sent, she uses this to pull
up similar pieces she's surfaced — a semantic search over the ``shared_news``
records the autonomy loop writes to mem0 (see autonomy/news_ledger.py). Returns
each match's headline, source, when it was shared, and the link, so she can talk
about them concretely instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "recall_news",
        "description": (
            "Search the news articles you've SHARED with Ika before, by topic or "
            "keyword. Use when Ika replies to or asks about a news item you sent "
            "('that Fed story', 'anything else like the BIRK thing', 'what was that "
            "space article'). Returns similar shared headlines with source + link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Topic/keywords to match against your shared news, "
                                   "e.g. 'federal reserve', 'semiconductors', 'BIRK'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many to return (default 4, max 8).",
                },
            },
            "required": ["query"],
        },
    },
}


def _ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        secs = (datetime.now(timezone.utc) - then).total_seconds()
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
        return f"{int(secs // 86400)}d ago"
    except Exception:
        return ""


async def execute(arguments: dict) -> str:
    from memory import mem0_store
    from autonomy.news_ledger import KIND

    query = (arguments.get("query") or "").strip()
    if not query:
        return "recall_news: provide a `query` (topic or keywords) to search your shared news."
    k = int(arguments.get("max_results", 4) or 4)
    k = max(1, min(k, 8))

    try:
        # Filter to shared-news records only; user_id defaults to the single
        # operator (config memory.mem0_user_id).
        hits = await mem0_store.search(query, k=k, filters={"kind": KIND})
    except Exception as exc:
        return f"recall_news: search failed ({exc})."

    if not hits:
        return f"No shared news matched '{query}' — nothing like it in what I've sent you."

    lines = [f"Shared news matching '{query}':"]
    for h in hits:
        meta = h.get("metadata") or {}
        headline = meta.get("headline") or h.get("text") or ""
        source = meta.get("source") or ""
        url = meta.get("url") or ""
        when = _ago(meta.get("shared_at") or "")
        bits = [f"- {headline}".rstrip()]
        tail = " · ".join(x for x in (source, when) if x)
        if tail:
            bits.append(f"  ({tail})")
        if url:
            bits.append(f"  {url}")
        lines.append("".join(bits))
    return "\n".join(lines)
