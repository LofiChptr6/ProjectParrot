"""Life-context ledger — the running answer to "what's going on in Ika's life".

A small set of THREADS (ongoing situations: a training run hogging the GPU, a
site launch, a trip, a deadline), persisted to data/life_context.json and
maintained by a nightly LLM merge run after the diary page finalizes. This is
the layer between mem0 facts (timeless preferences) and the diary (one day's
events): it holds what's *currently in motion*, so Mocha's awareness of his
life survives restarts and doesn't depend on similarity search getting lucky.

Injected into both system prompts as a compact background block. The block
itself carries the anti-recitation rule: awareness shows up as timing and
follow-ups, never as reading the list back.

Shape on disk:
    {"updated": iso, "threads": [
        {"id": "gpu-training", "title": "training run on the home box",
         "status": "active",           # active | waiting | done
         "note": "GPU held since 07-30; mocha voice lanes paused",
         "updated": iso}]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("memory.life_context")

ROOT = Path(__file__).resolve().parent.parent
_PATH = ROOT / "data" / "life_context.json"
_MAX_THREADS = 8          # the block must stay small — prompt budget
_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("threads"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("life_context: unreadable (%s) — starting empty", exc)
    return {"updated": None, "threads": []}


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


def _age_str(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        days = max(0, (datetime.now(timezone.utc) - dt).days)
        return "today" if days == 0 else (f"{days}d ago")
    except Exception:
        return ""


def format_block() -> Optional[str]:
    """Compact prompt block of live threads, or None when there's nothing.

    Deliberately terse — one line per thread — and framed as background so the
    model treats it as awareness, not content to recite."""
    threads = [t for t in load().get("threads", [])
               if t.get("status") in ("active", "waiting")][:_MAX_THREADS]
    if not threads:
        return None
    lines = []
    for t in threads:
        age = _age_str(t.get("updated"))
        note = (t.get("note") or "").strip()
        line = f"- {t.get('title', '?')}"
        if note:
            line += f" — {note}"
        if age:
            line += f" ({age})"
        lines.append(line)
    return (
        "[Ika's life right now — background awareness, maintained nightly. "
        "This is for TIMING and follow-ups (nudge before a deadline, don't ping "
        "him mid-crunch, ask how a thing went AFTER it happened). Never recite "
        "this list or prove you know it; if a thread is stale he'll tell you.]\n"
        + "\n".join(lines)
    )


_MERGE_SYSTEM = """You maintain a tiny ledger of what's going on in Ika's life — ongoing THREADS
(projects, situations, commitments, trips, deadlines), not one-off events and
not personality facts. You receive the current ledger and today's diary page.
Return STRICT JSON: {"threads": [{"id": str, "title": str, "status":
"active"|"waiting"|"done", "note": str}]}.

Rules:
- Keep at most 8 threads, most alive first. Merge duplicates. Drop stale
  threads (no signal in ~2 weeks) and anything status=done.
- "note" is ONE short clause of current state ("GPU held since 07-30",
  "waiting on scorer data ~08-21"). No prose paragraphs.
- Only include what the diary/ledger supports. Never invent.
- Titles are stable handles — keep an existing thread's id/title when
  updating it."""


async def update_from_diary(diary_page: dict, llm_client: Any) -> None:
    """Nightly merge: today's finalized diary page + current ledger → new ledger.
    Fail-silent: on any error the old ledger stands."""
    try:
        cur = load()
        page_summary = (diary_page or {}).get("summary") or ""
        highlights = (diary_page or {}).get("highlights") or []
        if not page_summary and not highlights:
            return
        user_msg = (
            "Current ledger:\n" + json.dumps(cur.get("threads", []),
                                             ensure_ascii=False, indent=1)
            + "\n\nToday's diary page:\nSummary: " + page_summary
            + ("\nHighlights: " + "; ".join(str(h) for h in highlights)
               if highlights else "")
            + "\n\nReturn the updated ledger JSON now."
        )
        res = await llm_client.chat(
            [{"role": "system", "content": _MERGE_SYSTEM},
             {"role": "user", "content": user_msg}],
            temperature=0.2, max_tokens=700, enable_thinking=False,
        )
        content = (res.get("content") or "").strip()
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            log.warning("life_context: merge returned no JSON — keeping old ledger")
            return
        data = json.loads(content[start:end + 1])
        threads = data.get("threads")
        if not isinstance(threads, list):
            return
        now = _now_iso()
        cleaned = []
        prev = {t.get("id"): t for t in cur.get("threads", [])}
        for t in threads[:_MAX_THREADS]:
            if not isinstance(t, dict) or not (t.get("title") or "").strip():
                continue
            tid = (t.get("id") or t["title"]).strip().lower().replace(" ", "-")[:48]
            status = t.get("status") if t.get("status") in ("active", "waiting", "done") else "active"
            if status == "done":
                continue
            old = prev.get(tid) or {}
            changed = (old.get("note") != t.get("note")
                       or old.get("status") != status
                       or old.get("title") != t.get("title"))
            cleaned.append({
                "id": tid,
                "title": str(t.get("title"))[:80],
                "status": status,
                "note": str(t.get("note") or "")[:160],
                "updated": now if (changed or not old) else old.get("updated", now),
            })
        async with _LOCK:
            _save({"updated": now, "threads": cleaned})
        log.info("life_context: ledger updated — %d live threads", len(cleaned))
    except Exception as exc:  # noqa: BLE001 — nightly maintenance must never crash the bridge
        log.warning("life_context: update failed (%s) — old ledger stands", exc)
