"""
Voice activity detection + two-tier endpointing for phone-call style streaming.

Consumes arbitrary-length chunks of PCM16 mono 16 kHz audio, runs Silero VAD
per frame, and yields utterance segments tagged as "interim" or "final":

  "interim" — a short pause was detected (user is mid-thought).  Caller should
              transcribe for visual feedback but NOT send to LLM yet.
  "final"   — a turn-ending pause was detected.  Caller should combine all
              interim texts + this final chunk and send to LLM.

Falls back to webrtcvad if Silero (torch) is not available.
"""

from __future__ import annotations

import logging
from typing import Literal

log = logging.getLogger("vad")

# ---------------------------------------------------------------------------
#  Try Silero first, fall back to webrtcvad
# ---------------------------------------------------------------------------
_USE_SILERO = False
_silero_model = None

try:
    import torch

    def _load_silero():
        global _silero_model, _USE_SILERO
        if _silero_model is not None:
            return _silero_model
        try:
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-vad",
                model="silero_vad",
                onnx=False,
                trust_repo=True,
            )
            # Force CPU — Silero is tiny, no point wasting GPU memory
            model = model.cpu()
            _silero_model = model
            _USE_SILERO = True
            log.info("Silero VAD loaded (CPU)")
            return model
        except Exception as exc:
            log.warning("Silero VAD load failed, falling back to webrtcvad: %s", exc)
            return None
except ImportError:
    torch = None  # type: ignore[assignment]
    def _load_silero():
        return None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None


SegmentTag = Literal["interim", "final"]


class VadUtteranceSegmenter:
    """Two-tier VAD segmenter with interim + final utterance emission.

    Parameters
    ----------
    sample_rate : int
        Must be 16000 for Silero; 8000/16000/32000/48000 for webrtcvad.
    frame_ms : int
        Frame duration in ms: 20 or 30 recommended for Silero (32ms native),
        10/20/30 for webrtcvad.
    aggressiveness : int
        webrtcvad aggressiveness (0-3). Ignored when using Silero.
    silence_ms_interim : int
        Short-pause silence gap to emit an interim utterance (default 350ms).
    silence_ms_final : int
        Turn-ending silence gap to emit a final utterance (default 900ms).
    min_speech_ms : int
        Minimum utterance length. Shorter bursts are dropped.
    max_utterance_ms : int
        Force-finalize after this duration (prevents unbounded accumulation).
    silero_threshold : float
        Speech probability threshold for Silero (0.0-1.0). Default 0.4.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        silence_ms: int = 450,         # legacy param — maps to silence_ms_final
        silence_ms_interim: int = 350,
        silence_ms_final: int = 900,
        min_speech_ms: int = 200,
        max_utterance_ms: int = 15000,
        silero_threshold: float = 0.4,
    ):
        self.sample_rate = sample_rate

        # Try Silero
        self._silero = _load_silero() if torch is not None else None
        self._silero_threshold = silero_threshold

        if self._silero is not None:
            # Silero works best with 512 samples at 16kHz (32ms)
            # We'll use 512-sample chunks regardless of frame_ms
            self._silero_chunk_samples = 512
            self.frame_bytes = self._silero_chunk_samples * 2  # PCM16 = 2 bytes/sample
            frame_ms_actual = (self._silero_chunk_samples * 1000) // sample_rate
        else:
            # Fallback: webrtcvad
            if webrtcvad is None:
                raise RuntimeError(
                    "Neither torch (for Silero VAD) nor webrtcvad is installed. "
                    "Install one: pip install torch  OR  pip install webrtcvad-wheels"
                )
            if sample_rate not in (8000, 16000, 32000, 48000):
                raise ValueError(f"Unsupported sample_rate for webrtcvad: {sample_rate}")
            if frame_ms not in (10, 20, 30):
                raise ValueError(f"frame_ms must be 10, 20, or 30, got {frame_ms}")
            self._vad = webrtcvad.Vad(aggressiveness)
            self.frame_bytes = (sample_rate * frame_ms // 1000) * 2
            frame_ms_actual = frame_ms

        # Two-tier silence thresholds (in frames)
        self._frame_ms = frame_ms_actual
        self.silence_frames_interim = max(1, silence_ms_interim // frame_ms_actual)
        self.silence_frames_final = max(1, silence_ms_final // frame_ms_actual)
        self.min_speech_frames = max(1, min_speech_ms // frame_ms_actual)
        self.max_utterance_bytes = (sample_rate * 2 * max_utterance_ms) // 1000

        # State
        self._pending = bytearray()
        self._in_speech = False
        self._utt = bytearray()          # current utterance PCM accumulator
        self._silent_run = 0
        self._interim_emitted = False     # whether we already emitted interim for current utt

        backend = "silero" if self._silero is not None else "webrtcvad"
        log.info(
            "VAD segmenter: backend=%s, interim=%dms, final=%dms, min=%dms, max=%dms",
            backend,
            self.silence_frames_interim * frame_ms_actual,
            self.silence_frames_final * frame_ms_actual,
            min_speech_ms,
            max_utterance_ms,
        )

    # ------------------------------------------------------------------ #
    #  Public API                                                         #
    # ------------------------------------------------------------------ #

    def feed(self, data: bytes) -> list[tuple[SegmentTag, bytes]]:
        """Append audio; return tagged utterance segments.

        Returns a list of ``("interim", pcm)`` or ``("final", pcm)`` tuples.
        """
        self._pending.extend(data)
        out: list[tuple[SegmentTag, bytes]] = []

        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[: self.frame_bytes])
            del self._pending[: self.frame_bytes]

            speech = self._is_speech(frame)

            if speech:
                self._silent_run = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._utt.clear()
                    self._interim_emitted = False
                self._utt.extend(frame)

                # Force-finalize on max length
                if len(self._utt) >= self.max_utterance_bytes:
                    u = bytes(self._utt)
                    self._utt.clear()
                    self._in_speech = False
                    self._silent_run = 0
                    self._interim_emitted = False
                    if self._silero is not None:
                        self._silero.reset_states()
                    if self._meets_min_len(u):
                        out.append(("final", u))
            else:
                if self._in_speech:
                    self._silent_run += 1

                    # Tier 1: interim emission (short pause)
                    if (
                        not self._interim_emitted
                        and self._silent_run >= self.silence_frames_interim
                        and self._meets_min_len(self._utt)
                    ):
                        out.append(("interim", bytes(self._utt)))
                        self._interim_emitted = True

                    # Tier 2: final emission (turn-ending pause)
                    if self._silent_run >= self.silence_frames_final:
                        u = bytes(self._utt)
                        self._utt.clear()
                        self._in_speech = False
                        self._silent_run = 0
                        self._interim_emitted = False
                        if self._silero is not None:
                            self._silero.reset_states()
                        if self._meets_min_len(u):
                            out.append(("final", u))

        return out

    def flush(self) -> list[tuple[SegmentTag, bytes]]:
        """End of stream: flush any pending speech as a final utterance."""
        out: list[tuple[SegmentTag, bytes]] = []
        if self._in_speech and self._utt and self._meets_min_len(self._utt):
            out.append(("final", bytes(self._utt)))
        self._utt.clear()
        self._in_speech = False
        self._silent_run = 0
        self._interim_emitted = False
        self._pending.clear()
        if self._silero is not None:
            self._silero.reset_states()
        return out

    def update_timings(self, *, silence_ms_interim: int | None = None,
                       silence_ms_final: int | None = None) -> None:
        """Update silence thresholds at runtime (called from dashboard Config tab)."""
        if silence_ms_interim is not None:
            self.silence_frames_interim = max(1, silence_ms_interim // self._frame_ms)
        if silence_ms_final is not None:
            self.silence_frames_final = max(1, silence_ms_final // self._frame_ms)

    # ------------------------------------------------------------------ #
    #  Internals                                                          #
    # ------------------------------------------------------------------ #

    def _is_speech(self, frame: bytes) -> bool:
        """Classify a single frame as speech or silence."""
        if self._silero is not None:
            return self._is_speech_silero(frame)
        return self._is_speech_webrtc(frame)

    def _is_speech_silero(self, frame: bytes) -> bool:
        import numpy as np
        pcm = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(pcm)
        prob = self._silero(tensor, self.sample_rate).item()
        return prob >= self._silero_threshold

    def _is_speech_webrtc(self, frame: bytes) -> bool:
        try:
            return self._vad.is_speech(frame, self.sample_rate)
        except Exception as e:
            log.warning("VAD frame error: %s", e)
            return False

    def _meets_min_len(self, pcm: bytes | bytearray) -> bool:
        frames = len(pcm) // self.frame_bytes
        return frames >= self.min_speech_frames
