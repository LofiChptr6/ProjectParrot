"""
Diary store — per-day memory pages for Mocha.

Two surfaces, kept in sync:
- **Flat JSON file** at ``data/diary/YYYY-MM-DD.json`` — ground-truth source.
  Holds summary, highlights, themes, and the structured activities list.
  Human-readable, easy to delete, easy to rsync off if the user wants.
- **Chroma collection** ``parrot_diary`` — embedding of ``summary`` keyed by
  date, for semantic search ("which day did we listen to lofi?").

Separated from ``memory/mem0_store.py`` (facts about the user) so fact
retrieval in `_query_memories` never mixes with activity log entries.

Draft vs final:
- Today's page is ``status="draft"``. Mutable. Over-written every time the
  diary writer runs.
- At local-tz midnight the writer calls ``finalize_page`` which flips status
  to ``"final"``, the JSON becomes immutable in practice (nothing else writes
  to it), and the summary is embedded into Chroma.

Public API — everything async so the bridge can call it without blocking:
    save_page(data)              -> None       (overwrites if date exists)
    get_page(date)               -> dict|None
    recent_pages(n=30)           -> list[dict] (newest first, summary + date only)
    search_pages(query, k=5)     -> list[dict] (Chroma semantic, summary + date)
    finalize_page(date)          -> None       (flips status + embeds)
    delete_page(date)            -> bool
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date as _date
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("memory.diary")

ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = ROOT / "config.yaml"


def _load_cfg() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("diary", {}) or {}
    except Exception:
        return {}


def _data_dir() -> Path:
    cfg = _load_cfg()
    p = ROOT / cfg.get("data_dir", "data/diary")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _chroma_path() -> Path:
    cfg = _load_cfg()
    p = ROOT / cfg.get("chroma_path", "memory/diary_chroma")
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
#  Chroma client singleton
# ---------------------------------------------------------------------------

_chroma_client: Any = None
_chroma_collection: Any = None
_COLLECTION_NAME = "parrot_diary"


def _get_collection() -> Any:
    """Lazily build the Chroma client + collection. Uses the same chromadb
    package already pulled in by mem0, so no new deps."""
    global _chroma_client, _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection

    import chromadb

    _chroma_client = chromadb.PersistentClient(path=str(_chroma_path()))
    _chroma_collection = _chroma_client.get_or_create_collection(
        name=_COLLECTION_NAME,
        # Use the same embedder mem0 uses so search quality matches — but
        # Chroma's default (all-MiniLM-L6-v2 via sentence-transformers) is
        # identical to our mem0 config, so "default" is fine.
        metadata={"hnsw:space": "cosine"},
    )
    log.info("diary Chroma collection ready at %s", _chroma_path())
    return _chroma_collection


# ---------------------------------------------------------------------------
#  File I/O helpers (sync; wrapped in to_thread for async API)
# ---------------------------------------------------------------------------


def _page_path(date: str, user_id: Optional[str] = None) -> Path:
    # Hard sanity check: dates only, no "..", no slashes
    if "/" in date or ".." in date or "\\" in date:
        raise ValueError(f"invalid diary date: {date!r}")
    if user_id:
        if "/" in user_id or ".." in user_id or "\\" in user_id:
            raise ValueError(f"invalid user_id: {user_id!r}")
        d = _data_dir() / user_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{date}.json"
    return _data_dir() / f"{date}.json"


def _chroma_id(date: str, user_id: Optional[str] = None) -> str:
    return f"{user_id}:{date}" if user_id else date


def _read_page_sync(date: str, user_id: Optional[str] = None) -> Optional[dict]:
    p = _page_path(date, user_id)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("failed to read diary %s: %s", date, exc)
        return None


def _write_page_sync(data: dict, user_id: Optional[str] = None) -> None:
    date = data.get("date")
    if not date:
        raise ValueError("diary page missing 'date'")
    p = _page_path(date, user_id)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def _delete_page_sync(date: str, user_id: Optional[str] = None) -> bool:
    p = _page_path(date, user_id)
    existed = p.exists()
    if existed:
        p.unlink()
    # Also drop from Chroma if present. Chroma delete by id is idempotent.
    cid = _chroma_id(date, user_id)
    try:
        _get_collection().delete(ids=[cid])
    except Exception as exc:
        log.debug("Chroma delete %s no-op: %s", cid, exc)
    return existed


def _list_dates_sync(user_id: Optional[str] = None) -> list[str]:
    out: list[str] = []
    search_dir = (_data_dir() / user_id) if user_id else _data_dir()
    if not search_dir.exists():
        return out
    for p in search_dir.glob("*.json"):
        if p.name.endswith(".tmp"):
            continue
        out.append(p.stem)
    out.sort(reverse=True)   # newest first
    return out


def _upsert_chroma_sync(page: dict, user_id: Optional[str] = None) -> None:
    """Put the page's summary into Chroma keyed by date. Idempotent — re-writes
    the doc if the date already exists."""
    date = page.get("date")
    summary = (page.get("summary") or "").strip()
    if not (date and summary):
        return
    coll = _get_collection()
    cid = _chroma_id(date, user_id)
    meta = {
        "date": date,
        "user_id": user_id or "",
        "tz": page.get("tz") or "",
        "status": page.get("status") or "draft",
        "activity_count": int(page.get("activity_count") or 0),
    }
    try:
        coll.upsert(
            ids=[cid],
            documents=[summary],
            metadatas=[meta],
        )
    except Exception as exc:
        log.warning("Chroma upsert failed for %s: %s", cid, exc)


def _search_chroma_sync(query: str, k: int, user_id: Optional[str] = None) -> list[dict]:
    coll = _get_collection()
    where = {"user_id": user_id} if user_id else {"user_id": ""}
    try:
        res = coll.query(query_texts=[query], n_results=k, where=where)
    except Exception as exc:
        log.warning("Chroma query failed: %s", exc)
        return []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    out: list[dict] = []
    for i, cid in enumerate(ids):
        date = (metas[i] or {}).get("date", cid.split(":")[-1]) if i < len(metas) else cid
        out.append({
            "date": date,
            "summary": docs[i] if i < len(docs) else "",
            "tz": (metas[i] or {}).get("tz", "") if i < len(metas) else "",
            "status": (metas[i] or {}).get("status", "") if i < len(metas) else "",
            "activity_count": (metas[i] or {}).get("activity_count", 0) if i < len(metas) else 0,
            "relevance": 1.0 - float(dists[i]) if i < len(dists) else 0.0,
        })
    return out


# ---------------------------------------------------------------------------
#  Public async API
# ---------------------------------------------------------------------------

async def save_page(page: dict, user_id: Optional[str] = None) -> None:
    """Write a diary page to disk. Mirrors summary into Chroma so search works
    on both draft and final pages (the draft is updated in place as the day
    progresses)."""
    def _sync():
        _write_page_sync(page, user_id)
        _upsert_chroma_sync(page, user_id)
    await asyncio.to_thread(_sync)


async def get_page(date: str, user_id: Optional[str] = None) -> Optional[dict]:
    return await asyncio.to_thread(_read_page_sync, date, user_id)


async def recent_pages(n: int = 30, user_id: Optional[str] = None) -> list[dict]:
    """Return summaries for the N most-recent dates (newest first). Excludes
    the full activities list — the UI fetches that on expand."""
    def _sync() -> list[dict]:
        out: list[dict] = []
        for date in _list_dates_sync(user_id)[:n]:
            page = _read_page_sync(date, user_id)
            if not page:
                continue
            out.append({
                "date": page.get("date"),
                "tz": page.get("tz"),
                "status": page.get("status"),
                "summary": page.get("summary", ""),
                "activity_count": page.get("activity_count", 0),
                "interaction_count": page.get("interaction_count", 0),
                "written_at": page.get("written_at"),
            })
        return out
    return await asyncio.to_thread(_sync)


async def search_pages(query: str, k: int = 5, user_id: Optional[str] = None) -> list[dict]:
    """Semantic search over page summaries. Returns date + summary + relevance."""
    if not (query or "").strip():
        return []
    return await asyncio.to_thread(_search_chroma_sync, query, k, user_id)


async def finalize_page(date: str, user_id: Optional[str] = None) -> None:
    """Flip a draft page to final + upsert into Chroma. Called at midnight."""
    def _sync():
        page = _read_page_sync(date, user_id)
        if not page:
            return
        if page.get("status") == "final":
            return
        page["status"] = "final"
        _write_page_sync(page, user_id)
        _upsert_chroma_sync(page, user_id)
        log.info("diary: finalized %s (activities=%d)", date, page.get("activity_count", 0))
    await asyncio.to_thread(_sync)


async def delete_page(date: str, user_id: Optional[str] = None) -> bool:
    return await asyncio.to_thread(_delete_page_sync, date, user_id)


def _delete_all_pages_sync(user_id: Optional[str] = None) -> int:
    """Delete every diary page for a user (JSON files + Chroma entries).

    Returns the number of dates deleted. Used by the "Clear Memory: All" path.
    """
    dates = _list_dates_sync(user_id)
    n = 0
    for d in dates:
        try:
            if _delete_page_sync(d, user_id):
                n += 1
        except Exception as exc:
            log.warning("delete_all: failed on %s: %s", d, exc)
    log.info("diary: delete_all_pages user_id=%s removed=%d", user_id, n)
    return n


async def delete_all_pages(user_id: Optional[str] = None) -> int:
    """Async wrapper around _delete_all_pages_sync."""
    return await asyncio.to_thread(_delete_all_pages_sync, user_id)


async def list_dates(user_id: Optional[str] = None) -> list[str]:
    return await asyncio.to_thread(_list_dates_sync, user_id)


def today_key(tz_name: Optional[str] = None) -> str:
    """Return today's date string (YYYY-MM-DD) in the given timezone, or
    system-local if tz_name is empty/invalid."""
    from datetime import datetime
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name)).date().isoformat()
        except Exception:
            pass
    return _date.today().isoformat()
