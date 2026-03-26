#!/usr/bin/env bash
# Quick health check for the ProjectParrot stack.
set -euo pipefail

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }

echo "── GPU ──"
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
           --format=csv,noheader 2>/dev/null || red "nvidia-smi not available"
echo

echo "── Ollama ──"
if systemctl is-active --quiet ollama 2>/dev/null; then
    green "Service: running"
    ollama list 2>/dev/null || true
else
    if pgrep -x ollama &>/dev/null; then
        yellow "Running manually (not via systemd)"
    else
        red "Not running"
    fi
fi
echo

echo "── OpenClaw (Docker) ──"
if docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -q '^openclaw'; then
    green "$(docker ps --format '{{.Names}}: {{.Status}}' --filter name=openclaw)"
else
    red "Container not running"
fi
echo

echo "── Connectivity ──"
if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    green "Ollama API: OK (http://localhost:11434)"
else
    red "Ollama API: unreachable"
fi

if curl -sf http://localhost:3000 > /dev/null 2>&1; then
    green "OpenClaw UI: OK (http://localhost:3000)"
else
    red "OpenClaw UI: unreachable"
fi
