"""News ledger — a durable record of the news items Mocha proactively SENT.

When the idle autonomy loop shares a news item over Telegram, we persist two things
so a later conversation about it actually works:

  (a) a fast keyed row in ``data/news_ledger.json`` ::

          { "by_id": { "<telegram_message_id>": {record} }, "order": [ids…] }

      so when Ika *replies* to that exact Telegram message, we resolve it back to
      the precise article deterministically (by message_id) — no guessing.

  (b) a verbatim ``mem0`` record (``infer=False``, ``kind='shared_news'``) so Mocha
      can *semantically* recall similar pieces she's surfaced before (the
      ``recall_news`` tool). infer=False keeps the headline/url intact instead of
      letting mem0's fact-extractor rewrite it.

Citation + time handling per record:
  headline · url · source · published_at (absolute, best-effort from Brave's
  relative "3 hours ago") · fetched_at (absolute, stamped at fetch) · shared_at
  (absolute, now) · topic · kind · take (Mocha's own one-line reaction).

Prime directive: every public function is fail-silent / fire-and-forget — a ledger
or mem0 hiccup can never break the autonomy heartbeat or touch Corvus.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("autonomy.news_ledger")

ROOT = Path(__file__).resolve().parent.parent
_PATH = ROOT / "data" / "news_ledger.json"
_MAX_RECORDS = 400
_lock = asyncio.Lock()

KIND = "shared_news"


# ---------------------------------------------------------------------------
#  Relative-age → absolute timestamp (Brave returns "3 hours ago", "2 days ago")
# ---------------------------------------------------------------------------

_AGE_UNITS = {
    "second": 1, "sec": 1,
    "minute": 60, "min": 60,
    "hour": 3600, "hr": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,   # ~30d, good enough for a mood/recency cue
    "year": 31536000,
}
_AGE_RE = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month|year)s?\s*ago", re.I)


def parse_published_at(age: str, *, now: Optional[datetime] = None) -> Optional[str]:
    """Best-effort absolute ISO-UTC for Brave's relative ``age`` string.

    Handles "3 hours ago", "2 days ago", "just now", "yesterday", and falls back
    to parsing an embedded ISO date. Returns None when it can't tell."""
    if not age:
        return None
    s = age.strip().lower()
    ref = now or datetime.now(timezone.utc)
    if s in ("just now", "now", "moments ago"):
        return ref.isoformat()
    if s == "yesterday":
        return (ref - timedelta(days=1)).isoformat()
    m = _AGE_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = _AGE_UNITS.get(m.group(2).lower())
        if unit:
            return (ref - timedelta(seconds=n * unit)).isoformat()
    # Maybe it's already an absolute date/ISO string.
    try:
        return datetime.fromisoformat(age.replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
#  Persistence
# ---------------------------------------------------------------------------

def _load_sync() -> dict:
    if not _PATH.exists():
        return {"by_id": {}, "order": []}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        data.setdefault("by_id", {})
        data.setdefault("order", [])
        return data
    except Exception as exc:
        log.warning("news_ledger: read failed (%s) — starting fresh", exc)
        return {"by_id": {}, "order": []}


def _save_sync(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        # Trim to the most-recent _MAX_RECORDS by insertion order.
        order = data.get("order", [])
        if len(order) > _MAX_RECORDS:
            drop = order[:-_MAX_RECORDS]
            for mid in drop:
                data["by_id"].pop(mid, None)
            data["order"] = order[-_MAX_RECORDS:]
        _PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("news_ledger: save failed: %s", exc)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

async def record_shared(message_id: Optional[int], item: dict, take: str,
                        user_id: Optional[str] = None) -> None:
    """Persist a shared news item: a keyed ledger row (if we know the Telegram
    message_id) AND a verbatim mem0 record for semantic recall. Never raises."""
    try:
        title = (item.get("title") or "").strip()
        if not title:
            return
        url = (item.get("url") or "").strip()
        source = (item.get("source") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        topic = item.get("topic") or ""
        kind = item.get("kind") or "self"
        fetched_at = item.get("fetched_at") or ""
        published_at = item.get("published_at") or parse_published_at(item.get("date") or "") or ""

        record = {
            "message_id": message_id,
            "headline": title,
            "url": url,
            "source": source,
            "snippet": snippet,
            "topic": topic,
            "kind": kind,
            "published_at": published_at,
            "fetched_at": fetched_at,
            "shared_at": _iso_now(),
            "take": (take or "").strip(),
        }

        # (a) Keyed ledger row — only when we actually captured a message_id
        #     (web-only shares have none; they're still recalled via mem0).
        if message_id is not None:
            async with _lock:
                data = _load_sync()
                mid = str(message_id)
                if mid not in data["by_id"]:
                    data["order"].append(mid)
                data["by_id"][mid] = record
                _save_sync(data)

        # (b) mem0 record for "find similar" — verbatim (infer=False) + citation
        #     metadata. Fire-and-forget; mem0 metadata must be scalars.
        try:
            from memory import mem0_store
            mem_text = title if not snippet else f"{title} — {snippet}"
            meta = {
                "kind": KIND,
                "headline": title,
                "url": url,
                "source": source,
                "topic": topic,
                "news_kind": kind,
                "published_at": published_at,
                "shared_at": record["shared_at"],
            }
            if message_id is not None:
                meta["telegram_message_id"] = int(message_id)
            await mem0_store.add(mem_text, role="assistant", user_id=user_id,
                                 metadata=meta, infer=False, fire_and_forget=True)
        except Exception as exc:
            log.warning("news_ledger: mem0 write failed: %s", exc)

        log.info("news_ledger: recorded share mid=%s %r", message_id, title[:80])
    except Exception as exc:
        log.warning("news_ledger: record_shared failed: %s", exc)


def get(message_id) -> Optional[dict]:
    """Look up the article a Telegram message_id referred to. Sync + fail-silent."""
    try:
        if message_id is None:
            return None
        data = _load_sync()
        return data.get("by_id", {}).get(str(message_id))
    except Exception as exc:
        log.warning("news_ledger: get failed: %s", exc)
        return None


def recent(n: int = 10) -> list[dict]:
    """Most-recent shared records (newest last). Never raises."""
    try:
        data = _load_sync()
        ids = data.get("order", [])[-n:]
        return [data["by_id"][m] for m in ids if m in data["by_id"]]
    except Exception:
        return []
