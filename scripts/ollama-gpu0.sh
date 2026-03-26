#!/usr/bin/env bash
# Start Ollama pinned to GPU 0 (RTX Pro 6000 Blackwell).
# Use this if you want to run Ollama manually instead of via systemd.
export CUDA_VISIBLE_DEVICES=0
exec ollama serve "$@"
