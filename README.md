# project mocha

Mocha is a real-time AI character that cheers you up and keeps you informed. She
has a 3D body (VRM), speaks with a cloned voice, shows emotions, and picks
gestures to match what she's saying. Talk to her by voice or text from a browser,
Telegram, Discord, or a CLI.

She lives **inside the opus trading desk** (ProjectCorvus) as a morale companion,
with a button that leads into the (password-gated) trading desk. Mocha is a
single agent — she calls data, weather, news, and the desk's read-only
trading-briefing tools directly. (The desk dashboard no longer embeds her landing/
gadget — that was removed 2026-07-01 — so reach her at her own web app, below.)

> Migrated from the former *ProjectParrot*. The Nori (research), Shiro (coaching),
> and Hana (design-critic) sub-agents were removed; the hand-rolled turn loop was
> rebuilt as a **LangGraph** graph; and Mocha now runs her own dedicated fast
> vLLM (Qwen3-8B-FP8 @ `:8893`) and routes only deep/tool-heavy turns to opus
> trading's shared 32B.

## Architecture

```
Mocha web app (Three.js + VRM, :8080)   ◄─ front door (own app; no longer
   │  (single-origin reverse proxy)         embedded by the desk dashboard)
   ▼
                       Bridge (FastAPI :8090)
   ┌──────────────────────────────────────────────────────┐
   │  LangGraph StateGraph  (bridge/graph.py)              │
   │   build_messages → llm_pass → log_pass               │
   │        ▲                         │                    │
   │        └──── run_tools ◄─────────┤ {tools | finalize} │
   │  inline-tag streaming parser + tool ReAct (≤5 rounds) │
   │  memory (mem0 + ChromaDB) · call log (PostgreSQL)     │
   └─────────┬───────────────────────┬────────────────────┘
             │                        │
   STT :8091 (Faster-Whisper)   TTS :8092 (F5-TTS)
             │
   LLM (dual-model):
     · fast/primary — Mocha's OWN vLLM, Qwen3-8B-FP8, :8893/v1
     · deep/routing — shared opus-trading vLLM, Qwen3-32B-FP8, :8000/v1
        both behind a rate-limit + circuit-breaker isolation guard
```

**Prime directive:** a Mocha failure must never impact Corvus trading ops. Mocha
runs as its own process; the dashboard never imports Mocha code; her primary
inference is fully isolated on her own vLLM (`:8893`) and her deep-routing calls
to the shared desk vLLM (`:8000`) are rate-limited + circuit-broken; she uses her
own `mocha` Postgres DB with a bounded pool; and her opus access is a read-only,
timed-out, Postgres-only subprocess proxy.

## Tech stack

| Layer | Tech |
|-------|------|
| Orchestration | **LangGraph** `StateGraph` (`bridge/graph.py`) |
| LLM | dual-model: own vLLM `Qwen/Qwen3-8B-FP8` (`:8893`, fast/primary) + shared opus-trading vLLM `Qwen/Qwen3-32B-FP8` (`:8000`, deep-routing) |
| STT | Faster-Whisper large-v3 (`:8091`) |
| TTS | F5-TTS zero-shot voice cloning (`:8092`) |
| Memory | mem0 + ChromaDB |
| Call log | PostgreSQL (`mocha` DB) |
| Frontend | Three.js, VRM loader, AudioWorklet (`:8080`) |
| Channels | WebSocket (web), Telegram, Discord, CLI |
| Auth | JWT (querystring + localStorage) |

## Prerequisites

- Python 3.11+
- Mocha's own vLLM on `:8893` (Qwen3-8B-FP8; `project-mocha-vllm.service`), plus
  opus trading's shared vLLM on `:8000` for deep-routing — see `config.yaml:llm`
- A CUDA GPU with headroom for STT + TTS + Mocha's 8B alongside opus's stack
- PostgreSQL with a `mocha` DB (DSN: `postgresql://mocha:5369@127.0.0.1:5432/mocha`)

## Running

The trading desk runs everything as always-on systemd services, so the simplest
setup makes Mocha one too.

**Recommended — always-on (one-time install):**
```bash
sudo scripts/install-service.sh     # installs + enables + starts project-mocha.service
```
That's it. Mocha now starts on boot and auto-restarts. (First boot runs
`setup.sh` automatically if the venv is missing.) Manage with
`systemctl {start,stop,restart,status} project-mocha`; uninstall with
`sudo scripts/install-service.sh remove`.

**Manual — one command (no systemd):**
```bash
./start.sh            # auto-runs setup.sh if needed, then starts everything
```
`start.sh` is now self-sufficient — it builds the venv on first run, starts
STT + TTS + Bridge + Web, waits for health, and prints where to meet Mocha.

Other subcommands: `./start.sh {bridge|web|stop|status|restart}`.
Logs: `logs/<service>.log`   ·   PIDs: `.pids/<service>.pid`

Either way, open Mocha at her own web app: **http://localhost:8080** (or your
cloudflared `mocha` tunnel). The desk dashboard no longer embeds her.

## Configuration

`config.yaml` is the single source of truth for ports, the dual-model LLM
endpoints (own `:8893` fast + shared `:8000` deep) and their isolation guards
(`llm.isolation`), tool allowlist, memory, and channels. Services read it on
startup.

`character/soul.md` and `character/behaviors.yaml` are hot-reloaded on every LLM
call — edit them and changes take effect immediately, no restart.

## Project layout

```
project_mocha/
├── bridge/
│   ├── server.py          # FastAPI HTTP/WS; _run_inline_turn = thin graph wrapper
│   ├── graph.py           # LangGraph StateGraph — the conversational loop
│   ├── graph_state.py     # TurnState
│   ├── llm_client.py      # LLM client (fast :8893 + deep :8000) + isolation guard (rate-limit/breaker)
│   ├── inline_route.py    # streaming inline-tag → speech/emotion/gesture events
│   └── call_log.py        # PostgreSQL logging (bounded pool)
├── character/             # soul.md, behaviors.yaml, emotions.yaml, animations csv
├── stt/                   # Faster-Whisper service (:8091)
├── tts/                   # F5-TTS service (:8092)
├── web/                   # Browser VRM app (:8080); GET /gadget = stable embed route (no longer used by the desk)
├── tools/
│   ├── custom/            # data/UI tools + opus read-only proxies
│   └── executor.py        # tool dispatch + handle resolution
├── memory/                # mem0 + ChromaDB store
├── config.yaml            # master config
└── start.sh               # service launcher
```

## Customization

- **Personality** — edit `character/soul.md` (immediate).
- **Behavior rules** — edit `character/behaviors.yaml` (immediate).
- **Voice** — replace `audio/reference_voice.wav`, restart TTS.
- **3D model** — drop a `.vrm` into `web/static/` and point the web config at it.
- **New tools** — add a file to `tools/custom/` exporting `TOOL_DEF` +
  `async def execute(args) -> str`, then add its name to `config.yaml:tools.allowed`.
  Hot-reload: `POST http://127.0.0.1:8090/admin/reload-tools`.
