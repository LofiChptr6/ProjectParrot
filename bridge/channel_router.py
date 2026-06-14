"""
Channel router — decides where a proactive message should land.

Priority:
  1. Web UI (any ``/ws/live`` client open) → full TTS + gesture + viseme speech_segment
  2. Telegram (if the primary user's chat_id has been learned) → text DM
  3. Fall back to the notifications queue → delivered on next ``client_hello``

Called from the autonomy engine and from scheduled cron jobs.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("channel_router")

ROOT = Path(__file__).resolve().parent.parent
_PRIMARY_USER_PATH = ROOT / "data" / "primary_user.json"


# ---------------------------------------------------------------------------
#  Primary user (Telegram chat_id) — learned from first POST /channel
# ---------------------------------------------------------------------------

def load_primary_user() -> Optional[dict]:
    if not _PRIMARY_USER_PATH.exists():
        return None
    try:
        return json.loads(_PRIMARY_USER_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read primary_user.json: %s", exc)
        return None


def save_primary_user(source: str, user_id: str, app_user_id: str | None = None) -> None:
    if not user_id or user_id == "agent":
        return
    _PRIMARY_USER_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = load_primary_user() or {}
    data.setdefault("first_seen_at", _iso_now())
    if source == "telegram":
        changed = (
            data.get("telegram_user_id") != user_id
            or (app_user_id and data.get("telegram_app_user_id") != app_user_id)
        )
        if not changed:
            return
        data["telegram_user_id"] = user_id
        if app_user_id:
            data["telegram_app_user_id"] = app_user_id
    else:
        if data.get(f"{source}_user_id") == user_id:
            return
        data[f"{source}_user_id"] = user_id
    data["updated_at"] = _iso_now()
    _PRIMARY_USER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Primary user updated: source=%s user_id=%s app_user_id=%s", source, user_id, app_user_id)


def save_primary_user_context(
    tz: Optional[str] = None,
    locale: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> bool:
    """Persist the web client's timezone / locale / optional coords into
    primary_user.json. Debounced: skips writes when values haven't changed.
    Returns True when a write happened."""
    _PRIMARY_USER_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = load_primary_user() or {}
    changed = False

    if tz:
        # Validate tz string; silently drop if unknown to this Python.
        try:
            from zoneinfo import ZoneInfo, available_timezones
            if tz in available_timezones():
                if data.get("timezone") != tz:
                    data["timezone"] = tz
                    changed = True
            else:
                # Still try to construct — some systems know more than the
                # static list. If it raises, we drop.
                try:
                    ZoneInfo(tz)
                    if data.get("timezone") != tz:
                        data["timezone"] = tz
                        changed = True
                except Exception:
                    log.warning("Dropping unknown timezone: %s", tz)
        except Exception:
            pass

    if locale and data.get("locale") != locale:
        data["locale"] = locale
        changed = True

    if lat is not None and lng is not None:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
            if (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0 and
                    (abs(data.get("lat", 0.0) - lat_f) > 1e-4 or
                     abs(data.get("lng", 0.0) - lng_f) > 1e-4)):
                data["lat"] = lat_f
                data["lng"] = lng_f
                changed = True
        except (TypeError, ValueError):
            pass

    if not changed:
        return False

    data.setdefault("first_seen_at", _iso_now())
    data["context_updated_at"] = _iso_now()
    data["updated_at"] = _iso_now()
    _PRIMARY_USER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Primary user context updated: tz=%s locale=%s geo=%s",
             data.get("timezone"), data.get("locale"),
             "yes" if "lat" in data else "no")
    return True


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
#  Delivery paths
# ---------------------------------------------------------------------------

async def _deliver_to_web(payload: dict) -> None:
    """Assemble a speech_segment (TTS + gesture) and broadcast to _ws_clients."""
    from bridge import server as _srv  # lazy to avoid circular import at module load

    text = payload["text"]
    emotion = payload.get("emotion", "neutral")
    action_hint = payload.get("action") or payload.get("gesture") or "stand calmly"

    clip_name = await _srv._resolve_action(action_hint, emotion)
    audio = await _srv._synthesize(text)
    viseme_data = await _srv._generate_visemes(audio, text) if audio else None

    job_id = _srv._new_job_id()
    message = {
        "type": "speech_segment",
        "job_id": job_id,
        "index": 0,
        "total": 1,
        "text": text,
        "emotion": emotion,
        "gesture": clip_name,
        "autonomous": True,
        "source": payload.get("source", "autonomy"),
    }
    if audio:
        message["audio_base64"] = base64.b64encode(audio).decode()
    if viseme_data:
        message["viseme_b64"] = viseme_data.get("viseme_b64")
        message["viseme_fps"] = viseme_data.get("viseme_fps")
        message["viseme_frames"] = viseme_data.get("viseme_frames")

    await _srv._broadcast_clients(message)
    # speech_end, so clients can tear down dedup buffers
    await _srv._broadcast_clients({"type": "speech_end", "job_id": job_id, "autonomous": True})


async def _deliver_to_telegram(text: str, tg_chat_id: str, app_user_id: str | None = None) -> bool:
    from channels.base import registry
    # Prefer the per-user bot; fall back to the legacy global bot.
    tg = (registry.get_user_bot(app_user_id) if app_user_id else None) or registry.get("telegram")
    if not tg:
        return False
    try:
        await tg.send(text, user_id=tg_chat_id)
        return True
    except Exception as exc:
        log.warning("Telegram send failed (chat_id=%s): %s", tg_chat_id, exc)
        return False


# ---------------------------------------------------------------------------
#  Public entry point
# ---------------------------------------------------------------------------

async def route_autonomous_for_user(app_user_id: str, payload: dict[str, Any]) -> str:
    """Like route_autonomous but targets a specific project mocha user.

    Delivery priority:
      1. Web UI (all connected clients — not yet per-user tagged)
      2. User's own Telegram bot + their stored telegram_chat_id
      3. Notification queue (picked up on next web reconnect)
    """
    cron_origin = bool(payload.pop("cron_origin", False))
    text = (payload.get("text") or "").strip()
    if not text:
        return "empty"

    results: list[str] = []

    # 1. Web UI (broadcast to all connected clients for now)
    try:
        from bridge import server as _srv
        if _srv._ws_clients:
            try:
                await _deliver_to_web(payload)
                results.append("web")
            except Exception as exc:
                log.warning("Web delivery failed for user %s: %s", app_user_id, exc)
    except Exception as exc:
        log.warning("Web delivery setup failed: %s", exc)

    # 2. Telegram — use the user's own bot + their stored chat_id
    if cron_origin or "web" not in results:
        try:
            from auth.db import get_user_setting
            tg_chat_id = get_user_setting(app_user_id, "telegram_chat_id")
            if tg_chat_id:
                ok = await _deliver_to_telegram(text, str(tg_chat_id), app_user_id=app_user_id)
                if ok:
                    results.append("telegram")
        except Exception as exc:
            log.warning("Per-user Telegram delivery failed for %s: %s", app_user_id, exc)

    # 3. Notification queue as last resort
    if not results:
        from bridge import notifications
        await notifications.enqueue({
            "kind": payload.get("kind", "autonomous_utterance"),
            "source": payload.get("source", "cron"),
            "summary": text,
            "detail": payload.get("detail", ""),
            "emotion": payload.get("emotion", "neutral"),
            "description": payload.get("description", ""),
            "cron_origin": cron_origin,
        })
        results.append("queued")

    return "+".join(results)


async def route_autonomous(payload: dict[str, Any]) -> str:
    """Deliver a proactive text. ``payload`` keys:
      text (required), emotion, gesture/action, source, kind, description,
      cron_origin (bool).
    Returns a ``+``-joined string of destinations reached, e.g.:
      ``"web"``, ``"telegram"``, ``"web+telegram"``, ``"queued"``, ``"empty"``.

    Routing semantics:
      - Normal autonomous (``cron_origin=False``):
          web → telegram (if web missed) → queue (last resort)
      - Cron-origin (``cron_origin=True``):
          web AND telegram are BOTH invoked unconditionally — user may be
          away from the desktop, so a phone ping is always wanted. If both
          fail, queue as last resort.
    """
    # Pop cron_origin so it doesn't leak into the web delivery payload.
    cron_origin = bool(payload.pop("cron_origin", False))

    text = (payload.get("text") or "").strip()
    if not text:
        return "empty"

    results: list[str] = []

    # --- 1. Web UI ---
    web_attempted = False
    try:
        from bridge import server as _srv
        if _srv._ws_clients:
            web_attempted = True
            try:
                await _deliver_to_web(payload)
                results.append("web")
            except Exception as exc:
                log.warning("Web delivery failed: %s", exc)
    except Exception as exc:
        log.warning("Web delivery setup failed: %s", exc)

    # --- 2. Telegram ---
    # For cron reports: ALWAYS attempt Telegram (user may be away from desktop).
    # For regular autonomous: only fall back to Telegram when web didn't land.
    primary = load_primary_user() or {}
    tg_user = primary.get("telegram_user_id")
    tg_app_user = primary.get("telegram_app_user_id")
    if tg_user and (cron_origin or "web" not in results):
        ok = await _deliver_to_telegram(text, tg_user, app_user_id=tg_app_user)
        if ok:
            results.append("telegram")

    # --- 3. Last-resort queue (only if nothing landed) ---
    if not results:
        from bridge import notifications
        await notifications.enqueue({
            "kind": payload.get("kind", "autonomous_utterance"),
            "source": payload.get("source", "autonomy"),
            "summary": text,
            "detail": payload.get("detail", ""),
            "emotion": payload.get("emotion", "neutral"),
            "description": payload.get("description", ""),
            "cron_origin": cron_origin,
        })
        results.append("queued")

    return "+".join(results)
