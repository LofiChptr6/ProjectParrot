"""
In-process mem0 memory store — replaces the old Chroma HTTP service.

Responsibilities:
  - Build a mem0 `Memory` instance from config.yaml on first use.
  - Provide an async façade that the bridge uses (add/search/recent/clear).
  - Keep every call non-blocking: mem0 runs an LLM pass on `add()` for fact
    extraction; we offload it to a thread so the event loop (and TTFT) is
    never held up.
  - Attach time/tz/location metadata to every add() from primary_user.json.

Config shape (see config.yaml.memory):
  backend: mem0
  chroma_path: memory/mem0_chroma
  history_db: memory/mem0_history.sqlite
  embedding_model: all-MiniLM-L6-v2
  max_results: 5
  short_term_limit: 22
  mem0_user_id: primary
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger("memory.mem0_store")

ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = ROOT / "config.yaml"

_memory_instance: Any = None  # mem0.Memory — typed lazily to avoid import at module load


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_mem0_config() -> dict:
    cfg_all = _load_config()
    mem_cfg = cfg_all.get("memory", {}) or {}
    llm_cfg = cfg_all.get("llm", {}) or {}

    chroma_path = str(ROOT / mem_cfg.get("chroma_path", "memory/mem0_chroma"))
    history_db = str(ROOT / mem_cfg.get("history_db", "memory/mem0_history.sqlite"))
    embed_model = mem_cfg.get("embedding_model", "all-MiniLM-L6-v2")
    collection = mem_cfg.get("collection", "parrot_mem0")

    # vLLM is OpenAI-API-compatible; mem0 uses the openai provider against any
    # base_url. Low temp/ceiling so fact-extraction is deterministic + cheap.
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": llm_cfg.get("model", "Qwen/Qwen3-32B"),
                "openai_base_url": llm_cfg.get("base_url", "http://127.0.0.1:8800/v1"),
                "api_key": os.environ.get("VLLM_API_KEY", "EMPTY"),
                "temperature": 0.2,
                "max_tokens": 1024,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": embed_model,
            },
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": collection,
                "path": chroma_path,
            },
        },
        "history_db_path": history_db,
    }


def get_store() -> Any:
    """Lazily build and cache the mem0 Memory singleton."""
    global _memory_instance
    if _memory_instance is None:
        from mem0 import Memory
        cfg = _build_mem0_config()
        # Ensure parent dirs exist (mem0 will create the chroma dir itself,
        # but the sqlite path's parent must exist).
        Path(cfg["history_db_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg["vector_store"]["config"]["path"]).mkdir(parents=True, exist_ok=True)
        _memory_instance = Memory.from_config(cfg)
        log.info("mem0 store initialized (chroma=%s history=%s)",
                 cfg["vector_store"]["config"]["path"], cfg["history_db_path"])
    return _memory_instance


def _default_user_id() -> str:
    return _load_config().get("memory", {}).get("mem0_user_id", "primary")


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_local(tz_name: Optional[str]) -> str:
    try:
        if tz_name:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name)).isoformat()
    except Exception:
        pass
    return datetime.now().astimezone().isoformat()


def _build_metadata(role: str, extra: Optional[dict] = None) -> dict:
    """Compose per-memory metadata, including user tz/location snapshot."""
    try:
        from bridge.channel_router import load_primary_user
        primary = load_primary_user() or {}
    except Exception:
        primary = {}

    meta: dict = {
        "timestamp": time.time(),
        "iso_utc": _iso_utc(),
        "iso_local": _iso_local(primary.get("timezone")),
        "timezone": primary.get("timezone") or "UTC",
        "role": role,
    }
    if primary.get("lat") is not None and primary.get("lng") is not None:
        meta["lat"] = primary["lat"]
        meta["lng"] = primary["lng"]
    if primary.get("locale"):
        meta["locale"] = primary["locale"]
    if extra:
        meta.update(extra)
    return meta


# ---------------------------------------------------------------------------
#  Public async API
# ---------------------------------------------------------------------------

async def add(
    text: str,
    role: str = "user",
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    infer: bool = True,
    fire_and_forget: bool = True,
) -> None:
    """Store a user/assistant utterance.

    `fire_and_forget=True` (default) schedules the mem0 call on a thread and
    returns immediately. mem0's add() runs an LLM fact-extraction pass which
    would otherwise block the event loop and hurt TTFT.
    """
    if not text or not text.strip():
        return

    uid = user_id or _default_user_id()
    meta = _build_metadata(role, metadata)

    def _sync_add() -> None:
        try:
            store = get_store()
            msg = [{"role": role if role in ("user", "assistant") else "user", "content": text}]
            store.add(msg, user_id=uid, metadata=meta, infer=infer)
        except Exception as exc:
            log.warning("mem0 add failed: %s", exc)

    if fire_and_forget:
        asyncio.create_task(asyncio.to_thread(_sync_add))
    else:
        await asyncio.to_thread(_sync_add)


async def search(
    query: str,
    user_id: Optional[str] = None,
    k: int = 5,
    filters: Optional[dict] = None,
) -> list[dict]:
    """Semantic search. Returns list of {text, role, timestamp, relevance, metadata}.

    ``filters`` merges extra metadata-equality constraints on top of the user_id
    partition — e.g. ``filters={"kind": "shared_news"}`` to retrieve only news
    items Mocha has shared (mem0 2.x accepts arbitrary metadata filters)."""
    if not query or not query.strip():
        return []
    uid = user_id or _default_user_id()
    flt = {"user_id": uid}
    if filters:
        flt.update(filters)

    def _sync_search() -> list[dict]:
        try:
            store = get_store()
            raw = store.search(query=query, top_k=k, filters=flt)
            if isinstance(raw, dict):
                return raw.get("results", []) or []
            return raw or []
        except Exception as exc:
            log.warning("mem0 search failed: %s", exc)
            return []

    results = await asyncio.to_thread(_sync_search)
    out: list[dict] = []
    for r in results:
        meta = r.get("metadata") or {}
        out.append({
            "text": r.get("memory") or r.get("text") or "",
            "role": meta.get("role", "memory"),
            "timestamp": float(meta.get("timestamp", 0) or 0),
            "relevance": round(float(r.get("score") or 0.0), 4),
            "metadata": meta,
        })
    return out


async def recent(user_id: Optional[str] = None, n: int = 10) -> list[dict]:
    """Return the N most recent memories (by timestamp metadata)."""
    uid = user_id or _default_user_id()

    def _sync_recent() -> list[dict]:
        try:
            store = get_store()
            raw = store.get_all(filters={"user_id": uid}, top_k=max(n * 3, 30))
            if isinstance(raw, dict):
                return raw.get("results", []) or []
            return raw or []
        except Exception as exc:
            log.warning("mem0 recent failed: %s", exc)
            return []

    results = await asyncio.to_thread(_sync_recent)
    out: list[dict] = []
    for r in results:
        meta = r.get("metadata") or {}
        out.append({
            "text": r.get("memory") or r.get("text") or "",
            "role": meta.get("role", "memory"),
            "timestamp": float(meta.get("timestamp", 0) or 0),
        })
    out.sort(key=lambda m: m["timestamp"], reverse=True)
    return out[:n]


async def clear(user_id: Optional[str] = None) -> None:
    """Wipe memories for a user (default: configured primary user)."""
    uid = user_id or _default_user_id()

    def _sync_clear() -> None:
        try:
            store = get_store()
            store.delete_all(user_id=uid)
            log.info("mem0 cleared for user_id=%s", uid)
        except Exception as exc:
            log.warning("mem0 clear failed: %s", exc)

    await asyncio.to_thread(_sync_clear)


async def count(user_id: Optional[str] = None) -> int:
    """Best-effort count for status endpoints."""
    uid = user_id or _default_user_id()

    def _sync_count() -> int:
        try:
            store = get_store()
            raw = store.get_all(filters={"user_id": uid}, top_k=10000)
            if isinstance(raw, dict):
                return len(raw.get("results", []) or [])
            return len(raw or [])
        except Exception as exc:
            log.warning("mem0 count failed: %s", exc)
            return 0

    return await asyncio.to_thread(_sync_count)
