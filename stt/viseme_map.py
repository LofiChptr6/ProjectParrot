"""Map CMU ARPABET phonemes to 5-channel VRM viseme weights.

Viseme channels (index order):
  0 = aa   wide open mouth ("ah", "aw")
  1 = ih   tight front ("ee", "ih")
  2 = ou   rounded lips ("oo", "uh")
  3 = ee   spread lips ("eh", "ae")
  4 = oh   open rounded ("ow", "oy")

Wire format: T frames x 5 floats (aa, ih, ou, ee, oh), float32 little-endian,
base64-encoded.
"""

from __future__ import annotations

import base64
import math
import struct

# ── Viseme channel names (index order) ───────────────────────────────────────

VISEME_NAMES: list[str] = ["aa", "ih", "ou", "ee", "oh"]

_VISEME_INDEX: dict[str, int] = {name: i for i, name in enumerate(VISEME_NAMES)}
_NUM_CHANNELS = len(VISEME_NAMES)

# ── ARPABET → viseme label mapping ───────────────────────────────────────────

PHONEME_TO_VISEME: dict[str, str] = {
    # Vowels — wide open (aa)
    "AA": "aa",
    "AO": "aa",
    "AH": "aa",
    # Vowels — tight front (ih)
    "IH": "ih",
    "IY": "ih",
    "EY": "ih",
    # Vowels — rounded (ou)
    "UW": "ou",
    "UH": "ou",
    "OW": "ou",
    # Vowels — spread lips (ee)
    "EH": "ee",
    "AE": "ee",
    "ER": "ee",
    "AX": "ee",
    # Diphthongs — open rounded (oh)
    "AW": "oh",
    "OY": "oh",
    # Bilabial stops — lips closed → silence
    "M": "silence",
    "B": "silence",
    "P": "silence",
    # Labiodental fricatives
    "F": "ih",
    "V": "ih",
    # Dental fricatives
    "TH": "ee",
    "DH": "ee",
    # Sibilants / affricates — teeth close together
    "S": "ee",
    "Z": "ee",
    "SH": "ee",
    "ZH": "ee",
    "CH": "ee",
    "JH": "ee",
    # Alveolar consonants
    "T": "ih",
    "D": "ih",
    "N": "ih",
    "L": "ih",
    # Velar consonants — open back
    "K": "aa",
    "G": "aa",
    "NG": "aa",
    # Approximants
    "R": "ou",
    "W": "ou",
    "Y": "ih",
    # Glottal — carry previous (default aa)
    "HH": "aa",
}

# ── Helpers ──────────────────────────────────────────────────────────────────


def _viseme_vec(label: str) -> list[float]:
    """Return a 5-element weight vector for *label* (one-hot or zeros)."""
    vec = [0.0] * _NUM_CHANNELS
    idx = _VISEME_INDEX.get(label)
    if idx is not None:
        vec[idx] = 1.0
    return vec


def _strip_stress(phoneme: str) -> str:
    """Remove trailing stress digit from ARPABET phoneme (e.g. 'AA1' → 'AA')."""
    return phoneme.rstrip("012")


def _lerp_vec(a: list[float], b: list[float], t: float) -> list[float]:
    """Element-wise linear interpolation between two vectors."""
    return [a[i] + (b[i] - a[i]) * t for i in range(_NUM_CHANNELS)]


# ── Public API ───────────────────────────────────────────────────────────────


def phonemes_to_viseme_weights(
    phoneme_timeline: list[dict],
    duration_s: float,
    fps: int = 30,
) -> list[list[float]]:
    """Convert a phoneme timeline into per-frame viseme weight arrays.

    Parameters
    ----------
    phoneme_timeline:
        List of dicts with keys ``'phoneme'`` (ARPABET string),
        ``'start'`` and ``'end'`` (seconds).
    duration_s:
        Total clip duration in seconds.
    fps:
        Output frame rate (default 30).

    Returns
    -------
    List of *T* frames, each a list of 5 floats ``[aa, ih, ou, ee, oh]``
    in the range 0.0–1.0.  A simple 2-frame lerp is applied to avoid
    harsh viseme transitions.
    """
    total_frames = max(1, math.ceil(duration_s * fps))
    frame_dur = 1.0 / fps

    # ── Pass 1: raw viseme per frame (no smoothing) ──────────────────────
    raw: list[list[float]] = []
    tl_idx = 0
    tl_len = len(phoneme_timeline)

    for fi in range(total_frames):
        t = fi * frame_dur

        # Advance index so tl_idx points at the first entry not yet ended.
        while tl_idx < tl_len and phoneme_timeline[tl_idx]["end"] <= t:
            tl_idx += 1

        # Find the active phoneme at time *t*.
        label: str | None = None
        for i in range(tl_idx, tl_len):
            entry = phoneme_timeline[i]
            if entry["start"] > t:
                break
            if entry["start"] <= t < entry["end"]:
                clean = _strip_stress(entry["phoneme"])
                label = PHONEME_TO_VISEME.get(clean)
                break

        raw.append(_viseme_vec(label or "silence"))

    # ── Pass 2: 2-frame lerp smoothing ───────────────────────────────────
    # For each frame that differs from its predecessor, blend over 2 frames.
    smoothed: list[list[float]] = [raw[0][:]]
    blend_frames = 2

    for fi in range(1, total_frames):
        prev = smoothed[fi - 1]
        target = raw[fi]
        if prev == target:
            smoothed.append(target[:])
            continue
        # How far into the blend window are we?  We look back to find when
        # the target first appeared and interpolate.
        blend_start = max(0, fi - blend_frames)
        span = fi - blend_start
        t_blend = 1.0 if span == 0 else min(1.0, (fi - blend_start) / blend_frames)
        smoothed.append(_lerp_vec(prev, target, t_blend))

    return smoothed


def pack_viseme_b64(viseme_frames: list[list[float]]) -> str:
    """Pack viseme weight frames into a base64 string.

    Format: T frames x 5 floats (aa, ih, ou, ee, oh), float32 little-endian.
    """
    buf = bytearray()
    for frame in viseme_frames:
        buf.extend(struct.pack("<5f", *frame))
    return base64.b64encode(bytes(buf)).decode()
