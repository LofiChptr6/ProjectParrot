#!/usr/bin/env bash
set -euo pipefail

PRO6000_GPU_INDEX=0
PRO6000_UUID="GPU-c800e834-4456-587f-6078-3402485acd47"

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }

cd "$(dirname "$0")"

# ── 1. Verify GPU visibility ────────────────────────────────────────
green "==> Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y zstd curl

if ! command -v nvidia-smi &>/dev/null; then
    yellow "nvidia-smi not found. Installing nvidia-utils-570…"
    sudo apt-get install -y nvidia-utils-570
fi

green "==> Checking NVIDIA GPUs…"
nvidia-smi -L
echo

# ── 2. Install Ollama ───────────────────────────────────────────────
green "==> Installing Ollama…"
if command -v ollama &>/dev/null; then
    yellow "Ollama already installed: $(ollama --version)"
else
    curl -fsSL https://ollama.com/install.sh | sh
fi
echo

# ── 3. Pin Ollama to GPU 0 (RTX Pro 6000) ──────────────────────────
green "==> Pinning Ollama service to GPU ${PRO6000_GPU_INDEX} (${PRO6000_UUID})…"
OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/gpu-pin.conf"

sudo mkdir -p "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_FILE" > /dev/null <<EOF
[Service]
Environment=CUDA_VISIBLE_DEVICES=${PRO6000_GPU_INDEX}
EOF

sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl restart ollama

sleep 2
if systemctl is-active --quiet ollama; then
    green "  Ollama service is running (GPU ${PRO6000_GPU_INDEX} only)."
else
    red "  Warning: Ollama service failed to start. Check: journalctl -u ollama"
fi
echo

# ── 4. Pull models (48 GB VRAM budget) ─────────────────────────────
green "==> Pulling recommended models for 48 GB VRAM…"

yellow "  [1/2] qwen3:30b-a3b  (MoE — 30B total / 3B activated, ~20 GB, think + no-think modes)"
ollama pull qwen3:30b-a3b

yellow "  [2/2] qwen3:32b  (dense, ~22 GB, strong reasoning fallback)"
ollama pull qwen3:32b

echo
green "  Installed models:"
ollama list
echo

# ── 5. Quick smoke test ─────────────────────────────────────────────
green "==> Smoke-testing qwen3:30b-a3b…"
ollama run qwen3:30b-a3b "Respond with exactly: Hello from ProjectParrot!" --verbose 2>&1 | head -20
echo

# ── 6. Prepare OpenClaw dirs + .env ─────────────────────────────────
green "==> Preparing OpenClaw directories…"
mkdir -p .openclaw openclaw-workspace

if [ ! -f .env ]; then
    cp .env.example .env
    yellow "  Created .env from .env.example — edit it if you need hosted API keys."
else
    yellow "  .env already exists, skipping."
fi
echo

# ── 7. Start OpenClaw container ─────────────────────────────────────
green "==> Starting OpenClaw container…"
if ! command -v docker &>/dev/null; then
    red "  Docker not found — install Docker Engine or Docker Desktop first."
    exit 1
fi

docker compose pull
docker compose up -d

sleep 3
if docker ps --format '{{.Names}}' | grep -q '^openclaw$'; then
    green "  OpenClaw is running. UI: http://localhost:3000"
else
    yellow "  Container may still be starting. Check: docker compose logs -f openclaw"
fi
echo

# ── Done ────────────────────────────────────────────────────────────
green "╔══════════════════════════════════════════════════════════════╗"
green "║  ProjectParrot setup complete!                              ║"
green "║                                                             ║"
green "║  Ollama:    http://localhost:11434  (GPU 0 / Pro 6000)      ║"
green "║  OpenClaw:  http://localhost:3000                           ║"
green "║                                                             ║"
green "║  Try:  ollama run qwen2.5:32b \"Hello!\"                      ║"
green "╚══════════════════════════════════════════════════════════════╝"
