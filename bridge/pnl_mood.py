"""P&L-driven mood — a soft emotional prior from the desk's COMBINED P&L.

Mocha and Ika are in this together: when the desk is up she feels it, when it's
down she's a little worried. This reads the desk's *combined* (realized +
unrealized) day P&L read-only and turns it into a one-line inner-state note that
BOTH brains inject as soft prompt coloring — never a script, never a number she
recites unprompted.

How it reads the desk (prime directive — never impact Corvus):
  - calls the read-only ``get_balances`` opus tool via the existing subprocess
    proxy (``combined_pnl_today`` = realized_today + unrealized; NAV for scale);
  - CACHED for ``refresh_s`` so we never spawn the opus venv more than ~once every
    few minutes; refresh runs in the background and never blocks a turn;
  - fail-silent: if the desk is unreachable, mood is None and callers inject
    nothing (the brains fall back to their existing behavior).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

log = logging.getLogger("bridge.pnl_mood")

# Cache: refreshed in the background when stale; reads are non-blocking.
_cache: dict = {"at": 0.0, "data": None}
_refresh_inflight: bool = False


def _cfg() -> dict:
    defaults = {"enabled": True, "refresh_s": 180.0}
    try:
        from bridge.server import full_config
        user = (full_config.get("autonomy") or {}).get("mood") or {}
    except Exception:
        user = {}
    merged = dict(defaults)
    merged.update({k: v for k, v in user.items() if v is not None})
    return merged


# ---------------------------------------------------------------------------
#  Reading the desk (read-only, cached)
# ---------------------------------------------------------------------------

def _coerce_float(d: dict, *keys) -> Optional[float]:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


async def _fetch() -> Optional[dict]:
    """One read-only get_balances call → {combined, nav, pct}. None on any error."""
    try:
        from tools.custom._opus_proxy import call_opus
        raw = await call_opus("get_balances", {}, "Desk balances")
        data = json.loads(raw)
        if not isinstance(data, dict) or "error" in data and len(data) <= 2:
            return None
        combined = _coerce_float(data, "combined_pnl_today")
        if combined is None:
            realized = _coerce_float(data, "realized_pnl_today", "realized_pnl") or 0.0
            unrealized = _coerce_float(data, "unrealized_pnl") or 0.0
            combined = realized + unrealized
        nav = _coerce_float(data, "nav", "net_liquidation", "NetLiquidation",
                            "equity_with_loan", "equity")
        pct = (combined / nav) if (nav and nav > 0) else None
        return {"combined": combined, "nav": nav, "pct": pct, "at": time.time()}
    except Exception as exc:
        log.debug("pnl_mood: fetch failed: %s", exc)
        return None


async def _refresh() -> None:
    global _refresh_inflight
    try:
        data = await _fetch()
        if data is not None:
            _cache["data"] = data
            _cache["at"] = time.monotonic()
    finally:
        _refresh_inflight = False


def _maybe_kick_refresh() -> None:
    """Kick a background refresh if the cache is stale. Never blocks/raises."""
    global _refresh_inflight
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return
    if _refresh_inflight:
        return
    if (time.monotonic() - _cache["at"]) < float(cfg.get("refresh_s", 180.0)) and _cache["data"]:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (import-time) — skip
    _refresh_inflight = True
    loop.create_task(_refresh())


# ---------------------------------------------------------------------------
#  Mapping P&L → mood
# ---------------------------------------------------------------------------

def _bucket(data: dict) -> Optional[dict]:
    """Return {label, gist} from the cached P&L, or None if not enough signal."""
    pct = data.get("pct")
    combined = data.get("combined")
    if pct is None:
        # No NAV to scale by — fall back to coarse absolute thresholds so the
        # sign at least registers (magnitude bands are deliberately wide).
        if combined is None:
            return None
        if combined >= 2000:
            return {"label": "buoyant", "gist": "the desk's having a good day"}
        if combined > 200:
            return {"label": "pleased", "gist": "the desk's in the green"}
        if combined >= -200:
            return {"label": "steady", "gist": "the desk's roughly flat today"}
        if combined > -2000:
            return {"label": "uneasy", "gist": "the desk's down a bit today"}
        return {"label": "worried", "gist": "the desk's having a rough day"}
    if pct >= 0.015:
        return {"label": "buoyant", "gist": "the desk's clearly up today"}
    if pct > 0.003:
        return {"label": "pleased", "gist": "the desk's in the green today"}
    if pct >= -0.003:
        return {"label": "steady", "gist": "the desk's roughly flat today"}
    if pct > -0.015:
        return {"label": "uneasy", "gist": "the desk's slipping a little today"}
    return {"label": "worried", "gist": "the desk's down hard today"}


def current_mood() -> Optional[str]:
    """The current P&L mood label, or None if unavailable. Kicks a refresh."""
    _maybe_kick_refresh()
    data = _cache.get("data")
    if not data:
        return None
    b = _bucket(data)
    return b["label"] if b else None


def _format_mood_message(b: dict) -> Optional[str]:
    """Render the inner-state block from a mood bucket. Pure (testable).

    Returns None for the neutral bucket: a flat day is not a feeling, and
    injecting "the desk's roughly flat" every turn primed desk-talk on turns
    that had nothing to do with it ("you there?" → "the desk's flat but
    breathing"). The line only exists when there's genuine color to carry."""
    if b["label"] == "steady":
        return None
    return (
        "[Inner state]\n"
        f"Ika runs a trading desk and you keep him company through the day. "
        f"Right now {b['gist']}, so you feel {b['label']}. Let it color your tone "
        "(a little brighter when you're up, a little more careful when you're "
        "down) — but do NOT announce P&L figures or bring up the desk unless Ika "
        "does first. This is mood, not content, and the desk is his work, not "
        "your whole world."
    )


def mood_system_message() -> Optional[str]:
    """A soft inner-state block both brains can inject, or None if unavailable.

    Deliberately tells her NOT to recite P&L numbers unprompted — it colors tone,
    not content, so she stays in character instead of reading out a ticker tape."""
    _maybe_kick_refresh()
    data = _cache.get("data")
    if not data:
        return None
    b = _bucket(data)
    if not b:
        return None
    return _format_mood_message(b)
