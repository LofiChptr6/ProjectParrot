# Parrot Assistant — 3D Character with Voice

A local-first personal assistant with speech-to-text, LLM reasoning, text-to-speech,
long-term memory, and a 3D animated character.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Unity (Windows)                          │
│         VRM Character · Lip Sync · Animations              │
│                        ↕ WebSocket                         │
├─────────────────────────────────────────────────────────────┤
│                Bridge / Orchestrator (:8000)                │
│         Connects all services · Manages sessions           │
├──────────┬──────────┬──────────────┬────────────────────────┤
│ STT      │ TTS      │ Memory       │ LLM                   │
│ Whisper  │ F5-TTS   │ ChromaDB     │ Ollama                │
│ :8001    │ :8002    │ :8003        │ :11434                │
└──────────┴──────────┴──────────────┴────────────────────────┘
    GPU 0       GPU 0      CPU            GPU 0
  (PRO 6000)  (PRO 6000)               (PRO 6000)
```

## Data Flow

```
Mic → Whisper STT → text
                      ↓
              ChromaDB query (relevant memories)
                      ↓
              Ollama LLM (text + memories → response)
                      ↓
              F5-TTS (response → audio)
                      ↓
              Bridge → Unity WebSocket
                      ↓
              3D Character speaks (lip sync + animation)
```

## Hardware

| Component | Spec |
|-----------|------|
| GPU 0 | NVIDIA RTX PRO 6000 Blackwell (98 GB) — all inference |
| GPU 1 | NVIDIA GeForce RTX 5070 Ti (16 GB) — not used (PCIe bottleneck) |
| RAM | 30 GB |
| OS | WSL2 (Linux) + Windows |

## Quick Start

### 1. Create virtual environment

```bash
cd ~/ProjectParrot/parrot-assistant
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install PyTorch with CUDA

For Blackwell GPUs (RTX 50-series, RTX PRO 6000) — needs nightly with CUDA 12.8:
```bash
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
```

For older GPUs (RTX 40-series and below):
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add a reference voice

Place a clean 6-15 second WAV recording at:
```
audio/reference_voice.wav
```
This is used by XTTS v2 for voice cloning. Clear speech, minimal background noise.

### 5. Make sure Ollama is running

```bash
# If using Docker (existing setup):
docker compose up ollama -d

# Or native:
ollama serve
```

Pull a model if you haven't:
```bash
ollama pull qwen3:30b-a3b
```

### 6. Start all services

```bash
chmod +x start.sh
./start.sh all
```

### 7. Check health

```bash
curl http://localhost:8000/health
```

### 8. Test the chat endpoint

```bash
# Text chat (returns JSON)
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello, who are you?", "include_audio": false}'

# Voice pipeline (send audio, get audio back)
curl -X POST http://localhost:8000/voice \
  -F "file=@test_audio.wav" \
  --output response.wav
```

## Service Details

### STT — Whisper large-v3 (`:8001`)
- Engine: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- REST: `POST /transcribe` (upload WAV)
- WebSocket: `WS /ws/stream` (real-time streaming)

### TTS — F5-TTS (`:8002`)
- Engine: [F5-TTS](https://github.com/SWivid/F5-TTS) (replaces coqui-ai/TTS which doesn't support Python 3.12+)
- Zero-shot voice cloning from a 6-15s reference clip
- REST: `POST /synthesize`

### Memory — ChromaDB (`:8003`)
- Vector similarity search over conversation history
- REST: `POST /store`, `POST /query`, `GET /recent`

### Bridge (`:8000`)
- Orchestrates the full pipeline
- REST: `POST /chat`, `POST /voice`
- WebSocket: `WS /ws/unity` (3D client), `WS /ws/voice-stream` (real-time voice)

## Unity 3D Character

See [unity/SETUP.md](unity/SETUP.md) for full Unity project setup instructions.

Short version:
1. Install Unity 2022.3+ on Windows
2. Import UniVRM + a VRM model
3. Add the WebSocket bridge script
4. Connect to `ws://localhost:8000/ws/unity`

## Future Upgrades

- **GPT-SoVITS** — Train a custom character voice (better than zero-shot cloning)
- **RVC** — Real-time voice conversion for even more natural output
- **vLLM** — Replace Ollama for faster batched inference
- **LoRA fine-tuning** — Customize model personality beyond prompting
- **NVIDIA Riva** — Production-grade STT if Whisper latency becomes an issue
- **Unreal Engine** — Higher fidelity 3D if Unity isn't enough
- **Fish-Speech** — Alternative TTS with emotion/prosody control via natural language tags

## Directory Structure

```
parrot-assistant/
├── audio/              ← Reference voice clips
├── bridge/
│   └── server.py       ← Orchestrator (FastAPI)
├── memory/
│   ├── service.py      ← ChromaDB service
│   └── chroma_db/      ← Persisted vector DB (created at runtime)
├── models/             ← Shared model cache
├── stt/
│   └── service.py      ← Whisper STT service
├── tts/
│   └── service.py      ← XTTS v2 TTS service
├── unity/
│   └── SETUP.md        ← Unity project setup guide
├── logs/               ← Service logs (created at runtime)
├── config.yaml         ← Central configuration
├── requirements.txt    ← Python dependencies
├── start.sh            ← Service launcher
└── README.md           ← This file
```
