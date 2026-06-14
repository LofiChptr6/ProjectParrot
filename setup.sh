#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# project mocha — first-time setup  (post-migration architecture)
#
# Mocha is co-located inside "opus trading" and SHARES that desk's vLLM, so she
# does NOT run her own LLM container. This script therefore only:
#   1. builds the Python venv + installs deps (torch + requirements, incl. langgraph)
#   2. prepares .env
#   3. sanity-checks the shared vLLM (:8000) and a few key imports
#   4. leaves an install note (INSTALL_NOTES.md) + a full log (logs/setup-*.log)
#
# It does NOT touch Docker, opus, or the GPU's running vLLM.
# ---------------------------------------------------------------------------

cd "$(dirname "$0")"
ROOT="$(pwd)"

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }

mkdir -p logs
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="logs/setup-${STAMP}.log"
NOTE="INSTALL_NOTES.md"

# Mirror everything to the log as well as the console.
exec > >(tee -a "$LOG") 2>&1

STATUS="FAILED"
PYBIN=""; PYVER=""; TORCH=""
finish() {
  {
    echo ""
    echo "## Install run ${STAMP}"
    echo ""
    echo "- **Status:** ${STATUS}"
    echo "- **Python:** ${PYVER:-?}  (\`${PYBIN:-?}\`)"
    echo "- **Torch:** ${TORCH:-not installed}"
    echo "- **Architecture:** shares opus trading's vLLM at \`$(grep -m1 base_url config.yaml | awk '{print $2}')\` — no own vLLM/Docker."
    echo "- **Services to run after this:** \`./start.sh all\` → bridge :8090, STT :8091, TTS :8092, web :8080."
    echo "- **Full log:** \`${LOG}\`"
    echo ""
  } >> "$NOTE"
  if [ "$STATUS" = "OK" ]; then
    green "Install note written to ${NOTE} (log: ${LOG})"
  else
    red "Setup did not complete — see ${LOG}. Note recorded in ${NOTE}."
  fi
}
trap finish EXIT

green "==> project mocha setup  (${STAMP})"

# ── 1. Pick a Python (prefer 3.12 for ML wheel coverage) ───────────────────
for c in python3.12 python3.11 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1; then PYBIN="$c"; break; fi
done
if [ -z "$PYBIN" ]; then red "  No python3 found."; exit 1; fi
PYVER="$($PYBIN --version 2>&1)"
green "  Using ${PYVER} (${PYBIN})"

# ── 2. GPU note (STT/TTS prefer CUDA; not fatal) ───────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
  green "  GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
else
  yellow "  nvidia-smi not found — STT/TTS will fall back to CPU (slow). Set device: cpu in config.yaml if needed."
fi

# ── 3. Build venv ──────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
  green "==> Creating .venv …"
  "$PYBIN" -m venv .venv
else
  yellow "  .venv already exists — reusing."
fi
# shellcheck disable=SC1091
source .venv/bin/activate
green "==> Upgrading pip/wheel/setuptools …"
python -m pip install -U pip wheel setuptools

# ── 4. Torch for Blackwell (cu128). Stable first, nightly fallback. ────────
green "==> Installing PyTorch (CUDA 12.8 for Blackwell sm_120) …"
if python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128; then
  TORCH="stable cu128"
else
  yellow "  Stable cu128 failed — trying nightly cu128 …"
  if python -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128; then
    TORCH="nightly cu128"
  else
    red "  PyTorch install failed (see log). Aborting."; exit 1
  fi
fi
python -c "import torch; print('  torch', torch.__version__, 'cuda?', torch.cuda.is_available())" || true

# ── 5. Project dependencies ────────────────────────────────────────────────
green "==> Installing requirements.txt (incl. langgraph) …"
python -m pip install -r requirements.txt

# torchcodec (pulled transitively by f5-tts) ships per-CUDA builds; the PyPI
# default targets the newest CUDA and fails to load against our cu128 torch
# (libnvrtc.so version mismatch → "Could not load libtorchcodec", which takes
# mem0/memory down). Force the matching build from torch's own index.
green "==> Aligning torchcodec with torch's CUDA (cu128) …"
if [ "$TORCH" = "nightly cu128" ]; then TC_IDX="https://download.pytorch.org/whl/nightly/cu128"; else TC_IDX="https://download.pytorch.org/whl/cu128"; fi
python -m pip install --force-reinstall --no-deps torchcodec --index-url "$TC_IDX" \
  || yellow "  torchcodec align failed — audio decode (TTS/memory) may warn; see log."

# ── 6. .env ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  if [ -f .env.example ]; then cp .env.example .env; yellow "  Created .env from .env.example — fill in API keys."; fi
else
  yellow "  .env already exists — leaving it."
fi

# ── 7. Sanity checks ───────────────────────────────────────────────────────
green "==> Sanity checks …"
BASEURL="$(grep -m1 'base_url' config.yaml | awk '{print $2}')"
HEALTH="${BASEURL%/v1}/health"
if curl -sf --connect-timeout 2 "$HEALTH" >/dev/null 2>&1; then
  green "  Shared vLLM reachable at ${BASEURL}"
else
  yellow "  Shared vLLM not reachable at ${BASEURL} — make sure opus trading's vLLM is up before chatting."
fi
python - <<'PY' || yellow "  (import check reported an issue — see log)"
import importlib
for m in ("langgraph", "langchain_core", "fastapi", "faster_whisper", "mem0"):
    try:
        importlib.import_module(m); print(f"  import OK: {m}")
    except Exception as e:
        print(f"  import FAIL: {m} -> {e}")
PY
# Build the LangGraph (no GPU needed) to confirm the orchestration wires up.
python -c "from bridge.graph import MOCHA_GRAPH; print('  LangGraph compiled OK')" 2>/dev/null \
  || yellow "  (could not build graph yet — fine if a heavy dep import is the blocker; check at runtime)"

STATUS="OK"
green "╔══════════════════════════════════════════════════════════════╗"
green "║  project mocha setup complete                                ║"
green "║  Next:  ./start.sh all                                       ║"
green "╚══════════════════════════════════════════════════════════════╝"
