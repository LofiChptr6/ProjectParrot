"""Shared HTTP client for the polygon_* tools.

Module name starts with ``_`` so ``tools/custom/__init__.py`` skips it when
discovering tools (see load_custom_tools:33-34).

Responsibilities:
- API key lookup (env var, then config.yaml fallback)
- httpx GET with short-TTL URL cache (stays under the free tier's 5 req/min)
- 429 retry with exponential backoff
- Concurrency semaphore so parallel Nori rounds don't stampede the limit
- Shared numeric-handle helpers reused by tools/custom/stock_data.py
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import yaml

from tools.handle_registry import get_registry

_BASE = "https://api.polygon.io"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _PROJECT_ROOT / "config.yaml"
_DOTENV = _PROJECT_ROOT / ".env"

_TTL_RULES = [
    ("/v1/marketstatus/now", 3600),   # status changes on the hour
    ("/v3/reference/tickers", 86400), # company metadata changes daily at most
    ("/v2/aggs/ticker", 300),         # bars / prev close
    ("/v2/snapshot", 20),             # snapshot is "live" — keep TTL very short
    ("/v2/reference/news", 300),
]
_DEFAULT_TTL = 45

_cache: dict[str, tuple[float, dict]] = {}
_sem = asyncio.Semaphore(4)  # below free-tier 5 req/min ceiling


def _read_dotenv_key(name: str) -> str:
    """Minimal .env parser — only looks for KEY=VALUE (optional quotes), no
    export/interpolation. Enough for our single-line secret, avoids a python-dotenv dep."""
    try:
        for line in _DOTENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                v = v.strip()
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                return v
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    return ""


def _api_key() -> str:
    """Resolve the Polygon API key. Order: env var → .env file → config.yaml."""
    if k := os.environ.get("POLYGON_API_KEY"):
        return k
    if k := _read_dotenv_key("POLYGON_API_KEY"):
        return k
    try:
        cfg = yaml.safe_load(_CONFIG.read_text()) or {}
    except Exception:
        return ""
    return (cfg.get("api_keys") or {}).get("polygon", "") or ""


def _ttl_for(path: str) -> int:
    for prefix, sec in _TTL_RULES:
        if path.startswith(prefix):
            return sec
    return _DEFAULT_TTL


async def fetch(path: str, params: dict | None = None) -> dict:
    """GET https://api.polygon.io{path} with Bearer auth. Cached and retried.

    Authorization header is used instead of the ``?apiKey=...`` query param so
    the secret never appears in httpx's INFO log line (which echoes the URL).
    """
    key = _api_key()
    if not key:
        raise RuntimeError("POLYGON_API_KEY not configured")

    params = dict(params or {})
    headers = {"Authorization": f"Bearer {key}"}
    cache_key = f"{path}?{sorted(params.items())}"
    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit and now - hit[0] < _ttl_for(path):
        return hit[1]

    async with _sem:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            for attempt in range(3):
                r = await client.get(f"{_BASE}{path}", params=params)
                if r.status_code == 429:
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                    continue
                r.raise_for_status()
                data = r.json()
                _cache[cache_key] = (now, data)
                return data
    raise RuntimeError("polygon rate-limited after 3 retries")


# ─── Numeric handle helpers (shared with stock_data.py) ─────────────────────

def h_price(val: float | int | None) -> str | None:
    """Mint a num: handle for a formatted USD price (e.g. '$39.75')."""
    if val is None:
        return None
    return get_registry().issue("num", f"${float(val):,.2f}")


def h_pct(val: float | int | None) -> str | None:
    """Mint a num: handle for a percentage with sign ('+1.24%' / '-0.50%')."""
    if val is None:
        return None
    v = float(val)
    sign = "+" if v >= 0 else ""
    return get_registry().issue("num", f"{sign}{v:.2f}%")


def h_delta(val: float | int | None) -> str | None:
    """Mint a num: handle for a signed absolute change ('+1.25' / '-0.50')."""
    if val is None:
        return None
    v = float(val)
    sign = "+" if v >= 0 else ""
    return get_registry().issue("num", f"{sign}{v:.2f}")


def h_count(val: float | int | None) -> str | None:
    """Mint a num: handle for a thousands-separated integer count (volume, shares)."""
    if val is None:
        return None
    return get_registry().issue("num", f"{int(val):,}")


def h_big_usd(val: float | int | None) -> str | None:
    """Mint a num: handle for a large-dollar value (market cap) in $X.XB / $X.XXT form."""
    if val is None:
        return None
    v = float(val)
    if v >= 1e12:
        return get_registry().issue("num", f"${v / 1e12:.2f}T")
    if v >= 1e9:
        return get_registry().issue("num", f"${v / 1e9:.2f}B")
    if v >= 1e6:
        return get_registry().issue("num", f"${v / 1e6:.1f}M")
    return get_registry().issue("num", f"${v:,.0f}")
