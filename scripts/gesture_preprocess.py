#!/usr/bin/env python3
"""Preprocess gesture recorder output for EMAGE fine-tuning.

Walks data/recordings/<session>/take_NNN/ and for each complete take:
  1. Validates landmarks.npy + audio.wav + transcript.json exist
  2. Resamples landmarks to exactly 30 fps (interpolation)
  3. Normalises landmarks: root-centred, unit shoulder width
  4. Re-exports as aligned (landmarks_30fps.npy, audio.wav) pairs
  5. Writes a manifest.json listing all valid takes

Usage:
    python scripts/gesture_preprocess.py
    python scripts/gesture_preprocess.py --sessions data/recordings/session_20260412_*
    python scripts/gesture_preprocess.py --min_duration 1.5 --max_duration 30

Output:
    data/processed/<session>_processed/
        take_NNN/
            landmarks_30fps.npy   — (T, 33, 3) float32, 30fps, normalised
            audio.wav             — 16 kHz mono WAV (unchanged)
            transcript.json       — original + added fields
        manifest.json             — list of all valid take paths
"""

from __future__ import annotations

import argparse
import glob
import json
import wave
from pathlib import Path

import numpy as np
from scipy.interpolate import interp1d


TARGET_FPS = 30
AUDIO_SR   = 16_000
# Minimum acceptable recording length
DEFAULT_MIN_DURATION = 1.0   # seconds
DEFAULT_MAX_DURATION = 60.0  # seconds

# MediaPipe landmark indices for normalisation
# 11=left_shoulder, 12=right_shoulder, 23=left_hip, 24=right_hip
IDX_L_SHOULDER = 11
IDX_R_SHOULDER = 12
IDX_L_HIP      = 23
IDX_R_HIP      = 24


def resample_landmarks(lm: np.ndarray, src_fps: float, dst_fps: float = 30.0) -> np.ndarray:
    """Temporal resample landmarks (T_src, 33, 3) → (T_dst, 33, 3)."""
    t_src, n_joints, n_dim = lm.shape
    if t_src < 2:
        return lm

    src_times = np.linspace(0, 1, t_src)
    dst_times = np.linspace(0, 1, max(1, int(round(t_src * dst_fps / src_fps))))

    flat = lm.reshape(t_src, -1)  # (T_src, 99)
    interp = interp1d(src_times, flat, axis=0, kind="linear", fill_value="extrapolate")
    resampled = interp(dst_times)
    return resampled.reshape(-1, n_joints, n_dim).astype(np.float32)


def normalise_landmarks(lm: np.ndarray) -> np.ndarray:
    """Root-centre and scale by shoulder width per frame.

    Root = midpoint of left/right hip.
    Scale = distance between left and right shoulder (set to 1.0).
    """
    lm = lm.copy()
    root = 0.5 * (lm[:, IDX_L_HIP] + lm[:, IDX_R_HIP])   # (T, 3)
    lm -= root[:, None, :]                                  # centre

    shoulder_dist = np.linalg.norm(
        lm[:, IDX_L_SHOULDER] - lm[:, IDX_R_SHOULDER], axis=-1
    )  # (T,)
    # Guard against zero width (bad frames) — clip to small positive
    scale = np.clip(shoulder_dist, 1e-4, None)             # (T,)
    lm /= scale[:, None, None]

    return lm


def load_audio_samples(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read WAV → float32 samples, returns (audio, sr)."""
    with wave.open(str(wav_path), "r") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        raw = wf.readframes(n)
        sampwidth = wf.getsampwidth()

    dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sampwidth, np.int16)
    pcm = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    pcm /= float(np.iinfo(dtype).max)
    return pcm, sr


def save_wav(audio: np.ndarray, sr: int, path: Path):
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def process_take(take_dir: Path, out_dir: Path,
                 min_dur: float, max_dur: float) -> dict | None:
    """Process one take directory. Returns manifest entry or None if invalid."""
    lm_path  = take_dir / "landmarks.npy"
    wav_path = take_dir / "audio.wav"
    meta_path = take_dir / "transcript.json"

    missing = [p.name for p in (lm_path, wav_path, meta_path) if not p.exists()]
    if missing:
        print(f"  SKIP {take_dir.name}: missing {missing}")
        return None

    meta = json.loads(meta_path.read_text())
    lm   = np.load(lm_path)            # (T, 33, 3)
    audio, sr = load_audio_samples(wav_path)

    n_frames = lm.shape[0]
    src_fps  = float(meta.get("fps", TARGET_FPS))
    duration = n_frames / src_fps if src_fps > 0 else 0.0

    if duration < min_dur:
        print(f"  SKIP {take_dir.name}: too short ({duration:.1f}s < {min_dur}s)")
        return None
    if duration > max_dur:
        print(f"  SKIP {take_dir.name}: too long ({duration:.1f}s > {max_dur}s)")
        return None
    if lm.shape[1] != 33 or lm.shape[2] != 3:
        print(f"  SKIP {take_dir.name}: unexpected landmarks shape {lm.shape}")
        return None

    # 1. Resample to 30 fps
    lm_30 = resample_landmarks(lm, src_fps, TARGET_FPS)

    # 2. Normalise
    lm_norm = normalise_landmarks(lm_30)

    # 3. Audio is already 16 kHz — just copy
    out_take = out_dir / take_dir.name
    out_take.mkdir(parents=True, exist_ok=True)

    np.save(out_take / "landmarks_30fps.npy", lm_norm)
    save_wav(audio, sr, out_take / "audio.wav")

    # 4. Updated metadata
    new_meta = {
        **meta,
        "fps_processed":     TARGET_FPS,
        "n_frames_30fps":    int(lm_norm.shape[0]),
        "duration_s_30fps":  round(lm_norm.shape[0] / TARGET_FPS, 3),
        "normalised":        True,
        "src_fps":           round(src_fps, 2),
    }
    (out_take / "transcript.json").write_text(json.dumps(new_meta, indent=2))

    n_out = lm_norm.shape[0]
    print(f"  ✓ {take_dir.name}  {n_frames}f@{src_fps:.0f}fps → {n_out}f@30fps  "
          f"{duration:.1f}s  \"{meta.get('text','')[:50]}\"")

    return {
        "take":        take_dir.name,
        "session":     meta.get("session", take_dir.parent.name),
        "path":        str(out_take.resolve()),
        "text":        meta.get("text", ""),
        "duration_s":  round(duration, 3),
        "n_frames":    n_out,
    }


def find_sessions(roots: list[str]) -> list[Path]:
    """Expand globs + direct paths into session directories."""
    sessions = []
    for pattern in roots:
        for p in sorted(glob.glob(pattern)):
            d = Path(p)
            if d.is_dir():
                sessions.append(d)
    return sessions


def main():
    ap = argparse.ArgumentParser(description="Preprocess gesture recordings for EMAGE")
    ap.add_argument(
        "--sessions", nargs="+",
        default=["data/recordings/session_*"],
        help="Session directories or globs to process",
    )
    ap.add_argument("--out",     default="data/processed",  help="Output root directory")
    ap.add_argument("--min_duration", type=float, default=DEFAULT_MIN_DURATION)
    ap.add_argument("--max_duration", type=float, default=DEFAULT_MAX_DURATION)
    args = ap.parse_args()

    sessions = find_sessions(args.sessions)
    if not sessions:
        print(f"No sessions found for patterns: {args.sessions}")
        return

    out_root = Path(args.out)
    all_entries = []

    for session in sessions:
        takes = sorted(session.glob("take_*"))
        if not takes:
            print(f"Session {session.name}: no takes, skipping")
            continue

        out_session = out_root / f"{session.name}_processed"
        print(f"\nSession {session.name}  ({len(takes)} takes) → {out_session}")

        for take in takes:
            entry = process_take(take, out_session,
                                 args.min_duration, args.max_duration)
            if entry:
                all_entries.append(entry)

        # Per-session manifest
        session_entries = [e for e in all_entries
                           if e["session"] == session.name]
        manifest_path = out_session / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(session_entries, indent=2))

    # Global manifest
    global_manifest = out_root / "manifest.json"
    global_manifest.write_text(json.dumps(all_entries, indent=2))

    total_dur = sum(e["duration_s"] for e in all_entries)
    print(f"\n{'='*60}")
    print(f"Processed {len(all_entries)} takes  |  {total_dur / 60:.1f} minutes total")
    print(f"Global manifest → {global_manifest}")


if __name__ == "__main__":
    main()
