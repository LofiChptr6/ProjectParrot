"""
TorchAudio 2.9+ routes load() through TorchCodec, which is uninstalled in this
environment (its native .so files require libnvrtc.so.13, absent under WSL /
CUDA 12.x). This shim replaces torchaudio.load with a soundfile-based reader
so F5-TTS WAV I/O works without TorchCodec.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import soundfile as sf
import torch

_applied = False


def apply_torchaudio_soundfile_shim() -> None:
    global _applied
    if _applied:
        return

    import torchaudio

    def load(
        uri,
        frame_offset: int = 0,
        num_frames: int = -1,
        normalize: bool = True,
        channels_first: bool = True,
        format=None,
        buffer_size: int = 4096,
        backend=None,
    ):
        del normalize, format, buffer_size, backend

        if hasattr(uri, "read") and not isinstance(uri, (str, bytes, Path)):
            raw = uri.read()
            wav, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        else:
            wav, sr = sf.read(os.fspath(uri), dtype="float32", always_2d=True)

        if frame_offset > 0 or num_frames >= 0:
            end = wav.shape[0] if num_frames < 0 else min(wav.shape[0], frame_offset + num_frames)
            wav = wav[frame_offset:end]

        if channels_first:
            tensor = torch.from_numpy(wav.T.copy())
        else:
            tensor = torch.from_numpy(wav.copy())
        return tensor, int(sr)

    torchaudio.load = load  # type: ignore[method-assign]
    _applied = True
