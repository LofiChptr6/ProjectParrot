#!/usr/bin/env python3
"""Quick-and-dirty PG query tool for llm_call_log.

Usage:
    python scripts/query_pg.py                  # recent conversations (last 2h)
    python scripts/query_pg.py --hours 8        # last 8 hours
    python scripts/query_pg.py --sql "SELECT ..." # arbitrary SQL
    python scripts/query_pg.py --conv CONV_ID   # full conversation by ID
"""

import argparse
import asyncio

import asyncpg

DSN = "postgresql://mocha:5369@127.0.0.1:5432/mocha"

RECENT_CONVERSATIONS = """
SELECT id, created_at, triggered_by, source,
       messages->-1->>'content' AS user_message,
       LEFT(response_content, 400) AS response_preview,
       ROUND(latency_ms::numeric) AS latency_ms,
       prompt_tokens, completion_tokens
FROM llm_call_log
WHERE triggered_by NOT IN ('shiro', 'cache_warm', 'warmup', 'repair')
  AND created_at > now() - interval '{hours} hours'
ORDER BY created_at DESC
LIMIT 50;
"""

FULL_CONVERSATION = """
SELECT id, created_at, triggered_by, pass_number, tool_round,
       messages->-1->>'content' AS last_msg,
       LEFT(response_content, 800) AS response,
       finish_reason
FROM llm_call_log
WHERE conversation_id = $1
ORDER BY id;
"""

PERFORMANCE = """
SELECT triggered_by,
       COUNT(*) AS calls,
       ROUND(AVG(latency_ms)::numeric) AS avg_latency_ms,
       ROUND(AVG(prompt_tokens)::numeric) AS avg_prompt_tok,
       ROUND(AVG(completion_tokens)::numeric) AS avg_comp_tok
FROM llm_call_log
WHERE created_at > now() - interval '{hours} hours'
GROUP BY triggered_by
ORDER BY calls DESC;
"""


def _fmt_row(row, cols):
    parts = []
    for c in cols:
        val = row[c]
        if val is None:
            continue
        s = str(val)
        if len(s) > 120:
            s = s[:120] + "…"
        parts.append(f"  {c}: {s}")
    return "\n".join(parts)


async def main():
    parser = argparse.ArgumentParser(description="Query llm_call_log")
    parser.add_argument("--hours", type=float, default=2, help="Lookback window (default: 2)")
    parser.add_argument("--sql", type=str, help="Run arbitrary SQL")
    parser.add_argument("--conv", type=str, help="Full conversation by conversation_id")
    parser.add_argument("--perf", action="store_true", help="Performance summary")
    args = parser.parse_args()

    conn = await asyncpg.connect(DSN)

    try:
        if args.sql:
            rows = await conn.fetch(args.sql)
        elif args.conv:
            rows = await conn.fetch(FULL_CONVERSATION, args.conv)
        elif args.perf:
            rows = await conn.fetch(PERFORMANCE.format(hours=args.hours))
        else:
            rows = await conn.fetch(RECENT_CONVERSATIONS.format(hours=args.hours))

        if not rows:
            print("(no results)")
            return

        cols = list(rows[0].keys())
        for i, row in enumerate(rows):
            print(f"--- [{i+1}] ---")
            print(_fmt_row(row, cols))
            print()

        print(f"({len(rows)} row(s))")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
