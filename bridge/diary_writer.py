"""
Diary writer — composes Mocha's diary page from the day's tool activity.

Writes are triggered from three places (see ``bridge/server.py``):
  * ``tick()``       — called from the idle heartbeat loop every 5s; gated by
                       idle timeout + min-interval so we don't spam writes
  * ``on_page_hidden()`` — called when the frontend tab goes hidden / is closing
  * ``finalize_today()`` — called by the midnight sweep (or manually); flips
                       the draft to ``status='final'`` and embeds into Chroma

The LLM pass runs on the bridge's shared `llm_client`, logged under
``triggered_by='diary_writer'`` so Shiro and our observability tooling can
distinguish it from normal Mocha turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from bridge import session_scratchpad
from memory import diary_store

log = logging.getLogger("diary")

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config.yaml"


def _cfg() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("diary", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
#  In-process state
# ---------------------------------------------------------------------------

# Last time we actually wrote a draft (monotonic seconds). Used by tick() to
# avoid writing more often than min_interval even if the heartbeat fires every
# 5s.
_last_write_monotonic: float = 0.0

# Count of user-turn interactions today (incremented by bridge hook).
_interaction_count_today: int = 0
_interaction_day_key: str = ""

# Serialize writes: page_hidden fires once PER CONNECTED TAB, so three open
# tabs used to produce three concurrent LLM writes of the same page (each
# confabulating a different day). One writer at a time, and a min-interval
# re-check inside the lock covers every trigger path.
_write_lock = asyncio.Lock()

# Tools that fire autonomously in the background (cache refreshes, polling) —
# they're not "things Mocha and Ika did" and don't justify an LLM diary pass
# on their own. A day of only these gets a truthful template page instead.
_ROUTINE_TOOLS = {"get_positions", "polygon_market_status"}


def note_interaction(tz_name: Optional[str]) -> None:
    """Called from the bridge on every user turn (end-of-turn). Just bumps a
    counter that's included in the diary page metadata."""
    global _interaction_count_today, _interaction_day_key
    today = diary_store.today_key(tz_name)
    if today != _interaction_day_key:
        _interaction_day_key = today
        _interaction_count_today = 0
    _interaction_count_today += 1


# ---------------------------------------------------------------------------
#  Helpers for building the LLM prompt
# ---------------------------------------------------------------------------

def _tz_name() -> Optional[str]:
    try:
        from bridge.channel_router import load_primary_user
        primary = load_primary_user() or {}
        return primary.get("timezone")
    except Exception:
        return None


def _local_iso_now(tz_name: Optional[str]) -> str:
    try:
        if tz_name:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name)).isoformat(timespec="seconds")
    except Exception:
        pass
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _format_activities_for_prompt(entries: list[dict], tz_name: Optional[str]) -> str:
    """Render scratchpad entries as a compact bullet list for the writer LLM."""
    if not entries:
        return "(no tool activity this session)"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else None
    except Exception:
        tz = None
    lines = []
    for e in entries:
        ts = e.get("ts") or 0
        if tz:
            t = datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")
        else:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"- {t}  {e.get('tool_name')}({e.get('args_preview','')}) → {e.get('result_preview','')}")
    return "\n".join(lines)


def _build_structured_activities(entries: list[dict], tz_name: Optional[str]) -> list[dict]:
    """Keep the structured list in the JSON so the diary modal can expand it."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else None
    except Exception:
        tz = None
    out: list[dict] = []
    for e in entries:
        ts = e.get("ts") or 0
        if tz:
            t = datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")
        else:
            t = datetime.fromtimestamp(ts).strftime("%H:%M")
        out.append({
            "t": t,
            "tool": e.get("tool_name"),
            "brief": f"{e.get('args_preview','')} → {e.get('result_preview','')}".strip(" →"),
        })
    return out


_DIARY_SYSTEM_PROMPT = """\
You are **Mocha**, writing a private diary entry about today. You are NOT talking
to anyone right now — this is your own journal. The reader is you, tomorrow.

Your voice:
- First-person, casual, warm. "Ika" is the person you hang out with — refer to
  them by name.
- Short. One short paragraph (3–6 sentences) is the target. No walls of text.
- Notice small things more than you narrate events. A diary is about what
  *felt* like it mattered, not a transcript.
- No markdown headers, no lists — just one plain paragraph of prose.
- Keep it true. If the day had only one small thing, write one small thing.
  Never invent activities that aren't in the tool log.
- You live on a screen on Ika's trading dashboard. You have NO body and NO
  apartment: never write physical scenes — no rooftops, sunsets, couches,
  cookies, guitars, visits, phone calls. If the log is just routine desk
  checks, say exactly that in one dry sentence ("quiet one — I mostly watched
  the desk breathe") rather than romanticizing. A boring true page beats a
  lovely invented one, every time.

Output format (strict JSON, nothing outside):
{
  "summary": "<one paragraph, 3-6 sentences, in Mocha's voice>",
  "highlights": ["<1 to 3 short phrases, good memories or notable moments>"],
  "themes": ["<1 to 4 one-word tags>"]
}

No tags like <emotion> or <gesture> in the summary — this is text for reading,
not speech. Do not mention the JSON format, Nori, tools, or "today" in a way
that sounds like a log. Just write.
"""


async def _compose_diary_via_llm(
    activities_block: str,
    previous_summary: Optional[str],
    local_time_iso: str,
) -> Optional[dict]:
    """Call the LLM once to produce {summary, highlights, themes}.

    Returns ``None`` if the call failed or the JSON didn't parse — the caller
    falls back to a minimal structural summary so the page is never empty.
    """
    try:
        from bridge.server import llm_client
        from bridge.call_log import log_call, CallContext
    except Exception as exc:
        log.warning("diary: llm_client not available: %s", exc)
        return None

    user_msg = (
        f"Today is {local_time_iso}. Here's the activity log of tools you ran "
        f"(with timestamps, args, and a preview of each result):\n\n"
        f"{activities_block}\n\n"
    )
    if previous_summary:
        user_msg += (
            "For reference, here's yesterday's diary entry so your voice stays "
            f"consistent — don't copy it, just match the rhythm:\n\n"
            f"{previous_summary}\n\n"
        )
    user_msg += "Write today's entry now, as strict JSON."

    messages = [
        {"role": "system", "content": _DIARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    t0 = time.monotonic()
    try:
        result = await llm_client.chat(
            messages,
            max_tokens=600,
            enable_thinking=False,
        )
    except Exception as exc:
        log.warning("diary: LLM call failed: %s", exc)
        return None
    latency_ms = (time.monotonic() - t0) * 1000

    asyncio.create_task(log_call(
        CallContext(triggered_by="diary_writer"),
        model=llm_client.model,
        temperature=llm_client.default_temperature,
        max_tokens=600, stream=False, tools_provided=False,
        messages=messages,
        response_content=result.get("content"),
        finish_reason=result.get("finish_reason"),
        error=result.get("_error"),
        latency_ms=latency_ms,
        prompt_tokens=(result.get("usage") or {}).get("prompt_tokens"),
        completion_tokens=(result.get("usage") or {}).get("completion_tokens"),
        total_tokens=(result.get("usage") or {}).get("total_tokens"),
    ))

    raw = (result.get("content") or "").strip()
    # Strip code fences if the model added them
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()
    try:
        parsed = json.loads(raw)
    except Exception:
        # Last resort: try to find an object in the text
        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(raw[first : last + 1])
            except Exception:
                log.warning("diary: unparseable JSON from LLM: %s", raw[:200])
                return None
        else:
            log.warning("diary: no JSON in LLM output: %s", raw[:200])
            return None

    if not isinstance(parsed, dict):
        return None
    # Clean up fields
    summary = str(parsed.get("summary") or "").strip()
    if not summary:
        return None
    highlights = parsed.get("highlights") or []
    if not isinstance(highlights, list):
        highlights = []
    highlights = [str(h).strip() for h in highlights if str(h).strip()][:3]
    themes = parsed.get("themes") or []
    if not isinstance(themes, list):
        themes = []
    themes = [str(t).strip() for t in themes if str(t).strip()][:4]
    return {"summary": summary, "highlights": highlights, "themes": themes}


# ---------------------------------------------------------------------------
#  Public triggers
# ---------------------------------------------------------------------------

async def write_draft(reason: str = "manual") -> Optional[str]:
    """Write / overwrite today's draft page from the current scratchpad.

    Returns the date that was written, or ``None`` if skipped (empty scratchpad
    *and* no existing page to refresh).
    """
    async with _write_lock:
        return await _write_draft_locked(reason)


async def _write_draft_locked(reason: str) -> Optional[str]:
    global _last_write_monotonic
    # Re-check the min interval INSIDE the lock: concurrent triggers (one
    # page_hidden per open tab) queue on the lock, and all but the first
    # become no-ops here instead of duplicate LLM writes.
    min_interval_s = float(_cfg().get("min_interval_minutes", 10)) * 60
    if reason != "manual" and (time.monotonic() - _last_write_monotonic) < min_interval_s:
        return None
    entries = session_scratchpad.all_entries()
    if not entries:
        # Even if the scratchpad is empty, we still want to update the
        # interaction_count on an existing draft if one exists. But if nothing
        # exists and there's nothing to write, skip.
        tz_name = _tz_name()
        today = diary_store.today_key(tz_name)
        existing = await diary_store.get_page(today)
        if not existing:
            return None

    tz_name = _tz_name()
    today = diary_store.today_key(tz_name)
    local_now = _local_iso_now(tz_name)

    # Yesterday's summary as style reference (if any)
    previous: Optional[str] = None
    try:
        prior_dates = await diary_store.list_dates()
        for d in prior_dates:
            if d == today:
                continue
            p = await diary_store.get_page(d)
            if p and p.get("summary"):
                previous = p["summary"]
                break
    except Exception:
        previous = None

    activities_block = _format_activities_for_prompt(entries, tz_name)
    structured = _build_structured_activities(entries, tz_name)

    # Grounding gate: background polling (held-tickers refresh etc.) is not a
    # day worth narrating. With nothing meaningful to write about, the LLM used
    # to fill the vacuum with fiction (rooftop sunsets, baked cookies) — which
    # then fed back into her context as false shared memories. Write a plain,
    # truthful template page instead and skip the LLM entirely.
    meaningful = [e for e in entries if (e.get("tool_name") or "") not in _ROUTINE_TOOLS]
    if not meaningful:
        if _interaction_count_today > 0:
            summary = ("Quiet one. Ika and I traded a few words, nothing that "
                       "needed writing down; the desk mostly just breathed.")
        else:
            summary = ("Nothing to report — I watched the desk tick over and "
                       "kept my own company. Some days are just that.")
        composed = {"summary": summary, "highlights": [], "themes": ["quiet"]}
    else:
        composed = await _compose_diary_via_llm(activities_block, previous, local_now)
    if not composed:
        # Fallback — a minimal page that at least captures the activities.
        # Prevents empty pages when the LLM hiccups.
        composed = {
            "summary": "(Mocha was too tired to write tonight.)",
            "highlights": [],
            "themes": [],
        }

    page = {
        "date": today,
        "tz": tz_name or "",
        "status": "draft",
        "written_at": local_now,
        "summary": composed["summary"],
        "highlights": composed["highlights"],
        "themes": composed["themes"],
        "activities": structured,
        "activity_count": len(structured),
        "interaction_count": _interaction_count_today,
    }
    await diary_store.save_page(page)
    _last_write_monotonic = time.monotonic()
    log.info("diary: wrote %s draft (reason=%s activities=%d)",
             today, reason, len(structured))
    return today


async def tick(now_monotonic: Optional[float] = None,
               last_interaction_monotonic: Optional[float] = None) -> None:
    """Heartbeat entrypoint — called every 5s from ``_idle_heartbeat_loop``.

    Writes a draft only if:
      - the diary feature is enabled
      - the user has actually interacted at some point
      - it's been at least `idle_threshold_minutes` since their last interaction
      - it's been at least `min_interval_minutes` since the last draft write
      - the scratchpad has at least one entry, OR an existing page for today
        would benefit from a refreshed interaction_count
    """
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return

    idle_thresh_s = float(cfg.get("idle_threshold_minutes", 10)) * 60
    min_interval_s = float(cfg.get("min_interval_minutes", 10)) * 60

    now = now_monotonic if now_monotonic is not None else time.monotonic()

    if last_interaction_monotonic is None or last_interaction_monotonic <= 0:
        return
    idle_for = now - last_interaction_monotonic
    if idle_for < idle_thresh_s:
        return
    since_last_write = now - _last_write_monotonic
    if since_last_write < min_interval_s:
        return
    if session_scratchpad.size() == 0:
        return

    try:
        await write_draft(reason="idle_tick")
    except Exception as exc:
        log.warning("diary: idle tick write failed: %s", exc)


async def on_page_hidden() -> None:
    """Triggered from the WS ``page_hidden`` message. Write an immediate
    draft; these are the "nice moments to capture the day" events."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return
    if session_scratchpad.size() == 0:
        return
    try:
        await write_draft(reason="page_hidden")
    except Exception as exc:
        log.warning("diary: page_hidden write failed: %s", exc)


async def finalize_today() -> None:
    """Finalize today's draft and clear the scratchpad — called at midnight
    local-tz. If there's no draft, creates a terse one from whatever is in
    the scratchpad so the day always gets a page."""
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return
    tz_name = _tz_name()
    today = diary_store.today_key(tz_name)
    # Make sure a draft exists
    if session_scratchpad.size() > 0 or not await diary_store.get_page(today):
        await write_draft(reason="midnight")
    await diary_store.finalize_page(today)
    session_scratchpad.clear()
    log.info("diary: finalized %s at local midnight", today)
