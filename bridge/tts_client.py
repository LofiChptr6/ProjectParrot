"""TTS client abstraction — async-generator interface over pluggable engines.

Usage:
    engine = get_tts_engine()
    async for chunk in engine.synthesize("Hello there."):
        # chunk is WAV bytes
        ...

Engines implement ``synthesize(text) -> AsyncIterator[bytes]``. Non-streaming
engines yield a single chunk. Streaming engines yield many. The downstream
consumer doesn't need to know which is which.

Engines available:
    - F5Engine:         calls existing tts/service.py /synthesize, single chunk
    - ChatterboxEngine: streaming (stub — raises NotImplementedError until wired)

Selected via config.tts.engine = "f5-tts" | "chatterbox".
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional, Protocol

import httpx

log = logging.getLogger(__name__)


class TTSEngine(Protocol):
    """Engine protocol — implement this to add a new TTS backend."""

    async def synthesize(self, text: str, *,
                         ref_audio_path: str | None = None,
                         ) -> AsyncIterator[bytes]:  # pragma: no cover
        ...

    async def close(self) -> None:  # pragma: no cover
        ...


class F5Engine:
    """Non-streaming F5-TTS engine — talks to tts/service.py over HTTP.

    Yields the whole WAV as a single chunk. Preserves the existing service
    contract so we can migrate to streaming engines without touching the bridge.

    `ref_audio_path` (optional) overrides the global reference voice on a
    per-call basis. The TTS service falls back to its config default when None.
    """

    def __init__(self, tts_url: str, timeout_s: float = 30.0) -> None:
        self._tts_url = tts_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout_s)

    async def synthesize(self, text: str, *,
                         ref_audio_path: str | None = None,
                         ) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        payload: dict = {"text": text}
        if ref_audio_path:
            payload["ref_audio_path"] = ref_audio_path
        try:
            resp = await self._http.post(
                f"{self._tts_url}/synthesize",
                json=payload,
            )
        except Exception as e:
            log.warning("F5 TTS request failed: %s", e)
            return
        if resp.status_code != 200:
            log.warning("F5 TTS HTTP %s: %s", resp.status_code, (resp.text or "")[:300])
            return
        # Non-streaming: emit the whole WAV as one chunk
        yield resp.content

    async def close(self) -> None:
        await self._http.aclose()


class ChatterboxEngine:
    """Streaming Chatterbox engine — TO BE IMPLEMENTED.

    Design:
        - Start a local Chatterbox model process (or embed via chatterbox-tts pkg)
        - Cache speaker embedding via model.prepare_conditionals() at startup
        - Stream WAV chunks as they are generated
    """

    def __init__(self, *, reference_audio: str, device: str = "cuda:0") -> None:
        self._reference_audio = reference_audio
        self._device = device

    async def synthesize(self, text: str, *,
                         ref_audio_path: str | None = None,
                         ) -> AsyncIterator[bytes]:
        raise NotImplementedError(
            "ChatterboxEngine not yet wired. "
            "See plan: Phase 1 follow-up."
        )
        yield b""  # pragma: no cover (for type checker)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
#  Factory
# ---------------------------------------------------------------------------

_engine_singleton: Optional[TTSEngine] = None


def get_tts_engine(config: dict) -> TTSEngine:
    """Construct the engine selected by config, memoizing the instance.

    ``config`` is the resolved bridge config dict (with ``tts_url`` already set).
    """
    global _engine_singleton
    if _engine_singleton is not None:
        return _engine_singleton

    engine_name = (config.get("tts", {}).get("engine") or "f5-tts").lower()
    tts_url = config.get("tts_url") or config.get("tts", {}).get("tts_url", "")

    if engine_name in ("f5", "f5-tts", "f5_tts"):
        _engine_singleton = F5Engine(tts_url=tts_url)
    elif engine_name in ("chatterbox", "chatterbox-tts"):
        tts_cfg = config.get("tts", {})
        _engine_singleton = ChatterboxEngine(
            reference_audio=tts_cfg.get("reference_audio", ""),
            device=str(tts_cfg.get("device", "cuda:0")),
        )
    else:
        log.warning("Unknown tts.engine=%r, defaulting to F5", engine_name)
        _engine_singleton = F5Engine(tts_url=tts_url)

    return _engine_singleton


async def close_tts_engine() -> None:
    """Release resources. Call on shutdown."""
    global _engine_singleton
    if _engine_singleton is not None:
        await _engine_singleton.close()
        _engine_singleton = None
