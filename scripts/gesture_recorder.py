#!/usr/bin/env python3
"""Gesture recorder — captures webcam pose + audio for EMAGE fine-tuning.

Reads prompts from a text file, displays them full-screen, and records
synchronized MediaPipe 3D pose landmarks + 16 kHz mono audio for each take.

Usage:
    python scripts/gesture_recorder.py
    python scripts/gesture_recorder.py --prompts scripts/recorder_prompts/english.txt
    python scripts/gesture_recorder.py --out data/recordings --cam 0

Controls (while preview window is focused):
    SPACE or R    — start / stop recording
    N or →        — next prompt (if not recording)
    P or ←        — previous prompt (if not recording)
    Q / Escape    — quit

Output per take (saved to <out>/<session>/take_NNN/):
    landmarks.npy   — (T, 33, 3) float32  MediaPipe 3D world landmarks
    audio.wav       — 16 kHz mono PCM WAV
    transcript.json — {text, fps, sr, n_frames, n_samples, duration_s, take}
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
import sounddevice as sd

# ── MediaPipe skeleton connections to draw ─────────────────────────────────────
# Upper-body subset of the 33 standard landmarks
POSE_CONNECTIONS = [
    (11, 12),  # left_shoulder — right_shoulder
    (11, 13),  # left_shoulder — left_elbow
    (13, 15),  # left_elbow — left_wrist
    (12, 14),  # right_shoulder — right_elbow
    (14, 16),  # right_elbow — right_wrist
    (11, 23),  # left_shoulder — left_hip
    (12, 24),  # right_shoulder — right_hip
    (23, 24),  # left_hip — right_hip
    (0,  11),  # nose — left_shoulder (upper torso approx)
    (0,  12),
    (0,   7),  # nose — left ear
    (0,   8),  # nose — right ear
]

# Landmark indices we actually care about (upper body)
UPPER_BODY_LANDMARKS = list(range(0, 25))  # 0-24: face + torso + arms to wrists


# ── prompt loader ──────────────────────────────────────────────────────────────

def load_prompts(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    prompts = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    if not prompts:
        raise ValueError(f"No prompts found in {path}")
    return prompts


# ── audio capture thread ───────────────────────────────────────────────────────

class AudioCapture:
    """Non-blocking audio capture via sounddevice InputStream."""

    SR = 16_000
    CHUNK = 1024  # frames per callback

    def __init__(self):
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._recording = False
        self._buf: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None

    def start_stream(self):
        self._stream = sd.InputStream(
            samplerate=self.SR,
            channels=1,
            dtype="float32",
            blocksize=self.CHUNK,
            callback=self._cb,
        )
        self._stream.start()

    def stop_stream(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _cb(self, indata, frames, time_info, status):
        if self._recording:
            self._buf.append(indata[:, 0].copy())

    def start_recording(self):
        self._buf.clear()
        self._recording = True

    def stop_recording(self) -> np.ndarray:
        self._recording = False
        if not self._buf:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._buf)

    def save_wav(self, audio: np.ndarray, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        pcm = (audio * 32767).astype(np.int16)
        with wave.open(str(path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SR)
            wf.writeframes(pcm.tobytes())


# ── main recorder ──────────────────────────────────────────────────────────────

class GestureRecorder:
    TARGET_FPS = 30
    FONT = cv2.FONT_HERSHEY_SIMPLEX

    # UI colours (BGR)
    C_WHITE  = (255, 255, 255)
    C_BLACK  = (0,   0,   0)
    C_GREEN  = (80,  200, 80)
    C_RED    = (60,  60,  200)
    C_YELLOW = (0,   210, 230)
    C_GRAY   = (140, 140, 140)
    C_BLUE   = (220, 130, 50)

    def __init__(self, prompts: list[str], out_dir: Path, cam_id: int = 0):
        self.prompts = prompts
        self.prompt_idx = 0
        self.out_dir = out_dir
        self.cam_id = cam_id

        self.session_dir = out_dir / datetime.now().strftime("session_%Y%m%d_%H%M%S")
        self.take_num = 1

        self._recording = False
        self._landmarks_buf: list[np.ndarray] = []  # (33, 3) per frame
        self._frame_times: list[float] = []
        self._actual_fps = TARGET_FPS = 30.0

        self.audio = AudioCapture()
        self._mp_pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            smooth_landmarks=True,
            enable_segmentation=False,
            min_detection_confidence=0.6,
            min_tracking_confidence=0.5,
        )

        self.cap = cv2.VideoCapture(cam_id)
        self.cap.set(cv2.CAP_PROP_FPS, self.TARGET_FPS)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera {cam_id}")

        # Window layout
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError("Cannot read from camera")
        self.cam_h, self.cam_w = frame.shape[:2]
        self.win_h = self.cam_h + 220  # extra rows for UI panels
        self.win_w = self.cam_w

    # ── drawing helpers ────────────────────────────────────────────────────────

    def _put_text(
        self, img: np.ndarray, text: str, pos: tuple[int, int],
        scale: float = 0.7, color=None, thickness: int = 2,
        shadow: bool = True,
    ):
        color = color or self.C_WHITE
        x, y = pos
        if shadow:
            cv2.putText(img, text, (x + 1, y + 1), self.FONT, scale,
                        self.C_BLACK, thickness + 1, cv2.LINE_AA)
        cv2.putText(img, text, pos, self.FONT, scale, color, thickness, cv2.LINE_AA)

    def _wrap_text(self, text: str, max_chars: int = 60) -> list[str]:
        """Wrap text to multiple lines."""
        words = text.split()
        lines, cur = [], ""
        for w in words:
            if len(cur) + len(w) + 1 <= max_chars:
                cur = (cur + " " + w).strip()
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or [""]

    def _draw_skeleton(self, img: np.ndarray, results):
        """Overlay MediaPipe skeleton on frame."""
        if not results or not results.pose_landmarks:
            return
        lm = results.pose_landmarks.landmark

        # Draw connections
        for a, b in POSE_CONNECTIONS:
            pa = lm[a]
            pb = lm[b]
            if pa.visibility < 0.3 or pb.visibility < 0.3:
                continue
            x1, y1 = int(pa.x * self.cam_w), int(pa.y * self.cam_h)
            x2, y2 = int(pb.x * self.cam_w), int(pb.y * self.cam_h)
            cv2.line(img, (x1, y1), (x2, y2), self.C_BLUE, 2, cv2.LINE_AA)

        # Draw joints
        for i in UPPER_BODY_LANDMARKS:
            p = lm[i]
            if p.visibility < 0.3:
                continue
            cx, cy = int(p.x * self.cam_w), int(p.y * self.cam_h)
            cv2.circle(img, (cx, cy), 5, self.C_GREEN, -1, cv2.LINE_AA)
            cv2.circle(img, (cx, cy), 5, self.C_WHITE, 1, cv2.LINE_AA)

    def _landmarks_to_array(self, results) -> np.ndarray | None:
        """Extract (33, 3) world landmark array or None if not detected."""
        if not results or not results.pose_world_landmarks:
            return None
        wlm = results.pose_world_landmarks.landmark
        arr = np.array([[l.x, l.y, l.z] for l in wlm], dtype=np.float32)  # (33, 3)
        return arr

    # ── take management ────────────────────────────────────────────────────────

    def _take_dir(self) -> Path:
        return self.session_dir / f"take_{self.take_num:03d}"

    def _save_take(self, prompt: str, audio_data: np.ndarray):
        d = self._take_dir()
        d.mkdir(parents=True, exist_ok=True)

        # Landmarks
        if self._landmarks_buf:
            arr = np.stack(self._landmarks_buf, axis=0)  # (T, 33, 3)
        else:
            arr = np.zeros((0, 33, 3), dtype=np.float32)
        np.save(d / "landmarks.npy", arr)

        # Audio
        self.audio.save_wav(audio_data, d / "audio.wav")

        # Transcript + metadata
        n_frames = arr.shape[0]
        n_samples = len(audio_data)
        # Use measured fps if we have timing data
        if len(self._frame_times) >= 2:
            elapsed = self._frame_times[-1] - self._frame_times[0]
            fps = (len(self._frame_times) - 1) / elapsed if elapsed > 0 else self.TARGET_FPS
        else:
            fps = float(self.TARGET_FPS)
        duration = n_frames / fps if fps > 0 else 0.0

        meta = {
            "text": prompt,
            "fps": round(fps, 2),
            "sr": AudioCapture.SR,
            "n_frames": n_frames,
            "n_samples": n_samples,
            "duration_s": round(duration, 3),
            "take": self.take_num,
            "session": self.session_dir.name,
        }
        (d / "transcript.json").write_text(json.dumps(meta, indent=2))

        print(f"  ✓ take_{self.take_num:03d}  {n_frames} frames @ {fps:.1f} fps  "
              f"{n_samples / AudioCapture.SR:.1f}s audio  → {d}")
        self.take_num += 1

    # ── main loop ──────────────────────────────────────────────────────────────

    def run(self):
        self.audio.start_stream()
        cv2.namedWindow("Gesture Recorder", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Gesture Recorder", self.win_w, self.win_h)

        rec_start = 0.0
        last_saved_prompt = ""
        _recorded_takes_for_prompt: dict[str, int] = {}

        try:
            while True:
                ret, frame = self.cap.read()
                if not ret:
                    print("Camera read failed, exiting.")
                    break

                frame = cv2.flip(frame, 1)  # mirror for natural feel
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                results = self._mp_pose.process(rgb)

                # Collect frame data when recording
                if self._recording:
                    lm_arr = self._landmarks_to_array(results)
                    if lm_arr is not None:
                        self._landmarks_buf.append(lm_arr)
                        self._frame_times.append(time.monotonic())

                # Draw skeleton on frame
                self._draw_skeleton(frame, results)

                # ── build canvas ───────────────────────────────────────────────
                canvas = np.zeros((self.win_h, self.win_w, 3), dtype=np.uint8)

                # Prompt panel (top 130px)
                panel_top = np.full((130, self.win_w, 3), (30, 30, 30), dtype=np.uint8)
                prompt_text = self.prompts[self.prompt_idx]
                takes_done = _recorded_takes_for_prompt.get(prompt_text, 0)
                idx_label = f"Prompt {self.prompt_idx + 1}/{len(self.prompts)}  " \
                            f"[takes: {takes_done}]"
                self._put_text(panel_top, idx_label, (12, 28), 0.55,
                               color=self.C_GRAY, thickness=1)

                wrapped = self._wrap_text(prompt_text, max_chars=72)
                for i, line in enumerate(wrapped[:3]):
                    self._put_text(panel_top, line, (12, 58 + i * 30),
                                   scale=0.72, color=self.C_WHITE, thickness=2)
                canvas[:130] = panel_top

                # Camera frame (130 .. 130+cam_h)
                canvas[130: 130 + self.cam_h, :self.cam_w] = frame

                # ── status / controls panel (bottom 90px) ─────────────────────
                panel_bot = np.full((90, self.win_w, 3), (20, 20, 20), dtype=np.uint8)

                if self._recording:
                    elapsed = time.monotonic() - rec_start
                    rec_label = f"● REC  {elapsed:.1f}s  |  {len(self._landmarks_buf)} frames"
                    # Blinking dot: show only on even seconds
                    dot_color = self.C_RED if int(elapsed * 2) % 2 == 0 else self.C_GRAY
                    self._put_text(panel_bot, rec_label, (12, 35),
                                   scale=0.9, color=dot_color, thickness=2)
                    self._put_text(panel_bot, "SPACE / R = stop",
                                   (12, 68), scale=0.55, color=self.C_GRAY, thickness=1)
                else:
                    pose_ok = results.pose_landmarks is not None
                    status = "Pose detected ✓" if pose_ok else "No pose detected"
                    status_c = self.C_GREEN if pose_ok else self.C_YELLOW
                    self._put_text(panel_bot, status, (12, 35), scale=0.75,
                                   color=status_c, thickness=2)
                    self._put_text(
                        panel_bot,
                        "SPACE/R = record    N/→ = next    P/← = prev    Q/Esc = quit",
                        (12, 68), scale=0.52, color=self.C_GRAY, thickness=1,
                    )

                canvas[130 + self.cam_h:] = panel_bot

                # Show total takes saved in session corner
                session_label = f"Session: {self.take_num - 1} takes saved"
                self._put_text(canvas, session_label,
                               (self.win_w - 310, self.win_h - 10),
                               scale=0.5, color=self.C_GRAY, thickness=1)

                cv2.imshow("Gesture Recorder", canvas)

                key = cv2.waitKey(1) & 0xFF

                # ── key handling ───────────────────────────────────────────────
                if key in (ord("q"), 27):  # Q or Escape
                    if self._recording:
                        audio_data = self.audio.stop_recording()
                        self._save_take(self.prompts[self.prompt_idx], audio_data)
                    break

                elif key in (ord(" "), ord("r"), ord("R")):  # SPACE or R
                    if not self._recording:
                        # Start recording
                        self._landmarks_buf.clear()
                        self._frame_times.clear()
                        self.audio.start_recording()
                        rec_start = time.monotonic()
                        self._recording = True
                    else:
                        # Stop and save
                        audio_data = self.audio.stop_recording()
                        self._recording = False
                        prompt = self.prompts[self.prompt_idx]
                        self._save_take(prompt, audio_data)
                        _recorded_takes_for_prompt[prompt] = \
                            _recorded_takes_for_prompt.get(prompt, 0) + 1

                elif key in (ord("n"), ord("N"), 83) and not self._recording:
                    # Next prompt (83 = right arrow key code on some systems)
                    self.prompt_idx = (self.prompt_idx + 1) % len(self.prompts)

                elif key in (ord("p"), ord("P"), 81) and not self._recording:
                    # Previous prompt (81 = left arrow)
                    self.prompt_idx = (self.prompt_idx - 1) % len(self.prompts)

        finally:
            if self._recording:
                self.audio.stop_recording()
            self.audio.stop_stream()
            self._mp_pose.close()
            self.cap.release()
            cv2.destroyAllWindows()

        print(f"\nSession complete — {self.take_num - 1} takes saved to {self.session_dir}")


# ── entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Gesture recorder for EMAGE fine-tuning")
    ap.add_argument(
        "--prompts",
        default="scripts/recorder_prompts/english.txt",
        help="Path to prompts text file (one line per prompt, # = comment)",
    )
    ap.add_argument(
        "--out",
        default="data/recordings",
        help="Output root directory",
    )
    ap.add_argument(
        "--cam",
        type=int,
        default=0,
        help="Camera device index (default: 0)",
    )
    args = ap.parse_args()

    prompts_path = Path(args.prompts)
    if not prompts_path.exists():
        print(f"Prompts file not found: {prompts_path}")
        print("Run from the project root:  python scripts/gesture_recorder.py")
        return

    prompts = load_prompts(str(prompts_path))
    print(f"Loaded {len(prompts)} prompts from {prompts_path}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rec = GestureRecorder(prompts=prompts, out_dir=out_dir, cam_id=args.cam)

    print("\n=== Gesture Recorder ===")
    print("SPACE / R : start & stop recording")
    print("N / →     : next prompt")
    print("P / ←     : previous prompt")
    print("Q / Esc   : quit\n")
    rec.run()


if __name__ == "__main__":
    main()
