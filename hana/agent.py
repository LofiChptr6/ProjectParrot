"""
Hana — vision sub-agent powered by the Anthropic Claude API.

Two public entry points:

- ``inspire(images, notes)`` — given one or more reference images, return a
  workable UI palette, mood, and layout hints (JSON).
- ``critique(screenshot_b64, context, variables_used)`` — given a screenshot
  of Mocha's draft, return a score, specific wins/fails, and suggested CSS
  variable deltas (JSON).

Both entry points always return a Python dict. If the model response can't be
parsed as JSON, we wrap it into a ``{error, raw_notes}`` shape so the caller
can always surface something meaningful.

Daily-call ceiling is enforced here — see ``_check_daily_budget``.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from hana.client import get_client, client_status

log = logging.getLogger("hana.agent")

_SOUL_PATH = Path(__file__).parent / "soul.md"
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_DEEP_MODEL = "claude-sonnet-4-6"
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TEMPERATURE = 0.5
_DEFAULT_DAILY_MAX_CALLS = 200
_DEFAULT_MAX_IMAGE_BYTES = 3_500_000


# ---------------------------------------------------------------------------
#  Daily-budget counter (simple in-memory; resets at local midnight)
# ---------------------------------------------------------------------------
_day_key: str = ""
_calls_today: int = 0


def _roll_day() -> None:
    global _day_key, _calls_today
    today = dt.date.today().isoformat()
    if today != _day_key:
        _day_key = today
        _calls_today = 0


def _check_daily_budget(cap: int) -> tuple[bool, str]:
    _roll_day()
    if cap <= 0:
        return True, ""
    if _calls_today >= cap:
        return False, f"Hana is at her daily cap ({_calls_today}/{cap}). Resets at local midnight."
    return True, ""


# ---------------------------------------------------------------------------
#  Config + soul loading
# ---------------------------------------------------------------------------
def _load_cfg() -> dict:
    try:
        full = yaml.safe_load(_CONFIG_PATH.read_text())
        return full.get("hana") or {}
    except Exception:
        return {}


def _load_soul() -> str:
    try:
        return _SOUL_PATH.read_text()
    except FileNotFoundError:
        return "You are Hana, a UI design critic. Return strict JSON. Never write prose."


# ---------------------------------------------------------------------------
#  Image normalization
# ---------------------------------------------------------------------------
_IMAGE_MIME_BY_SIG: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # rough; WEBP container starts with RIFF
]


def _detect_mime(raw: bytes) -> str:
    for sig, mime in _IMAGE_MIME_BY_SIG:
        if raw.startswith(sig):
            return mime
    return "image/png"


async def _normalize_images(images: list[dict], max_bytes: int) -> list[dict]:
    """Return Anthropic API image blocks. Each input item is either
    {type: 'url', data: '...'} or {type: 'base64', data: '<b64>'}.
    For URLs we let the API fetch when possible; for local/base64 we
    inline as base64.
    """
    blocks: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
        for item in images or []:
            kind = (item.get("type") or "").lower()
            data = item.get("data") or ""
            if not data:
                continue
            if kind == "url" and data.startswith(("http://", "https://")):
                # Prefer URL source so Anthropic fetches server-side.
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": data},
                })
                continue
            if kind == "base64":
                raw = base64.b64decode(data, validate=False)
                if len(raw) > max_bytes:
                    log.warning("Hana: image too large (%d bytes), skipping", len(raw))
                    continue
                mime = _detect_mime(raw)
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                })
                continue
            # If caller passed a bare URL without type
            if kind == "" and data.startswith(("http://", "https://")):
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": data},
                })
    return blocks


# ---------------------------------------------------------------------------
#  JSON extraction (belt + suspenders)
# ---------------------------------------------------------------------------
_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    # Try fenced first.
    m = _JSON_FENCE.search(text)
    candidates = [m.group(1)] if m else []
    # Try raw — find the first balanced {...}.
    if not candidates:
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)
    # Broad fallback: first "{" to last "}".
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])

    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
#  Core API call
# ---------------------------------------------------------------------------
async def _call(model: str, content_blocks: list[dict], max_tokens: int,
                 temperature: float) -> str:
    client = get_client()
    if client is None:
        raise RuntimeError(client_status().get("reason", "Anthropic client unavailable"))

    system = _load_soul()
    t0 = time.monotonic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": content_blocks}],
    )
    elapsed = (time.monotonic() - t0) * 1000
    usage = getattr(response, "usage", None)
    log.info(
        "Hana: %s call finished in %.0fms (input=%s output=%s)",
        model, elapsed,
        getattr(usage, "input_tokens", "?"),
        getattr(usage, "output_tokens", "?"),
    )

    # Surface the first text block; the soul instructs strict JSON only.
    text_parts: list[str] = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
    return "\n".join(text_parts).strip()


# ---------------------------------------------------------------------------
#  Public entry points
# ---------------------------------------------------------------------------
async def design_scene(notes: str = "", mode: str = "standard") -> dict:
    """Design a full theme package from a vibe: palette + image/music search
    queries + decor brief. Returns JSON for Nori to fetch + assemble.

    NO URLs come out of this — Hana can't validate the live web. She returns
    search queries that Nori uses with web_search + validate_url.
    """
    global _calls_today
    cfg = _load_cfg()
    model = cfg.get("deep_model", _DEFAULT_DEEP_MODEL) if mode == "deep" else cfg.get("model", _DEFAULT_MODEL)
    max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
    temperature = float(cfg.get("temperature", _DEFAULT_TEMPERATURE))
    cap = int(cfg.get("daily_max_calls", _DEFAULT_DAILY_MAX_CALLS))

    ok, err = _check_daily_budget(cap)
    if not ok:
        return {"task": "design_scene", "error": err}

    notes_clean = (notes or "").strip()
    if not notes_clean:
        return {"task": "design_scene", "error": "Need a vibe — e.g. '90s anime cafe' or 'serene zen temple'."}

    prompt = (
        "Task: design_scene — assemble a FULL coordinated VISUAL theme package for "
        "this vibe. Music is NOT part of a theme — do not propose it here. If the user "
        "wants ambient music, that's a separate task handled by the music_player tool "
        "after the theme is set.\n"
        "Return STRICT JSON with EXACTLY these keys:\n"
        "  palette: object with 8 CSS hex colors: bg, surface, card, border, accent, accent-dim, text, muted\n"
        "  bg_query: string — a specific web_search query that will surface a landscape wallpaper "
        "matching the vibe. Include 'wallpaper 4k' or 'unsplash' to bias toward direct CDN bytes. "
        "Example: 'rainy tokyo alley neon night wallpaper 4k unsplash'. Empty string if the vibe "
        "is a pure palette tweak with no environment.\n"
        "  decor_brief: string — a 1-3 sentence brief describing decorative HTML that Nori "
        "should compose. Keep it purely ambient (floating particles, corner ornaments, subtle "
        "scanline overlay). Example: 'Six slow-falling sakura petals, pink #f8bbd0, opacity 0.4, "
        "random horizontal positions, 12s loop.' Empty string if no decor fits.\n"
        "  mood: string — 2-3 sentences of mood prose (for the preview modal).\n"
        "  notes: short string — one-line summary you'd want Mocha to read aloud.\n"
        f"\nVibe: {notes_clean}\n"
        "The target UI wraps a 3D anime VTuber character (Mocha) in a dark room. "
        "Pick colors that keep UI readable over the background (not mid-tones that compete). "
        "Return ONLY the JSON object. No prose, no fences."
    )
    content: list[dict] = [{"type": "text", "text": prompt}]

    try:
        raw = await _call(model, content, max_tokens, temperature)
    except Exception as exc:
        log.error("Hana design_scene failed: %s", exc)
        return {"task": "design_scene", "error": str(exc)}

    _calls_today += 1
    parsed = _extract_json(raw)
    if parsed:
        parsed.setdefault("task", "design_scene")
    else:
        parsed = {"task": "design_scene", "error": "Could not parse Hana response as JSON.", "raw": raw[:2000]}
    try:
        from bridge.server import _broadcast_agent_thought
        pal = (parsed.get("palette") or {}) if isinstance(parsed, dict) else {}
        preview = ", ".join(f"{k}={v}" for k, v in list(pal.items())[:4]) if pal else (parsed.get("error") or "done")
        await _broadcast_agent_thought(source="hana", kind="design_scene", text=preview)
    except Exception:
        pass
    return parsed


async def inspire(images: list[dict] | None = None, notes: str = "",
                   mode: str = "standard") -> dict:
    """Extract palette + mood from reference images, from a text vibe, or both.

    images: optional list of {"type": "url"|"base64", "data": "..."}.
    notes:  free-text guidance from Mocha/Ika ("warm cozy cafe", "cyberpunk").
            When no images are provided, Hana works from notes alone — like a
            designer giving you a palette from a description.
    mode:   "standard" (Haiku) or "deep" (Sonnet).
    """
    global _calls_today
    cfg = _load_cfg()
    model = cfg.get("deep_model", _DEFAULT_DEEP_MODEL) if mode == "deep" else cfg.get("model", _DEFAULT_MODEL)
    max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
    temperature = float(cfg.get("temperature", _DEFAULT_TEMPERATURE))
    cap = int(cfg.get("daily_max_calls", _DEFAULT_DAILY_MAX_CALLS))
    max_image_bytes = int(cfg.get("max_image_bytes", _DEFAULT_MAX_IMAGE_BYTES))

    ok, err = _check_daily_budget(cap)
    if not ok:
        return {"task": "inspire", "error": err}

    img_blocks = await _normalize_images(images or [], max_image_bytes)
    has_images = bool(img_blocks)
    notes_clean = (notes or "").strip()

    if not has_images and not notes_clean:
        return {
            "task": "inspire",
            "error": "No references and no vibe text — give me something to work with.",
        }

    if has_images:
        prompt = (
            "Task: inspire — derive a palette from the reference image(s).\n"
            f"Context from Mocha/Ika: {notes_clean or '(no extra notes — read the image)'}\n"
            "The target UI wraps a 3D anime VTuber character (Mocha). "
            "Return the JSON schema described in your soul — nothing else."
        )
    else:
        prompt = (
            "Task: inspire — NO reference images this time. Work from the vibe text only. "
            "You're a designer giving a starting palette for this description. "
            "Use your knowledge of real design references that match the mood.\n"
            f"Vibe: {notes_clean}\n"
            "The target UI wraps a 3D anime VTuber character (Mocha) in a dark room. "
            "Return the JSON schema described in your soul — nothing else."
        )
    content: list[dict] = list(img_blocks)
    content.append({"type": "text", "text": prompt})

    try:
        raw = await _call(model, content, max_tokens, temperature)
    except Exception as exc:
        log.error("Hana inspire failed: %s", exc)
        return {"task": "inspire", "error": str(exc)}

    _calls_today += 1
    parsed = _extract_json(raw)
    if parsed:
        parsed.setdefault("task", "inspire")
    else:
        parsed = {"task": "inspire", "error": "Could not parse Hana response as JSON.", "raw": raw[:2000]}
    try:
        from bridge.server import _broadcast_agent_thought
        pal = (parsed.get("palette") or {}) if isinstance(parsed, dict) else {}
        preview = ", ".join(f"{k}={v}" for k, v in list(pal.items())[:4]) if pal else (parsed.get("error") or "done")
        await _broadcast_agent_thought(source="hana", kind="inspire", text=preview)
    except Exception:
        pass
    return parsed


async def critique(screenshot_b64: str, context: str = "",
                    variables_used: dict | None = None, mode: str = "standard") -> dict:
    """Critique a UI draft from a screenshot.

    screenshot_b64: base64-encoded PNG of the current draft.
    context:        Mocha's notes on what she tried ("softened accent, glass panels").
    variables_used: the CSS variables the preview is using (optional — helps Hana
                    name her fix in the same variable namespace).
    """
    global _calls_today
    cfg = _load_cfg()
    model = cfg.get("deep_model", _DEFAULT_DEEP_MODEL) if mode == "deep" else cfg.get("model", _DEFAULT_MODEL)
    max_tokens = int(cfg.get("max_tokens", _DEFAULT_MAX_TOKENS))
    temperature = float(cfg.get("temperature", _DEFAULT_TEMPERATURE))
    cap = int(cfg.get("daily_max_calls", _DEFAULT_DAILY_MAX_CALLS))

    ok, err = _check_daily_budget(cap)
    if not ok:
        return {"task": "critique", "error": err}

    if not screenshot_b64:
        return {"task": "critique", "error": "No screenshot provided."}

    vars_json = json.dumps(variables_used, sort_keys=True, indent=2) if variables_used else "{}"
    prompt = (
        "Task: critique.\n"
        f"Mocha's notes: {context or '(no notes — score it cold)'}\n"
        f"Current CSS variables in use:\n{vars_json}\n"
        "The target UI wraps a 3D anime VTuber character. Score 0-10. "
        "If suggesting variable changes, prefer the same variable names. "
        "Return the JSON schema described in your soul — nothing else."
    )
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
        },
        {"type": "text", "text": prompt},
    ]

    try:
        raw = await _call(model, content, max_tokens, temperature)
    except Exception as exc:
        log.error("Hana critique failed: %s", exc)
        return {"task": "critique", "error": str(exc)}

    _calls_today += 1
    parsed = _extract_json(raw)
    if parsed:
        parsed.setdefault("task", "critique")
    else:
        parsed = {"task": "critique", "error": "Could not parse Hana response as JSON.", "raw": raw[:2000]}
    try:
        from bridge.server import _broadcast_agent_thought
        score = parsed.get("score") if isinstance(parsed, dict) else None
        fails = (parsed.get("what_fails") or []) if isinstance(parsed, dict) else []
        summary = f"score={score}; {(fails[0] if fails else parsed.get('error') or 'looks ok')[:160]}"
        await _broadcast_agent_thought(source="hana", kind="critique", text=summary)
    except Exception:
        pass
    return parsed


# ---------------------------------------------------------------------------
#  Status (for tools and health checks)
# ---------------------------------------------------------------------------
def status() -> dict:
    _roll_day()
    cfg = _load_cfg()
    return {
        "ready": get_client() is not None,
        "client": client_status(),
        "calls_today": _calls_today,
        "daily_cap": int(cfg.get("daily_max_calls", _DEFAULT_DAILY_MAX_CALLS)),
        "model": cfg.get("model", _DEFAULT_MODEL),
        "deep_model": cfg.get("deep_model", _DEFAULT_DEEP_MODEL),
    }
