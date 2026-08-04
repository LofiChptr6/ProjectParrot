"""Daily LLM token quota for anonymous users.

Registered users have no cap. Anonymous users (account_type='anon') are
limited to a fixed number of total_tokens (prompt + completion) per local
calendar day, summed from llm_call_log. The cap is configured under the
`quota:` block in config.yaml.

The "today" window is defined in the bridge host's local timezone — good
enough for v1; per-user TZ is tracked as a follow-up.
"""

from __future__ import annotations

from typing import Any

from bridge import call_log


# Loaded from config.yaml on import below; see _load_caps().
ANON_DAILY_TOKEN_CAP: int | None = 150_000
REGISTERED_DAILY_TOKEN_CAP: int | None = None  # None = unlimited


def _load_caps() -> None:
    """Pull cap values from config.yaml on first import. Best-effort; fails
    closed (= cap stays at module defaults) if config can't be parsed."""
    global ANON_DAILY_TOKEN_CAP, REGISTERED_DAILY_TOKEN_CAP
    try:
        from pathlib import Path
        import yaml  # type: ignore[import-untyped]
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        q = cfg.get("quota") or {}
        if "anon_daily_token_cap" in q:
            ANON_DAILY_TOKEN_CAP = q["anon_daily_token_cap"]  # may be None
        if "registered_daily_token_cap" in q:
            REGISTERED_DAILY_TOKEN_CAP = q["registered_daily_token_cap"]
    except Exception:
        pass  # use module defaults


_load_caps()


def cap_for(account_type: str) -> int | None:
    """Return the daily total_tokens cap for an account_type.

    None = unlimited. Anything not 'anon' is treated as registered.
    """
    if account_type == "anon":
        return ANON_DAILY_TOKEN_CAP
    return REGISTERED_DAILY_TOKEN_CAP


# 60s TTL cache for the per-user daily SUM — this aggregate ran on the TTFT
# path of every anon turn. A cap check may lag real usage by up to a minute,
# which is fine for an abuse cap (the cap is approximate by nature: the
# in-flight turn's own tokens aren't counted either).
_USED_CACHE: dict[str, tuple[float, int]] = {}
_USED_CACHE_TTL_S = 60.0


async def tokens_used_today(user_id: str) -> int:
    """Sum total_tokens for this user across all LLM calls today (host TZ).

    Returns 0 if user_id is None/empty, the pool isn't initialized, or no rows.
    Cached for 60s per user.
    """
    if not user_id:
        return 0
    import time as _time
    hit = _USED_CACHE.get(user_id)
    now = _time.monotonic()
    if hit and (now - hit[0]) < _USED_CACHE_TTL_S:
        return hit[1]
    pool = call_log.get_pool()
    if pool is None:
        return 0
    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(total_tokens), 0)::BIGINT AS used
        FROM llm_call_log
        WHERE user_id = $1
          AND created_at >= date_trunc('day', now())
        """,
        user_id,
    )
    used = int(row["used"] or 0) if row else 0
    _USED_CACHE[user_id] = (now, used)
    # Bound the cache — anon ids churn.
    if len(_USED_CACHE) > 512:
        for k, _ in sorted(_USED_CACHE.items(), key=lambda kv: kv[1][0])[:256]:
            _USED_CACHE.pop(k, None)
    return used


async def check_quota(
    user_id: str | None, account_type: str
) -> tuple[bool, int, int | None]:
    """Pre-LLM-call gate: should this call be allowed?

    Returns (allowed, used_today, cap).
      - allowed: False if used_today >= cap (and cap is finite).
      - used_today: 0 when no quota applies (cap is None) — saves a query.
      - cap: None if unlimited.
    """
    cap = cap_for(account_type)
    if cap is None:
        return True, 0, None
    used = await tokens_used_today(user_id or "")
    return (used < cap), used, cap


def quota_refusal_message() -> str:
    """The line Mocha says when she's out of guest tokens for the day.

    Kept short, in Mocha's voice, with a clear next step. No exclamation marks
    or assistant-coded politeness.
    """
    return (
        "hit my limit for guest chats today. "
        "if you want to keep going — settings → account → sign up. "
        "it's free and the cap goes away."
    )
