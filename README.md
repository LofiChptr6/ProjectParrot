# ProjectParrot

Local AI playground — Ollama (GPU) + OpenClaw agent in Docker.

## Hardware

| Slot | GPU | VRAM | Role |
|------|-----|------|------|
| 0 | NVIDIA RTX PRO 6000 Blackwell | 48 GB | **This project** (Ollama LLM inference) |
| 1 | NVIDIA GeForce RTX 5070 Ti | 16 GB | Available for other work |

Ollama is pinned to **GPU 0 only** via `CUDA_VISIBLE_DEVICES=0`.

## Quick start (one command)

```bash
chmod +x setup.sh scripts/*.sh
./setup.sh
```

This will:
1. Verify GPU visibility (`nvidia-smi`)
2. Install **Ollama** and pin it to GPU 0
3. Pull models sized for 48 GB VRAM:
   - `qwen3:30b-a3b` — MoE, 30B total / 3B activated, think + no-think modes (~20 GB)
   - `qwen3:32b` — dense, strong reasoning fallback (~22 GB)
4. Create `.env` from `.env.example`
5. Start the **OpenClaw** Docker container

After setup:
- **Ollama API**: http://127.0.0.1:11434 (local to this machine)
- **Parrot Assistant**: `cd parrot-assistant && ./start.sh all`

## Manual steps (if you prefer)

### Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

### Pin to GPU 0

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/gpu-pin.conf <<'EOF'
[Service]
Environment=CUDA_VISIBLE_DEVICES=0
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Or run manually: `CUDA_VISIBLE_DEVICES=0 ollama serve`

### Pull models

```bash
ollama pull qwen2.5:32b
ollama pull qwen2.5-coder:32b
ollama pull qwen2.5:14b
```

### Start OpenClaw

```bash
cp .env.example .env        # edit API keys if needed
mkdir -p .openclaw openclaw-workspace
docker compose up -d
```

## Repo structure

```
ProjectParrot/
├── setup.sh                  # One-shot setup (run this first)
├── docker-compose.yml        # OpenClaw container definition
├── .env.example              # Template — copied to .env
├── .gitignore                # Keeps secrets + local state out of git
├── scripts/
│   ├── ollama-gpu0.sh        # Manual Ollama launcher (GPU 0 only)
│   └── status.sh             # Health check for the full stack
├── .openclaw/                # (gitignored) OpenClaw config + memory
└── openclaw-workspace/       # (gitignored) Agent working directory
```

## Pointing at local Ollama

Ollama runs natively on this machine. `.env` sets:

```
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
```

No Docker networking (`host.docker.internal`) needed for native Ollama.

## Health check

```bash
./scripts/status.sh
```

Shows GPU usage, Ollama status, Docker container state, and API connectivity.

## Workflow: public data → analysis → apps → GitHub

1. Put datasets, notebooks, or scripts in `openclaw-workspace/` so the agent can read/write them.
2. Keep secrets in `.env` (gitignored).
3. Commit code and non-secret artifacts:

```bash
git add -A
git commit -m "your message"
git push
```
