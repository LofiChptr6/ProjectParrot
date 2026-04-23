"""
PostgreSQL call logger — fire-and-forget async inserts for every LLM call.

This is a **write-only** logging sink.  The agent never reads from this table
at runtime; it exists purely for offline evaluation / analytics.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

log = logging.getLogger("call_log")

try:
    import asyncpg  # type: ignore[import-untyped]
except ImportError:
    asyncpg = None  # type: ignore[assignment]

_pool: Any = None  # asyncpg.Pool | None
_enabled: bool = False


# ------------------------------------------------------------------
#  Public dataclass — callers build this to describe *who* triggered a call
# ------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class CallContext:
    triggered_by: str
    source: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    pass_number: int | None = None
    tool_round: int | None = None


# ------------------------------------------------------------------
#  Pool lifecycle
# ------------------------------------------------------------------

async def init_pool(dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
    global _pool, _enabled
    if asyncpg is None:
        log.warning("asyncpg not installed — call logging disabled")
        return
    _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    _enabled = True


async def close_pool() -> None:
    global _pool, _enabled
    if _pool is not None:
        await _pool.close()
    _pool = None
    _enabled = False


# ------------------------------------------------------------------
#  Schema bootstrap (idempotent)
# ------------------------------------------------------------------

_DDL = """\
CREATE TABLE IF NOT EXISTS llm_call_log (
    id                  BIGSERIAL PRIMARY KEY,
    call_id             UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Trigger context
    triggered_by        TEXT NOT NULL,
    source              TEXT,
    user_id             TEXT,
    conversation_id     TEXT,
    pass_number         SMALLINT,
    tool_round          SMALLINT,

    -- Model config
    model               TEXT NOT NULL,
    temperature         REAL,
    max_tokens          INTEGER,
    stream              BOOLEAN NOT NULL,
    enable_thinking     BOOLEAN,
    tools_provided      BOOLEAN NOT NULL DEFAULT FALSE,

    -- Input
    messages            JSONB NOT NULL,
    message_count       INTEGER NOT NULL,

    -- Output
    response_content    TEXT,
    response_tool_calls JSONB,
    finish_reason       TEXT,
    error               TEXT,

    -- Timing
    latency_ms          REAL,
    ttft_ms             REAL,

    -- Token counts (from vLLM usage)
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_llm_log_created  ON llm_call_log (created_at);
CREATE INDEX IF NOT EXISTS idx_llm_log_conv     ON llm_call_log (conversation_id);
CREATE INDEX IF NOT EXISTS idx_llm_log_trigger  ON llm_call_log (triggered_by);
"""


async def ensure_schema() -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(_DDL)


# ------------------------------------------------------------------
#  Insert
# ------------------------------------------------------------------

_INSERT = """\
INSERT INTO llm_call_log (
    triggered_by, source, user_id, conversation_id, pass_number, tool_round,
    model, temperature, max_tokens, stream, enable_thinking, tools_provided,
    messages, message_count,
    response_content, response_tool_calls, finish_reason, error,
    latency_ms, ttft_ms,
    prompt_tokens, completion_tokens, total_tokens
) VALUES (
    $1,$2,$3,$4,$5,$6,
    $7,$8,$9,$10,$11,$12,
    $13,$14,
    $15,$16,$17,$18,
    $19,$20,
    $21,$22,$23
)
"""


async def log_call(
    ctx: CallContext,
    *,
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    stream: bool,
    enable_thinking: bool | None = None,
    tools_provided: bool = False,
    messages: list[dict],
    response_content: str | None = None,
    response_tool_calls: list[dict] | None = None,
    finish_reason: str | None = None,
    error: str | None = None,
    latency_ms: float | None = None,
    ttft_ms: float | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    """Insert one row.  Designed to be wrapped in ``asyncio.create_task()``."""
    if not _enabled or not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                _INSERT,
                # trigger context
                ctx.triggered_by,
                ctx.source,
                ctx.user_id,
                ctx.conversation_id,
                ctx.pass_number,
                ctx.tool_round,
                # model config
                model,
                temperature,
                max_tokens,
                stream,
                enable_thinking,
                tools_provided,
                # input
                json.dumps(messages, ensure_ascii=False),
                len(messages),
                # output
                response_content,
                json.dumps(response_tool_calls, ensure_ascii=False) if response_tool_calls else None,
                finish_reason,
                error,
                # timing
                latency_ms,
                ttft_ms,
                # tokens
                prompt_tokens,
                completion_tokens,
                total_tokens,
            )
    except Exception as exc:
        log.warning("Failed to log LLM call: %s", exc)
