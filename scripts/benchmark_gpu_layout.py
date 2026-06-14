#!/usr/bin/env python3
"""
A/B benchmark: compare two GPU layouts for the project mocha pipeline.

NOTE: Both layouts are now historical. The project has moved to single-GPU mode
(all services on Blackwell GPU 0). This script is kept for reference/regression testing.

Layout A (legacy):   Blackwell (GPU 0) → LLM only,  5070 Ti (GPU 1) → TTS
Layout B (legacy):   Blackwell (GPU 0) → LLM + TTS, 5070 Ti (GPU 1) → STT
Current setup:       Blackwell (GPU 0) → LLM + TTS + STT

This script:
  1. Patches config.yaml to Layout B (TTS gpu_id:0, STT gpu_id:1)
  2. Restarts TTS + STT services
  3. Runs the benchmark (LLM + TTS timing on 10 phrases)
  4. Restores config.yaml to Layout A
  5. Restarts TTS + STT services
  6. Runs the same benchmark again
  7. Draws a side-by-side comparison chart

Usage:
    cd parrot-assistant
    source .venv/bin/activate
    python scripts/benchmark_gpu_layout.py
    python scripts/benchmark_gpu_layout.py -n 5        # fewer phrases (faster)
    python scripts/benchmark_gpu_layout.py --layout-b   # only run layout B
    python scripts/benchmark_gpu_layout.py --layout-a   # only run layout A
"""

import argparse
import copy
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
START_SH = ROOT / "start.sh"

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
]


def load_config():
    return yaml.safe_load(CONFIG_PATH.read_text())


def save_config(cfg):
    CONFIG_PATH.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))


def restart_services():
    """Stop all, then start all services."""
    subprocess.run(["bash", str(START_SH), "stop"], capture_output=True)
    time.sleep(2)
    subprocess.run(["bash", str(START_SH), "all"], capture_output=True)


def wait_for_services(timeout=90):
    """Poll /health on bridge until all services are up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get("http://127.0.0.1:8000/health", timeout=5)
            data = r.json()
            svc = data.get("services", {})
            if svc.get("tts") == "ok" and svc.get("llm") == "ok":
                return True
        except Exception:
            pass
        time.sleep(3)
    return False


def query_llm(prompt: str, cfg) -> tuple[str, float]:
    llm_url = cfg["bridge"]["llm_url"]
    model = cfg["llm"]["model"]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply in 1-3 short sentences."},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.7, "num_predict": 256},
    }
    t0 = time.perf_counter()
    resp = httpx.post(f"{llm_url}/api/chat", json=body, timeout=120)
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    text = resp.json().get("message", {}).get("content", "")
    return text, elapsed


def query_tts(text: str, cfg) -> tuple[int, float]:
    tts_port = cfg["tts"]["port"]
    t0 = time.perf_counter()
    resp = httpx.post(f"http://127.0.0.1:{tts_port}/synthesize", json={"text": text}, timeout=120)
    elapsed = time.perf_counter() - t0
    resp.raise_for_status()
    return len(resp.content), elapsed


def warmup(cfg):
    """Send one throwaway request to warm up LLM + TTS."""
    print("  Warming up LLM + TTS...")
    reply, _ = query_llm("Say hi.", cfg)
    query_tts(reply or "Hello world.", cfg)
    print("  Warm-up done.\n")


def run_benchmark(n: int, cfg, label: str) -> list[dict]:
    phrases = random.sample(PHRASES, min(n, len(PHRASES)))
    if n > len(PHRASES):
        phrases += random.choices(PHRASES, k=n - len(PHRASES))

    results = []
    for i, phrase in enumerate(phrases, 1):
        print(f"  [{label}] {i}/{n}: {phrase}")

        reply, llm_sec = query_llm(phrase, cfg)
        reply_short = reply.replace("\n", " ")[:70]
        print(f"           LLM {llm_sec:5.2f}s  → {reply_short}…")

        wav_bytes, tts_sec = query_tts(reply, cfg)
        wav_kb = wav_bytes // 1024
        print(f"           TTS {tts_sec:5.2f}s  → {wav_kb} KB")

        results.append({
            "phrase": phrase[:50],
            "llm_sec": llm_sec,
            "tts_sec": tts_sec,
            "total": llm_sec + tts_sec,
            "reply_len": len(reply),
            "wav_kb": wav_kb,
        })

    return results


def draw_comparison(results_a, results_b, out_path: Path):
    n = len(results_a)

    fig, axes = plt.subplots(1, 2, figsize=(16, max(4, n * 0.55)), sharey=True)

    for ax, results, title, colors in [
        (axes[0], results_a, "Layout A: Blackwell→LLM, 5070→TTS", ("#4C78A8", "#E45756")),
        (axes[1], results_b, "Layout B: Blackwell→LLM+TTS, 5070→STT", ("#54A24B", "#EECA3B")),
    ]:
        labels = [f"#{i+1} {r['phrase'][:35]}…" for i, r in enumerate(results)]
        llm = [r["llm_sec"] for r in results]
        tts = [r["tts_sec"] for r in results]

        y = range(n)
        ax.barh(y, llm, height=0.45, color=colors[0], label="LLM")
        ax.barh(y, tts, left=llm, height=0.45, color=colors[1], label="TTS")

        for i in range(n):
            ax.text(llm[i] + tts[i] + 0.08, i, f"{llm[i]+tts[i]:.1f}s", va="center", fontsize=7, color="#333")

        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel("Time (seconds)")
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.legend(loc="lower right", fontsize=8)
        ax.grid(axis="x", alpha=0.3)

    fig.suptitle("GPU Layout A vs B — Latency Comparison", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"\nChart saved → {out_path}")


def draw_summary_bars(results_a, results_b, out_path: Path):
    """Draw a grouped bar chart: mean LLM + mean TTS for both layouts."""
    def means(r):
        llm = sum(x["llm_sec"] for x in r) / len(r)
        tts = sum(x["tts_sec"] for x in r) / len(r)
        return llm, tts

    a_llm, a_tts = means(results_a)
    b_llm, b_tts = means(results_b)

    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.array([0, 1])
    w = 0.3

    ax.bar(x - w/2, [a_llm, b_llm], w, label="LLM (mean)", color=["#4C78A8", "#54A24B"])
    ax.bar(x + w/2, [a_tts, b_tts], w, label="TTS (mean)", color=["#E45756", "#EECA3B"])

    for i, (l, t) in enumerate([(a_llm, a_tts), (b_llm, b_tts)]):
        ax.text(i - w/2, l + 0.05, f"{l:.2f}s", ha="center", fontsize=8)
        ax.text(i + w/2, t + 0.05, f"{t:.2f}s", ha="center", fontsize=8)

    ax.set_xticks(x)
    ax.set_xticklabels(["Layout A\nBlackwell→LLM\n5070→TTS", "Layout B\nBlackwell→LLM+TTS\n5070→STT"], fontsize=9)
    ax.set_ylabel("Mean time (seconds)")
    ax.set_title("Average Latency: Layout A vs Layout B")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    summary_path = out_path.with_name("benchmark_gpu_summary.png")
    fig.savefig(summary_path, dpi=150)
    plt.close(fig)
    print(f"Summary chart saved → {summary_path}")


def print_summary(label, results):
    llm = [r["llm_sec"] for r in results]
    tts = [r["tts_sec"] for r in results]
    total = [r["total"] for r in results]
    print(f"\n  {label}:")
    print(f"    {'':>8}  {'LLM':>8}  {'TTS':>8}  {'Total':>8}")
    print(f"    {'Mean':>8}  {sum(llm)/len(llm):>7.2f}s  {sum(tts)/len(tts):>7.2f}s  {sum(total)/len(total):>7.2f}s")
    print(f"    {'Min':>8}  {min(llm):>7.2f}s  {min(tts):>7.2f}s  {min(total):>7.2f}s")
    print(f"    {'Max':>8}  {max(llm):>7.2f}s  {max(tts):>7.2f}s  {max(total):>7.2f}s")


def main():
    parser = argparse.ArgumentParser(description="A/B GPU layout benchmark")
    parser.add_argument("-n", type=int, default=10, help="phrases per layout (default 10)")
    parser.add_argument("--layout-a", action="store_true", help="only run layout A")
    parser.add_argument("--layout-b", action="store_true", help="only run layout B")
    parser.add_argument("-o", "--output", type=str, default=None)
    args = parser.parse_args()

    run_a = not args.layout_b or args.layout_a
    run_b = not args.layout_a or args.layout_b
    out = Path(args.output) if args.output else ROOT / "scripts" / "benchmark_gpu_layout.png"

    original_config = load_config()
    random.seed(42)

    # ── Layout A (current): Blackwell→LLM, 5070 Ti→TTS ──────────────
    results_a = []
    if run_a:
        print("=" * 60)
        print("LAYOUT A: Blackwell (GPU 0) → LLM,  5070 Ti (GPU 1) → TTS")
        print("  stt.gpu_id=0, tts.gpu_id=1")
        print("=" * 60)

        cfg_a = copy.deepcopy(original_config)
        cfg_a["stt"]["gpu_id"] = 0
        cfg_a["tts"]["gpu_id"] = 1
        save_config(cfg_a)

        print("  Restarting services...")
        restart_services()
        if not wait_for_services():
            print("  ERROR: services did not come up in time.")
            save_config(original_config)
            sys.exit(1)
        print("  Services ready.\n")

        warmup(cfg_a)
        random.seed(42)
        results_a = run_benchmark(args.n, cfg_a, "A")
        print_summary("Layout A", results_a)

    # ── Layout B (proposed): Blackwell→LLM+TTS, 5070 Ti→STT ─────────
    results_b = []
    if run_b:
        print("\n" + "=" * 60)
        print("LAYOUT B: Blackwell (GPU 0) → LLM+TTS,  5070 Ti (GPU 1) → STT")
        print("  stt.gpu_id=1, tts.gpu_id=0")
        print("=" * 60)

        cfg_b = copy.deepcopy(original_config)
        cfg_b["stt"]["gpu_id"] = 1
        cfg_b["tts"]["gpu_id"] = 0
        save_config(cfg_b)

        print("  Restarting services...")
        restart_services()
        if not wait_for_services():
            print("  ERROR: services did not come up in time.")
            save_config(original_config)
            sys.exit(1)
        print("  Services ready.\n")

        warmup(cfg_b)
        random.seed(42)
        results_b = run_benchmark(args.n, cfg_b, "B")
        print_summary("Layout B", results_b)

    # ── Restore original config ──────────────────────────────────────
    print("\n  Restoring original config and restarting...")
    save_config(original_config)
    restart_services()
    wait_for_services()

    # ── Charts ────────────────────────────────────────────────────────
    if results_a and results_b:
        draw_comparison(results_a, results_b, out)
        draw_summary_bars(results_a, results_b, out)
        print("\n" + "=" * 60)
        print("COMPARISON")
        print_summary("Layout A (Blackwell→LLM, 5070→TTS)", results_a)
        print_summary("Layout B (Blackwell→LLM+TTS, 5070→STT)", results_b)

        a_mean = sum(r["total"] for r in results_a) / len(results_a)
        b_mean = sum(r["total"] for r in results_b) / len(results_b)
        faster = "B" if b_mean < a_mean else "A"
        diff = abs(a_mean - b_mean)
        pct = diff / max(a_mean, b_mean) * 100
        print(f"\n  → Layout {faster} is faster by {diff:.2f}s ({pct:.1f}%) on average.")
        print("=" * 60)
    elif results_a:
        from scripts.benchmark_latency import draw_chart
        draw_chart(results_a, out)
    elif results_b:
        from scripts.benchmark_latency import draw_chart
        draw_chart(results_b, out)

    # Save raw JSON for later analysis
    json_path = out.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump({"layout_a": results_a, "layout_b": results_b}, f, indent=2)
    print(f"Raw data saved → {json_path}")


if __name__ == "__main__":
    main()
