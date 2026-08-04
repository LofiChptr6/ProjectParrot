# Latency benchmarks — 2026-08-04 (post model-consolidation)

Box: HomePCBlackwell (RTX PRO 6000 Blackwell, 96GB). GPU otherwise idle.
Model: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` on vLLM 0.20.1, `:8893`,
`--gpu-memory-utilization 0.42 --max-model-len 16384` → 41.3GB VRAM used,
56GB free. Serves BOTH lanes (fast chat + deep tool mode).

## LLM (direct vLLM, streaming)

| case | TTFT | total | decode |
|---|---|---|---|
| first call after server start | 17.5s (one-time CUDA graph capture) | — | — |
| warm, short prompt | **23–62 ms** | 167 ms / 27 tok | 187 tok/s |
| warm, longer generation | 315 ms | 887 ms / 119 tok | **208 tok/s** |

Reference (from llm_call_log, June–Jul): Qwen3-8B-FP8 TTFT p50 77 ms,
~100–150 tok/s; Qwen3-32B-FP8 TTFT p50 387 ms / p90 2.4 s, turn p50 4.8 s /
p90 19 s; deepseek-v3 remote 67–90 s avg. The MoE decodes faster than the old
8B at 32B-class quality.

## Full turns through the bridge (`POST /channel`, cli)

| turn | wall clock | LLM passes (from llm_call_log) |
|---|---|---|
| casual ("you there?") | **< 0.9 s** | interpret 229 ms → pass1 227 ms (TTFT 42 ms) → verify 208 ms |
| tool ("whats AAPL trading at") | **3.6 s** | interpret 592 ms → tool-emit 561 ms (TTFT 326 ms) → yfinance fetch → synthesis 477 ms (TTFT 62 ms) → verify 310 ms |

All passes local; `interpret` is now logged (`triggered_by='interpret'`).

## TTS (F5, `/synthesize`, warm)

| text length | nfe_step 32 (default) | nfe_step 16 |
|---|---|---|
| short (44 ch) | ~520–1240 ms* | **466 ms** |
| medium (120 ch) | 523 ms | 522 ms |
| long (235 ch) | 1097 ms | — |

*first synth after service start includes warm-up.

`tts.nfe_step: 16` roughly halves short-line synthesis at some audio-quality
cost — samples for ear-judgment were generated during this run. Default ships
at 32 (stock quality); flip the config knob and `./start.sh tts` to compare.

## STT

`large-v3-turbo` loaded clean (fallback to large-v3 untested-needed), beam 2,
internal VAD off. Not re-benchmarked end-to-end (needs a live mic session);
expected ~100–300 ms per short utterance vs 300–900 ms for the old
large-v3/beam-5/double-VAD config.
