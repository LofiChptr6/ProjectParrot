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


def save_primary_user(source: str, user_id: str) -> None:
    if not user_id or user_id == "agent":
        return
    _PRIMARY_USER_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = load_primary_user() or {}
    data.setdefault("first_seen_at", _iso_now())
    if source == "telegram":
        if data.get("telegram_user_id") == user_id:
            return
        data["telegram_user_id"] = user_id
    else:
        if data.get(f"{source}_user_id") == user_id:
            return
        data[f"{source}_user_id"] = user_id
    data["updated_at"] = _iso_now()
    _PRIMARY_USER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Primary user updated: source=%s user_id=%s", source, user_id)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
#  Delivery paths
# ---------------------------------------------------------------------------

async def _deliver_to_web(payload: dict) -> None:
    """Assemble a speech_segment (TTS + gesture) and broadcast to unity_clients."""
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

    await _srv._broadcast_to_unity(message)
    # speech_end, so clients can tear down dedup buffers
    await _srv._broadcast_to_unity({"type": "speech_end", "job_id": job_id, "autonomous": True})


async def _deliver_to_telegram(text: str, user_id: str) -> bool:
    from channels.base import registry
    tg = registry.get("telegram")
    if not tg:
        return False
    try:
        await tg.send(text, user_id=user_id)
        return True
    except Exception as exc:
        log.warning("Telegram send failed (user_id=%s): %s", user_id, exc)
        return False


# ---------------------------------------------------------------------------
#  Public entry point
# ---------------------------------------------------------------------------

async def route_autonomous(payload: dict[str, Any]) -> str:
    """Deliver a proactive text. ``payload`` keys:
      text (required), emotion, gesture/action, source, kind, description.
    Returns one of: "web" | "telegram" | "queued" | "empty".
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return "empty"

    # --- 1. Web UI ---
    try:
        from bridge import server as _srv
        if _srv.unity_clients:
            await _deliver_to_web(payload)
            return "web"
    except Exception as exc:
        log.warning("Web delivery failed, will try Telegram: %s", exc)

    # --- 2. Telegram ---
    primary = load_primary_user() or {}
    tg_user = primary.get("telegram_user_id")
    if tg_user:
        ok = await _deliver_to_telegram(text, tg_user)
        if ok:
            return "telegram"

    # --- 3. Queue ---
    from bridge import notifications
    await notifications.enqueue({
        "kind": payload.get("kind", "autonomous_utterance"),
        "source": payload.get("source", "autonomy"),
        "summary": text,
        "detail": payload.get("detail", ""),
        "emotion": payload.get("emotion", "neutral"),
        "description": payload.get("description", ""),
    })
    return "queued"
