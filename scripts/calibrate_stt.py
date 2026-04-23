#!/usr/bin/env python3
"""
STT Calibration Script — measures Whisper accuracy on your voice and suggests
vocabulary biasing (initial_prompt / hotwords) improvements.

Usage:
    python scripts/calibrate_stt.py [--stt-url http://127.0.0.1:8001]

What it does:
  1. Shows you a sentence to read aloud
  2. Records your mic (press Enter to start, Enter to stop)
  3. Sends the recording to the STT service
  4. Compares transcription against the known text (WER)
  5. After all sentences, prints a report with misrecognized words
  6. Saves results to stt/calibration_results.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parent.parent

# Sentences covering typical vocabulary + phonetically tricky terms
CALIBRATION_SENTENCES = [
    "Hey Mocha, how are you doing today?",
    "Can you ask Shiro to analyze the last conversation?",
    "I'm working on ProjectParrot, the real-time voice assistant.",
    "The vLLM server is running on port eight eight hundred.",
    "Please use the barge-in feature to interrupt the current response.",
    "The WebSocket connection is streaming PCM sixteen mono audio.",
    "Whisper large version three is our speech-to-text model.",
    "The Blackwell GPU has ninety-six gigabytes of VRAM.",
    "Can you search the web for the latest PyTorch release notes?",
    "Mocha, tell me a joke about neural networks.",
    "The voice activity detection threshold is set to zero point four.",
    "I need to fine-tune the silence gap for better endpointing.",
    "Run the git status command and show me the diff.",
    "The Telegram bot token starts with eight three seven five.",
    "Switch to headphone echo mode so I can use barge-in.",
]


def record_audio(sample_rate: int = 16000) -> bytes:
    """Record from mic until user presses Enter. Returns WAV bytes."""
    print("  Press ENTER to start recording...", end="", flush=True)
    input()
    print("  Recording... press ENTER to stop.", flush=True)

    frames: list[np.ndarray] = []
    recording = True

    def callback(indata, frame_count, time_info, status):
        if recording:
            frames.append(indata.copy())

    stream = sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16",
        blocksize=1024, callback=callback,
    )
    stream.start()
    input()
    recording = False
    stream.stop()
    stream.close()

    if not frames:
        return b""

    pcm = np.concatenate(frames, axis=0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(wav_bytes: bytes, stt_url: str) -> str:
    """Send WAV to STT /transcribe endpoint."""
    resp = httpx.post(
        f"{stt_url}/transcribe",
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def word_error_rate(reference: str, hypothesis: str) -> tuple[float, list[str], list[str]]:
    """Compute WER and return (wer, missed_words, inserted_words)."""
    ref_words = reference.lower().split()
    hyp_words = hypothesis.lower().split()

    # Simple Levenshtein-based WER
    d = [[0] * (len(hyp_words) + 1) for _ in range(len(ref_words) + 1)]
    for i in range(len(ref_words) + 1):
        d[i][0] = i
    for j in range(len(hyp_words) + 1):
        d[0][j] = j
    for i in range(1, len(ref_words) + 1):
        for j in range(1, len(hyp_words) + 1):
            cost = 0 if ref_words[i - 1] == hyp_words[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)

    wer = d[len(ref_words)][len(hyp_words)] / max(len(ref_words), 1)

    ref_set = set(ref_words)
    hyp_set = set(hyp_words)
    missed = sorted(ref_set - hyp_set)
    inserted = sorted(hyp_set - ref_set)

    return wer, missed, inserted


def main():
    parser = argparse.ArgumentParser(description="Calibrate STT to your voice")
    parser.add_argument("--stt-url", default="http://127.0.0.1:8001")
    parser.add_argument("--sentences", type=int, default=len(CALIBRATION_SENTENCES),
                        help="Number of sentences to test (default: all)")
    args = parser.parse_args()

    sentences = CALIBRATION_SENTENCES[:args.sentences]
    results = []
    all_missed: dict[str, int] = {}

    print(f"\n{'='*60}")
    print("  STT Calibration — Read each sentence aloud")
    print(f"{'='*60}\n")

    for i, sentence in enumerate(sentences, 1):
        print(f"\n[{i}/{len(sentences)}] Read this sentence:")
        print(f"  \"{sentence}\"\n")

        wav = record_audio()
        if not wav:
            print("  (empty recording, skipping)")
            continue

        print("  Transcribing...", end=" ", flush=True)
        try:
            hypothesis = transcribe(wav, args.stt_url)
        except Exception as e:
            print(f"ERROR: {e}")
            continue

        wer, missed, inserted = word_error_rate(sentence, hypothesis)
        print(f"done.")
        print(f"  Expected:     {sentence}")
        print(f"  Transcribed:  {hypothesis}")
        print(f"  WER: {wer:.0%}", end="")
        if missed:
            print(f"  missed: {missed}", end="")
        if inserted:
            print(f"  inserted: {inserted}", end="")
        print()

        for w in missed:
            all_missed[w] = all_missed.get(w, 0) + 1

        results.append({
            "reference": sentence,
            "hypothesis": hypothesis,
            "wer": round(wer, 4),
            "missed_words": missed,
            "inserted_words": inserted,
        })

    # Report
    print(f"\n{'='*60}")
    print("  CALIBRATION REPORT")
    print(f"{'='*60}\n")

    if not results:
        print("No results.")
        return

    avg_wer = sum(r["wer"] for r in results) / len(results)
    print(f"  Average WER: {avg_wer:.1%}")
    print(f"  Sentences tested: {len(results)}")

    if all_missed:
        # Sort by frequency, suggest as hotwords
        sorted_missed = sorted(all_missed.items(), key=lambda x: -x[1])
        print(f"\n  Frequently missed words (candidates for hotwords):")
        for word, count in sorted_missed[:15]:
            print(f"    {word:20s}  missed {count}x")

        suggested_hotwords = ", ".join(w for w, _ in sorted_missed[:10])
        print(f"\n  Suggested hotwords addition: \"{suggested_hotwords}\"")

    # Save results
    out_path = ROOT / "stt" / "calibration_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stt_url": args.stt_url,
        "average_wer": round(avg_wer, 4),
        "sentences": results,
        "frequently_missed": dict(sorted(all_missed.items(), key=lambda x: -x[1])),
    }, indent=2))
    print(f"\n  Results saved to: {out_path}")


if __name__ == "__main__":
    main()
