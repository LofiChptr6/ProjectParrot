"""PCM / WAV helpers for live streaming."""

from __future__ import annotations

import io
import wave


def pcm16_mono_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw little-endian PCM16 mono in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()
