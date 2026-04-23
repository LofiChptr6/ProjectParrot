"""
Thin wrapper around the Anthropic SDK for Hana.

Reads ANTHROPIC_API_KEY from the environment. If the variable isn't set at
process start, we fall back to a minimal parse of the project's .env file so
running under ./start.sh (which doesn't source .env) still works out of the box.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("hana.client")

_client = None
_client_err: Optional[str] = None

_DOTENV = Path(__file__).resolve().parent.parent / ".env"


def _load_env_fallback() -> Optional[str]:
    """Minimal .env parser — returns the value for ANTHROPIC_API_KEY or None.
    Doesn't mutate os.environ; callers should assign if needed.
    """
    if not _DOTENV.exists():
        return None
    try:
        for raw_line in _DOTENV.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() != "ANTHROPIC_API_KEY":
                continue
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            return val or None
    except Exception as exc:
        log.warning("Hana: .env read failed: %s", exc)
    return None


def get_client():
    """Lazy singleton; returns None (and logs) if the key is missing."""
    global _client, _client_err
    if _client is not None:
        return _client
    if _client_err is not None:
        return None

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        key = (_load_env_fallback() or "").strip()
        if key:
            os.environ["ANTHROPIC_API_KEY"] = key
    if not key:
        _client_err = "ANTHROPIC_API_KEY not set (env or .env)"
        log.warning("Hana: %s — sub-agent is offline", _client_err)
        return None

    try:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=key)
        log.info("Hana: Anthropic client initialized")
        return _client
    except Exception as exc:
        _client_err = f"Failed to init Anthropic client: {exc}"
        log.error("Hana: %s", _client_err)
        return None


def client_status() -> dict:
    """Expose readiness for the ask_hana tool to report."""
    if _client is not None:
        return {"ready": True}
    return {"ready": False, "reason": _client_err or "not initialized"}
