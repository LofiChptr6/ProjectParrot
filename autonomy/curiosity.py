"""
Curiosity — a small pool of fresh, genuinely interesting news items that Mocha
surfaces when idle, instead of content-free "it's been quiet" filler.

The pool is refilled on a throttled timer by calling the existing ``get_news``
tool (Brave) with a rotating mix of "her-taste" topics (science / space / odd)
and desk-relevant topics (markets / macro). Items are deduped against a seen-set
so she never brings up the same thing twice, which also kills the repetition
problem that plagued the old idle check-ins.

Prime directive — Mocha must never impact Corvus, and nothing here may break the
autonomy heartbeat. Every public coroutine swallows its own errors and degrades
to "no item" / "no refill". Brave usage is bounded by ``refill_interval_s`` and
only happens when a surface is actually present (the engine gates that).

Persistence: ``data/curiosity_pool.json``
    {
      "pool":        [ {title, source, snippet, url, topic, kind} ... ],  # unseen
      "seen":        [ "<dedup-hash>", ... ],
      "last_refill": <unix ts>,
      "rotation":    <int>,   # cursor into the interleaved topic list
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("curiosity")

ROOT = Path(__file__).resolve().parent.parent
_PATH = ROOT / "data" / "curiosity_pool.json"
_lock = asyncio.Lock()

_DEFAULTS = {
    "enabled": True,
    "refill_interval_s": 2700,   # ~45 min between Brave refills
    "pool_target": 6,            # refill only when fewer than this remain unseen
    "fetch_per_topic": 5,        # articles requested per refill
    "max_seen": 500,             # cap on the dedup ledger
    # Her taste — small questions that are secretly enormous. Phrased with mild
    # recency cues so Brave leans toward concrete articles over section pages.
    "topics_self": [
        "latest space exploration discovery",
        "new neuroscience study brain",
        "physics discovery this week",
        "recent archaeology discovery",
        "strange offbeat science news",
        "deep sea creature discovery",
        "AI research breakthrough this week",
        "new astronomy discovery telescope",
    ],
    # Desk-relevant — the world the trading floor lives in.
    "topics_desk": [
        "stock market today",
        "federal reserve interest rates",
        "technology company earnings",
        "global economy outlook",
    ],
}


def _cfg() -> dict:
    """Merge user config (autonomy.curiosity) over defaults on every access."""
    try:
        from bridge.server import full_config
        user = ((full_config.get("autonomy") or {}).get("curiosity")) or {}
    except Exception:
        user = {}
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in user.items() if v is not None})
    return merged


# ---------------------------------------------------------------------------
#  Persistence
# ---------------------------------------------------------------------------

def _load_sync() -> dict:
    if not _PATH.exists():
        return {"pool": [], "seen": [], "last_refill": 0.0, "rotation": 0}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        data.setdefault("pool", [])
        data.setdefault("seen", [])
        data.setdefault("last_refill", 0.0)
        data.setdefault("rotation", 0)
        return data
    except Exception as exc:
        log.warning("curiosity: failed to read pool (%s) — starting fresh", exc)
        return {"pool": [], "seen": [], "last_refill": 0.0, "rotation": 0}


def _save_sync(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        # Bound the seen ledger so the file can't grow without limit.
        max_seen = int(_cfg().get("max_seen", 500))
        if len(data.get("seen", [])) > max_seen:
            data["seen"] = data["seen"][-max_seen:]
        _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=0),
                         encoding="utf-8")
    except Exception as exc:
        log.warning("curiosity: failed to save pool: %s", exc)


def _key(item: dict) -> str:
    """Stable dedup key — prefer URL, fall back to the (lowercased) title."""
    basis = (item.get("url") or item.get("title") or "").strip().lower()
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
#  Topic rotation — interleave self/desk so a "mix" is guaranteed
# ---------------------------------------------------------------------------

def _held_ticker_topics() -> list[str]:
    """News queries for the tickers Ika's desk actually holds, so the idle feed
    is about OUR positions. Best-effort + non-blocking (held_tickers is cached)."""
    try:
        from bridge.server import held_tickers
        return [f"{t} stock news" for t in (held_tickers() or [])[:6]]
    except Exception:
        return []


def _topic_rotation() -> list[dict]:
    """Interleave topics, biased toward the desk (2 desk : 1 self) and seeded with
    Ika's actual holdings — so when Mocha surfaces something idle, it's mostly the
    markets/world the desk lives in, with her-taste science/space as a garnish."""
    cfg = _cfg()
    self_t = list(cfg.get("topics_self") or [])
    desk_t = list(cfg.get("topics_desk") or []) + _held_ticker_topics()
    out: list[dict] = []
    di = si = 0
    while di < len(desk_t) or si < len(self_t):
        for _ in range(2):  # 2 desk-relevant
            if di < len(desk_t):
                out.append({"topic": desk_t[di], "kind": "desk"})
                di += 1
        if si < len(self_t):  # : 1 her-taste
            out.append({"topic": self_t[si], "kind": "self"})
            si += 1
    return out


# ---------------------------------------------------------------------------
#  Refill
# ---------------------------------------------------------------------------

async def maybe_refill() -> int:
    """Top up the pool from ONE rotating topic if it's run low and the throttle
    window has elapsed. Returns the number of fresh items added (0 if skipped or
    on any failure). Never raises."""
    try:
        cfg = _cfg()
        if not cfg.get("enabled", True):
            return 0

        async with _lock:
            data = _load_sync()
            now = time.time()
            unseen = len(data.get("pool", []))
            if unseen >= int(cfg.get("pool_target", 6)):
                return 0
            if (now - float(data.get("last_refill", 0.0))) < float(cfg.get("refill_interval_s", 2700)):
                return 0

            rotation = _topic_rotation()
            if not rotation:
                return 0
            cursor = int(data.get("rotation", 0)) % len(rotation)
            choice = rotation[cursor]
            data["rotation"] = cursor + 1
            data["last_refill"] = now
            # Persist the advanced cursor/timestamp up front so a slow or failing
            # fetch doesn't cause us to hammer the same topic next tick.
            _save_sync(data)
            seen = set(data.get("seen", []))

        articles = await _fetch_topic(choice["topic"], int(cfg.get("fetch_per_topic", 5)))

        added = 0
        async with _lock:
            data = _load_sync()
            seen = set(data.get("seen", []))
            for art in articles:
                if not art.get("title") or not art.get("url"):
                    continue
                k = _key(art)
                if k in seen:
                    continue
                seen.add(k)
                data.setdefault("seen", []).append(k)
                data.setdefault("pool", []).append({
                    "title": art.get("title", ""),
                    "source": art.get("source", ""),
                    "snippet": (art.get("snippet") or "")[:240],
                    "url": art.get("url", ""),
                    "topic": choice["topic"],
                    "kind": choice["kind"],
                    # Time handling: keep Brave's relative age string AND stamp an
                    # absolute fetch time, so when this item is shared the ledger
                    # can resolve a real published_at + know how fresh it was.
                    "date": art.get("date", ""),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })
                added += 1
            _save_sync(data)

        if added:
            log.info("curiosity: +%d items from %r (%s)", added, choice["topic"], choice["kind"])
        return added
    except Exception as exc:
        log.warning("curiosity: refill failed: %s", exc)
        return 0


async def _fetch_topic(topic: str, count: int) -> list[dict]:
    """Call the get_news tool and parse its JSON. Returns [] on any error."""
    try:
        from tools.custom import news_fetch
        raw = await news_fetch.execute({"topic": topic, "max_results": count})
        if not raw or raw.startswith("Error"):
            log.info("curiosity: get_news(%r) → %s", topic, (raw or "")[:80])
            return []
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except Exception as exc:
        log.warning("curiosity: fetch %r failed: %s", topic, exc)
        return []


# ---------------------------------------------------------------------------
#  Consumption
# ---------------------------------------------------------------------------

async def next_item() -> Optional[dict]:
    """Pop the oldest unseen item from the pool (FIFO) and return it, or None if
    the pool is empty / disabled. The item's key is already in ``seen`` from
    refill, so it won't be re-pooled. Never raises."""
    try:
        if not _cfg().get("enabled", True):
            return None
        async with _lock:
            data = _load_sync()
            pool = data.get("pool", [])
            if not pool:
                return None
            item = pool.pop(0)
            data["pool"] = pool
            _save_sync(data)
            return item
    except Exception as exc:
        log.warning("curiosity: next_item failed: %s", exc)
        return None


async def pool_status() -> dict:
    """Small introspection helper for admin/debugging. Never raises."""
    try:
        async with _lock:
            data = _load_sync()
        return {
            "pool_size": len(data.get("pool", [])),
            "seen_count": len(data.get("seen", [])),
            "last_refill": data.get("last_refill", 0.0),
            "next_topics": [t["topic"] for t in _topic_rotation()[:3]],
        }
    except Exception as exc:
        return {"error": str(exc)}
