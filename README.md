# ProjectParrot

Mocha is a real-time AI character that cheers you up and keeps you informed. She has a 3D body (VRM), speaks with a cloned voice, shows emotions, and picks gestures to match what she's saying. Talk to her by voice or text from a browser, Telegram, Discord, or a CLI.

Two sub-agents run behind the scenes: **Nori** handles research and data visualization (stocks, news, weather, slides), and **Hana** critiques UI design and extracts color palettes. Mocha presents their work as her own.

Everything runs locally on one GPU machine.

## Agents

| Agent | Role | Model | Visibility |
|-------|------|-------|-----------|
| **Mocha** | Conversational character — voice, emotions, gestures | Qwen3-32B (vLLM) | Front-facing |
| **Nori** | Research analyst — data, charts, narration | Qwen3-32B (vLLM) | Behind the scenes |
| **Hana** | Design critic — palettes, contrast, typography | Claude Haiku 4.5 | Behind the scenes |

## Architecture

```
Browser / Telegram / Discord / CLI
           │
           ▼
    Bridge (FastAPI :8000)
    ┌──────────────────────────────────────┐
    │  Complexity router → 2-pass LLM      │
    │  Tool executor (ReAct, up to 5 rds)  │
    │  Memory (mem0 + ChromaDB)            │
    │  Call logger (PostgreSQL)            │
    └──────────┬───────────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
  vLLM :8800      STT :8001  (Faster-Whisper large-v3)
  Qwen3-32B       TTS :8002  (F5-TTS, zero-shot voice clone)
       │
       ▼
  Web app :8080
  Three.js + VRM + AudioWorklet
  (lip-sync, blend shapes, animation retargeting)
```

## Tech stack

| Layer | Tech |
|-------|------|
| LLM | vLLM serving Qwen3-32B (fp8, GPU 0) |
| STT | Faster-Whisper large-v3 |
| TTS | F5-TTS zero-shot voice cloning |
| Memory | mem0 + ChromaDB (semantic search + fact extraction) |
| Call log | PostgreSQL |
| Frontend | Three.js, VRM loader, AudioWorklet |
| Channels | WebSocket (web), Telegram, Discord, CLI |
| Auth | JWT |

## Prerequisites

- Python 3.11+
- CUDA GPU with enough VRAM for Qwen3-32B fp8 (~24 GB)
- Docker + docker-compose (for the vLLM container)
- PostgreSQL running locally (DSN: `postgresql://mocha:5369@127.0.0.1:5432/mocha`)

## Quick start

```bash
cp .env.example .env        # fill in: ANTHROPIC_API_KEY, POLYGON_API_KEY,
                            # BRAVE_API_KEY, Telegram/Discord bot tokens
./setup.sh                  # create .venv, install deps, start vLLM container
./start.sh all              # STT + TTS + Bridge + Web
```

Open http://localhost:8080

## Service management

```bash
./start.sh all              # start everything
./start.sh bridge           # bridge + web only (most common during dev)
./start.sh stt              # Faster-Whisper STT service
./start.sh tts              # F5-TTS service
./start.sh web              # web dashboard only
./start.sh stop             # stop all
./start.sh restart          # stop + start all
./start.sh status           # show what's running
```

Logs: `logs/<service>.log`  
PIDs: `.pids/<service>.pid`

## Configuration

`config.yaml` is the single source of truth for all service ports, LLM params, complexity routing thresholds, channel tokens, memory settings, autonomy/idle behavior, and per-user quotas. Services read it on startup.

`character/soul.md` and `character/behaviors.yaml` are hot-reloaded on every LLM call — edit them and changes take effect immediately, no restart.

## Project layout

```
ProjectParrot/
├── bridge/          # Central orchestrator (FastAPI :8000)
│   ├── server.py    # Endpoints, WebSocket, tool loop
│   ├── llm_client.py
│   └── call_log.py  # PostgreSQL logging
├── character/
│   ├── soul.md      # Mocha's identity (hot-reloaded)
│   ├── behaviors.yaml
│   ├── emotions.yaml
│   └── animation_functions.csv   # 76 gesture clips
├── nori/            # Research sub-agent
├── hana/            # Design critic sub-agent
├── stt/             # Faster-Whisper service (:8001)
├── tts/             # F5-TTS service (:8002)
├── web/             # Browser dashboard (:8080)
│   └── static/js/animation-controller.js
├── tools/
│   ├── custom/      # Data tools (stocks, news, weather, …)
│   └── executor.py  # ReAct loop
├── memory/          # mem0 + ChromaDB store
├── channels/        # Telegram, Discord, CLI bots
├── auth/            # JWT helpers
├── config.yaml      # Master config
├── start.sh         # Service launcher
└── docker-compose.yml   # vLLM + gesture service containers
```

## Customization

**Personality** — edit `character/soul.md` (takes effect immediately).

**Behavior rules** — edit `character/behaviors.yaml` (also hot-reloaded).

**Voice** — replace `audio/reference_voice.wav` with any clean mono recording, then restart TTS.

**3D model** — drop a `.vrm` file into `web/static/` and update the model path in the web config.

**New tools** — add a file to `tools/custom/`. It must export:

```python
TOOL_DEF = { "type": "function", "function": { "name": "...", ... } }

async def execute(arguments: dict) -> str: ...
```

Hot-reload without restart: `POST http://127.0.0.1:8000/admin/reload-tools`
