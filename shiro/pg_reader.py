"""Read-only PostgreSQL queries for Shiro's conversation analysis."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

log = logging.getLogger("shiro.pg")

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore[assignment]


_RECENT_QUERY = """\
SELECT id, created_at, triggered_by, source, user_id,
       conversation_id, pass_number, tool_round,
       messages, response_content, response_tool_calls,
       prompt_tokens, completion_tokens, latency_ms, finish_reason
FROM llm_call_log
WHERE triggered_by NOT IN ('shiro', 'cache_warm', 'warmup', 'repair')
  AND created_at > now() - make_interval(mins => $1)
ORDER BY created_at ASC
"""

_LAST_SHIRO_QUERY = """\
SELECT MAX(created_at) AS ts FROM llm_call_log WHERE triggered_by = 'shiro'
"""

_LAST_MOCHA_QUERY = """\
SELECT MAX(created_at) AS ts FROM llm_call_log
WHERE triggered_by NOT IN ('shiro', 'cache_warm', 'warmup', 'repair')
"""


async def create_pool(dsn: str) -> Any:
    if asyncpg is None:
        log.warning("asyncpg not installed — Shiro PG reader disabled")
        return None
    return await asyncpg.create_pool(dsn, min_size=1, max_size=2)


async def fetch_recent_conversations(
    pool, since_minutes: int = 120,
) -> list[list[dict]]:
    """Fetch recent Mocha conversations grouped by conversation_id.

    Returns a list of conversation groups.  Each group is a list of
    exchange dicts sorted chronologically::

        [{"user": str, "assistant": str, "source": str, "latency_ms": float,
          "tokens": int, "timestamp": str, "triggered_by": str}, ...]
    """
    rows = await pool.fetch(_RECENT_QUERY, since_minutes)
    if not rows:
        return []

    conversations: dict[str, list[dict]] = {}
    for row in rows:
        cid = row["conversation_id"] or str(row["id"])
        try:
            messages = json.loads(row["messages"])
        except (json.JSONDecodeError, TypeError):
            continue

        # Extract the last user message from the messages array
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        prompt_tok = row["prompt_tokens"] or 0
        comp_tok = row["completion_tokens"] or 0

        conversations.setdefault(cid, []).append({
            "user": user_msg,
            "assistant": row["response_content"] or "",
            "source": row["source"] or "",
            "triggered_by": row["triggered_by"] or "",
            "latency_ms": row["latency_ms"],
            "tokens": prompt_tok + comp_tok,
            "timestamp": str(row["created_at"]),
            "pass_number": row["pass_number"],
            "tool_round": row["tool_round"],
            "tool_calls": row["response_tool_calls"],
        })

    return list(conversations.values())


async def last_shiro_analysis(pool) -> datetime | None:
    """Timestamp of the most recent Shiro analysis, or None."""
    row = await pool.fetchrow(_LAST_SHIRO_QUERY)
    return row["ts"] if row and row["ts"] else None


async def last_mocha_activity(pool) -> datetime | None:
    """Timestamp of the most recent Mocha LLM call, or None."""
    row = await pool.fetchrow(_LAST_MOCHA_QUERY)
    return row["ts"] if row and row["ts"] else None
