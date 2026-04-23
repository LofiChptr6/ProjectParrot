"""
Notification queue — append-only JSONL store for messages that should be
delivered to the user when a surface (web UI, Telegram) becomes available.

Written by cron jobs (e.g. Nori research findings) and by the autonomy engine
when both web and Telegram are offline. Consumed by the channel router on
``client_hello`` and by the autonomy reconnect-hello composer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("notifications")

ROOT = Path(__file__).resolve().parent.parent
_PATH = ROOT / "data" / "notifications.jsonl"
_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def enqueue(item: dict[str, Any]) -> str:
    """Append a notification. Returns the assigned id."""
    item = dict(item)
    item.setdefault("id", f"nf_{uuid.uuid4().hex[:10]}")
    item.setdefault("ts", _now_iso())
    item.setdefault("delivered", False)
    line = json.dumps(item, ensure_ascii=False)

    async with _lock:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log.info("Enqueued %s: %s", item["id"], str(item.get("summary", ""))[:120])
    return item["id"]


def _read_all_sync() -> list[dict]:
    if not _PATH.exists():
        return []
    out: list[dict] = []
    for line in _PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("Skipping malformed notification line: %s", line[:200])
    return out


async def list_undelivered() -> list[dict]:
    async with _lock:
        items = _read_all_sync()
    return [it for it in items if not it.get("delivered")]


async def mark_delivered(ids: list[str]) -> None:
    if not ids:
        return
    wanted = set(ids)
    async with _lock:
        items = _read_all_sync()
        for it in items:
            if it.get("id") in wanted:
                it["delivered"] = True
                it["delivered_at"] = _now_iso()
        _PATH.write_text(
            "\n".join(json.dumps(it, ensure_ascii=False) for it in items) + ("\n" if items else ""),
            encoding="utf-8",
        )
    log.info("Marked delivered: %s", ", ".join(sorted(wanted)))


async def prune(max_retained_delivered: int = 200) -> int:
    """Trim the file so no more than ``max_retained_delivered`` delivered entries remain.
    Keeps all undelivered entries. Returns number of entries removed."""
    async with _lock:
        items = _read_all_sync()
        undelivered = [it for it in items if not it.get("delivered")]
        delivered = [it for it in items if it.get("delivered")]
        if len(delivered) <= max_retained_delivered:
            return 0
        delivered.sort(key=lambda it: it.get("delivered_at", it.get("ts", "")))
        trimmed = delivered[-max_retained_delivered:]
        keep = undelivered + trimmed
        keep.sort(key=lambda it: it.get("ts", ""))
        _PATH.write_text(
            "\n".join(json.dumps(it, ensure_ascii=False) for it in keep) + ("\n" if keep else ""),
            encoding="utf-8",
        )
        removed = len(items) - len(keep)
    log.info("Pruned %d delivered notifications", removed)
    return removed
