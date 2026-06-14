# project mocha

Mocha is a real-time AI character that cheers you up and keeps you informed. She
has a 3D body (VRM), speaks with a cloned voice, shows emotions, and picks
gestures to match what she's saying. Talk to her by voice or text from a browser,
Telegram, Discord, or a CLI.

She lives **inside the opus trading desk** (ProjectCorvus) as a morale companion:
the dashboard opens on Mocha, and a button leads into the (password-gated)
trading desk. Mocha is a single agent — she calls data, weather, news, and the
desk's read-only trading-briefing tools directly.

> Migrated from the former *ProjectParrot*. The Nori (research), Shiro (coaching),
> and Hana (design-critic) sub-agents were removed; the hand-rolled turn loop was
> rebuilt as a **LangGraph** graph; and Mocha now shares opus trading's vLLM
> instead of running her own.

## Architecture

```
opus trading dashboard (Streamlit :8501)
   └─ landing embeds  ─►  Mocha web app (Three.js + VRM, :8080)
                              │  (single-origin reverse proxy)
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
   LLM: shared opus-trading vLLM (Qwen3-32B-FP8, :8000/v1)
        behind a rate-limit + circuit-breaker isolation guard
```

**Prime directive:** a Mocha failure must never impact Corvus trading ops. Mocha
runs as its own process; the dashboard never imports Mocha code; her vLLM use is
rate-limited + circuit-broken; she uses her own `mocha` Postgres DB with a bounded
pool; and her opus access is a read-only, timed-out, Postgres-only subprocess
proxy.

## Tech stack

| Layer | Tech |
|-------|------|
| Orchestration | **LangGraph** `StateGraph` (`bridge/graph.py`) |
| LLM | shared opus-trading vLLM, `Qwen/Qwen3-32B-FP8` (OpenAI-compatible) |
| STT | Faster-Whisper large-v3 (`:8091`) |
| TTS | F5-TTS zero-shot voice cloning (`:8092`) |
| Memory | mem0 + ChromaDB |
| Call log | PostgreSQL (`mocha` DB) |
| Frontend | Three.js, VRM loader, AudioWorklet (`:8080`) |
| Channels | WebSocket (web), Telegram, Discord, CLI |
| Auth | JWT (querystring + localStorage) |

## Prerequisites

- Python 3.11+
- opus trading's vLLM running on `:8000` (Mocha reuses it — see `config.yaml:llm`)
- A CUDA GPU with headroom for STT + TTS alongside opus's stack
- PostgreSQL with a `mocha` DB (DSN: `postgresql://mocha:5369@127.0.0.1:5432/mocha`)

## Running

The trading desk runs everything as always-on systemd services (incl. the
dashboard on :8501), so the simplest setup makes Mocha one too.

**Recommended — always-on (one-time install):**
```bash
sudo scripts/install-service.sh     # installs + enables + starts project-mocha.service
```
That's it. Mocha now starts on boot and auto-restarts; the always-on dashboard
shows her landing page. (First boot runs `setup.sh` automatically if the venv is
missing.) Manage with `systemctl {start,stop,restart,status} project-mocha`;
uninstall with `sudo scripts/install-service.sh remove`.

> **One-time:** after deploying these changes, restart the dashboard once so it
> picks up the new Mocha landing: `sudo systemctl restart trading-dashboard`.

**Manual — one command (no systemd):**
```bash
./start.sh            # auto-runs setup.sh if needed, then starts everything
```
`start.sh` is now self-sufficient — it builds the venv on first run, starts
STT + TTS + Bridge + Web, waits for health, and prints where to meet Mocha.

Other subcommands: `./start.sh {bridge|web|stop|status|restart}`.
Logs: `logs/<service>.log`   ·   PIDs: `.pids/<service>.pid`

Either way, open Mocha at **http://127.0.0.1:8501** (the desk dashboard, or your
cloudflared `mocha` tunnel) — or the standalone web app at **http://localhost:8080**.

## Configuration

`config.yaml` is the single source of truth for ports, the shared-vLLM endpoint +
isolation guard (`llm.isolation`), tool allowlist, memory, and channels. Services
read it on startup.

`character/soul.md` and `character/behaviors.yaml` are hot-reloaded on every LLM
call — edit them and changes take effect immediately, no restart.

## Project layout

```
project_mocha/
├── bridge/
│   ├── server.py          # FastAPI HTTP/WS; _run_inline_turn = thin graph wrapper
│   ├── graph.py           # LangGraph StateGraph — the conversational loop
│   ├── graph_state.py     # TurnState
│   ├── llm_client.py      # shared-vLLM client + isolation guard (rate-limit/breaker)
│   ├── inline_route.py    # streaming inline-tag → speech/emotion/gesture events
│   └── call_log.py        # PostgreSQL logging (bounded pool)
├── character/             # soul.md, behaviors.yaml, emotions.yaml, animations csv
├── stt/                   # Faster-Whisper service (:8091)
├── tts/                   # F5-TTS service (:8092)
├── web/                   # Browser VRM app (:8080); GET /gadget = dashboard embed
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
