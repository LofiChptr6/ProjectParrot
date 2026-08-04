"""Disk persistence for short-term conversation history + its rolling summary.

The audit's "forgetful" finding had two structural causes: the 22-entry
history lived only in RAM (a bridge restart hard-reset Mocha mid-relationship),
and everything older than the window vanished without a trace until mem0's
fact extraction happened to keep a fragment. This module fixes the first cause
and stores the artifact that fixes the second (the rolling summary composed in
``server._summarize_history_span``).

One small JSON file per history bucket under ``data/history/``:

    {"summary": "<condensed earlier conversation>", "entries": [{role, content}, …]}

Files are a few KB (the in-RAM window is bounded), so whole-file atomic
rewrite per append is cheaper than being clever. Writes are serialized by a
process-wide lock and always run off the event loop (``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger("bridge.history_store")

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "history"

_WRITE_LOCK = threading.Lock()


def _path_for(uid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", uid or "anonymous")
    return HISTORY_DIR / f"{safe}.json"


def load_sync(uid: str) -> tuple[list[dict], str]:
    """Read (entries, summary) for a bucket. Missing/corrupt file → ([], "")."""
    p = _path_for(uid)
    if not p.exists():
        return [], ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = [e for e in (data.get("entries") or [])
                   if isinstance(e, dict) and e.get("role") and e.get("content")]
        summary = str(data.get("summary") or "")
        return entries, summary
    except Exception as exc:  # noqa: BLE001 — a broken file must not block startup
        log.warning("history load failed for %s: %s", uid, exc)
        return [], ""


def _persist_sync(uid: str, entries: list[dict], summary: str) -> None:
    p = _path_for(uid)
    tmp = p.with_suffix(".json.tmp")
    payload = json.dumps({"summary": summary or "", "entries": entries},
                         ensure_ascii=False)
    with _WRITE_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(p)


def schedule_persist(uid: str, entries: list[dict], summary: str) -> None:
    """Fire-and-forget durable write. Snapshots the list NOW (the caller keeps
    mutating it) and runs the file IO in a thread. Fail-soft."""
    snapshot = [dict(e) for e in entries]

    async def _run() -> None:
        try:
            await asyncio.to_thread(_persist_sync, uid, snapshot, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("history persist failed for %s: %s", uid, exc)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # No loop (import-time / tests): write inline — still small and rare.
        try:
            _persist_sync(uid, snapshot, summary)
        except Exception as exc:  # noqa: BLE001
            log.warning("history persist failed for %s: %s", uid, exc)
