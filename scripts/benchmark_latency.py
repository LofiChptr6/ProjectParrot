#!/usr/bin/env python3
"""
Benchmark LLM vs TTS latency for the project mocha pipeline.

Sends 10 random phrases through the LLM and TTS services independently,
times each stage, and renders a Gantt-style horizontal bar chart.

Usage:
    cd parrot-assistant
    python scripts/benchmark_latency.py          # uses defaults from config.yaml
    python scripts/benchmark_latency.py -n 15    # 15 phrases instead of 10
    python scripts/benchmark_latency.py --no-tts # skip TTS (LLM-only timing)

Requires: httpx, matplotlib, pyyaml  (all in the project venv)
"""

import argparse
import random
import sys
import time
from pathlib import Path

import httpx
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import yaml

ROOT = Path(__file__).resolve().parent.parent
config = yaml.safe_load((ROOT / "config.yaml").read_text())

LLM_URL = config["bridge"]["llm_url"]
TTS_URL = f"http://127.0.0.1:{config['tts']['port']}"
LLM_MODEL = config["llm"]["model"]

PHRASES = [
    "What's the meaning of life?",
    "Tell me a joke about robots.",
    "How does a neural network learn?",
    "What's your favourite colour and why?",
    "Explain quantum entanglement in one sentence.",
    "If you could travel anywhere, where would you go?",
    "What's the best programming language for beginners?",
    "Summarise the plot of Star Wars in three sentences.",
    "Why is the sky blue?",
    "Can you write a haiku about coffee?",
    "What would you do with a million dollars?",
    "How do airplanes stay in the air?",
    "Tell me something surprising about the ocean.",
    "What's the difference between AI and machine learning?",
    "Recommend a good book for a rainy day.",
    "How does a refrigerator work?",
    "If animals could talk, which would be the rudest?",
    "Explain recursion like I'm five.",
    "What's the fastest animal on Earth?",
    "Why do we dream?",
]


def query_llm(prompt: str) -> tuple[str, float]:
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "Reply in 1-3 short sentences."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.7, "num_predict": 256},
    }
    t0 = time.perf_counter()
    resp = httpx.post(f"{LLM_URL}/api/chat", json=body, timeout=120)
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    text = resp.json().get("message", {}).get("content", "")
    return text, elapsed


def query_tts(text: str) -> tuple[int, float]:
    t0 = time.perf_counter()
    resp = httpx.post(f"{TTS_URL}/synthesize", json={"text": text}, timeout=120)
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    return len(resp.content), elapsed


def run_benchmark(n: int, skip_tts: bool):
    phrases = random.sample(PHRASES, min(n, len(PHRASES)))
    if n > len(PHRASES):
        phrases += random.choices(PHRASES, k=n - len(PHRASES))

    results = []
    for i, phrase in enumerate(phrases, 1):
        print(f"[{i}/{n}] {phrase}")

        reply, llm_sec = query_llm(phrase)
        reply_short = reply.replace("\n", " ")[:80]
        print(f"       LLM  {llm_sec:5.2f}s  → {reply_short}…")

        tts_sec = 0.0
        wav_kb = 0
        if not skip_tts:
            wav_bytes, tts_sec = query_tts(reply)
            wav_kb = wav_bytes // 1024
            print(f"       TTS  {tts_sec:5.2f}s  → {wav_kb} KB WAV")

        results.append({
            "phrase": phrase[:50],
            "llm_sec": llm_sec,
            "tts_sec": tts_sec,
            "reply_len": len(reply),
            "wav_kb": wav_kb,
        })

    return results


def draw_chart(results: list[dict], out_path: Path):
    n = len(results)
    labels = [f"#{i+1}  {r['phrase'][:38]}…" for i, r in enumerate(results)]
    llm_times = [r["llm_sec"] for r in results]
    tts_times = [r["tts_sec"] for r in results]

    fig, ax = plt.subplots(figsize=(12, max(4, n * 0.55)))

    y = range(n)
    bars_llm = ax.barh(y, llm_times, height=0.45, color="#4C78A8", label="LLM")
    bars_tts = ax.barh(
        y, tts_times, left=llm_times, height=0.45, color="#E45756", label="TTS"
    )

    for i in range(n):
        total = llm_times[i] + tts_times[i]
        ax.text(
            total + 0.08, i, f"{total:.1f}s",
            va="center", fontsize=8, color="#333",
        )

    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Time (seconds)")
    ax.set_title("project mocha — LLM vs TTS Latency per Phrase")
    ax.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax.legend(loc="lower right")
    ax.grid(axis="x", alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    print(f"\nChart saved → {out_path}")
    plt.close(fig)


def print_summary(results: list[dict]):
    llm = [r["llm_sec"] for r in results]
    tts = [r["tts_sec"] for r in results]
    total = [l + t for l, t in zip(llm, tts)]

    print("\n" + "=" * 60)
    print(f"{'':>12}  {'LLM':>8}  {'TTS':>8}  {'Total':>8}")
    print(f"{'Mean':>12}  {sum(llm)/len(llm):>7.2f}s  {sum(tts)/len(tts):>7.2f}s  {sum(total)/len(total):>7.2f}s")
    print(f"{'Min':>12}  {min(llm):>7.2f}s  {min(tts):>7.2f}s  {min(total):>7.2f}s")
    print(f"{'Max':>12}  {max(llm):>7.2f}s  {max(tts):>7.2f}s  {max(total):>7.2f}s")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Benchmark project mocha latency")
    parser.add_argument("-n", type=int, default=10, help="number of phrases (default 10)")
    parser.add_argument("--no-tts", action="store_true", help="skip TTS, measure LLM only")
    parser.add_argument("-o", "--output", type=str, default=None, help="chart output path (default: scripts/benchmark_latency.png)")
    args = parser.parse_args()

    out = Path(args.output) if args.output else ROOT / "scripts" / "benchmark_latency.png"

    print(f"project mocha Latency Benchmark")
    print(f"  LLM : {LLM_URL} ({LLM_MODEL})")
    print(f"  TTS : {TTS_URL} {'(skipped)' if args.no_tts else ''}")
    print(f"  N   : {args.n}")
    print()

    results = run_benchmark(args.n, args.no_tts)
    print_summary(results)
    draw_chart(results, out)


if __name__ == "__main__":
    main()
