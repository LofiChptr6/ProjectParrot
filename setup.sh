#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# ProjectParrot — First-time setup
#
# GPU-heavy services (vLLM) run in Docker with explicit GPU reservation.
# Python services (STT, TTS, memory, animation, bridge) run natively.
# ---------------------------------------------------------------------------

PRO6000_GPU_INDEX=0

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }

cd "$(dirname "$0")"

# ── 1. System dependencies ─────────────────────────────────────────
green "==> Installing system dependencies…"
sudo dnf install -y zstd curl 2>/dev/null \
  || sudo apt-get update -qq && sudo apt-get install -y zstd curl 2>/dev/null \
  || yellow "  Could not install zstd/curl — install them manually if missing."

# ── 2. Verify GPU visibility ───────────────────────────────────────
green "==> Checking NVIDIA GPUs…"
if ! command -v nvidia-smi &>/dev/null; then
    red "  nvidia-smi not found."
    red "  Install the NVIDIA driver for your distro (e.g. akmod-nvidia on Fedora,"
    red "  nvidia-utils-570 on Ubuntu/Debian)."
    exit 1
fi
nvidia-smi -L
echo

# ── 3. Verify Docker + NVIDIA Container Toolkit ───────────────────
green "==> Checking Docker…"
if ! command -v docker &>/dev/null; then
    red "  Docker not found. Install Docker Engine first:"
    red "    https://docs.docker.com/engine/install/"
    exit 1
fi
green "  Docker: $(docker --version)"

green "==> Checking NVIDIA Container Toolkit…"
if ! docker info 2>/dev/null | grep -qi "nvidia"; then
    yellow "  NVIDIA runtime not detected in Docker."
    yellow "  Quick test: running a GPU container…"
    if docker run --rm --gpus device=0 nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi &>/dev/null; then
        green "  GPU access works despite missing 'nvidia' in docker info — OK."
    else
        red ""
        red "  Docker cannot access GPUs. Install NVIDIA Container Toolkit:"
        red ""
        red "  Fedora/RHEL:"
        red "    curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \\"
        red "      | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo"
        red "    sudo dnf install -y nvidia-container-toolkit"
        red "    sudo nvidia-ctk runtime configure --runtime=docker"
        red "    sudo systemctl restart docker"
        red ""
        red "  Ubuntu/Debian:"
        red "    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\"
        red "      | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
        red "    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\"
        red "      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\"
        red "      | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list"
        red "    sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit"
        red "    sudo nvidia-ctk runtime configure --runtime=docker"
        red "    sudo systemctl restart docker"
        red ""
        red "  Then re-run this script."
        exit 1
    fi
else
    green "  NVIDIA Container Toolkit: detected."
fi
echo

# ── 4. Disable native Ollama (if installed) ────────────────────────
if systemctl is-active --quiet ollama 2>/dev/null; then
    yellow "==> Stopping native Ollama systemd service (no longer needed)…"
    sudo systemctl stop ollama
    sudo systemctl disable ollama
    green "  Native Ollama disabled."
fi
echo

# ── 5. Start vLLM in Docker ──────────────────────────────────────
green "==> Starting vLLM in Docker (GPU ${PRO6000_GPU_INDEX})…"
docker compose pull
docker compose up -d

# Wait for health check (model download + load can take several minutes)
yellow "  Waiting for vLLM to become healthy (this may take a few minutes on first run)…"
for i in $(seq 1 120); do
    if curl -sf --connect-timeout 2 http://127.0.0.1:8800/health >/dev/null 2>&1; then
        green "  vLLM is up."
        break
    fi
    if [[ "$i" -eq 120 ]]; then
        red "  vLLM did not respond after 120s. Check: docker compose logs vllm"
        exit 1
    fi
    sleep 2
done
echo

# ── 6. Verify GPU is actually being used ──────────────────────────
green "==> Verifying GPU access inside container…"
if docker exec vllm nvidia-smi &>/dev/null; then
    green "  Container has GPU access:"
    docker exec vllm nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
else
    red "  WARNING: Container does NOT have GPU access!"
    red "  vLLM will fail to load models. Fix NVIDIA Container Toolkit and re-run."
fi
echo

# ── 7. Verify model is loaded ────────────────────────────────────
green "==> Checking loaded models…"
curl -s http://127.0.0.1:8800/v1/models | python3 -c "
import sys, json
data = json.load(sys.stdin)
for m in data.get('data', []):
    print(f'  Model: {m[\"id\"]}')
" 2>/dev/null || yellow "  Could not list models — vLLM may still be loading."
echo

# ── 8. Prepare .env ──────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    yellow "  Created .env from .env.example — set HF_TOKEN for gated models."
else
    yellow "  .env already exists, skipping."
fi
echo

# ── Done ──────────────────────────────────────────────────────────
green "╔══════════════════════════════════════════════════════════════╗"
green "║  ProjectParrot setup complete!                              ║"
green "║                                                             ║"
green "║  vLLM (Docker):  http://127.0.0.1:8800  (GPU ${PRO6000_GPU_INDEX})         ║"
green "║                                                             ║"
green "║  Next: ./start.sh all                                       ║"
green "╚══════════════════════════════════════════════════════════════╝"
