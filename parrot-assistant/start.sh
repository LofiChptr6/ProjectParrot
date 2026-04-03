#!/usr/bin/env bash
# Parrot Assistant — Start all services
# Usage: ./start.sh [all|stt|tts|memory|bridge|stop|status]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PIDS_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$PIDS_DIR" "$LOG_DIR"

activate_venv() {
    if [ ! -d "$VENV" ]; then
        echo "ERROR: Virtual environment not found at $VENV"
        echo "Run setup first:"
        echo "  python3 -m venv --without-pip $VENV"
        echo "  curl -sS https://bootstrap.pypa.io/get-pip.py | $VENV/bin/python3"
        echo "  $VENV/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124"
        echo "  $VENV/bin/pip install -r requirements.txt"
        exit 1
    fi
    source "$VENV/bin/activate"
}

start_service() {
    local name="$1"
    local module="$2"
    local port="$3"
    local pidfile="$PIDS_DIR/$name.pid"
    local logfile="$LOG_DIR/$name.log"

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "[$name] Already running (PID $(cat "$pidfile"))"
        return
    fi

    # Kill any zombie process squatting on this port
    local squatter=""
    squatter=$(ss -tlnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)
    if [ -n "$squatter" ]; then
        echo "[$name] Killing stale process $squatter on port $port"
        kill "$squatter" 2>/dev/null || true
        sleep 1
    fi

    echo "[$name] Starting on port $port..."
    cd "$SCRIPT_DIR"
    python -m uvicorn "$module" --host 0.0.0.0 --port "$port" &> "$logfile" &
    local pid=$!
    echo "$pid" > "$pidfile"
    echo "[$name] Started (PID $pid) — log: $logfile"
}

stop_all() {
    echo "Stopping all services..."
    for pidfile in "$PIDS_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "[$name] Stopped (PID $pid)"
        fi
        rm -f "$pidfile"
    done
}

status() {
    echo "=== Parrot Assistant Status ==="
    for pidfile in "$PIDS_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "  [$name] Running (PID $pid)"
        else
            echo "  [$name] Dead (stale PID $pid)"
            rm -f "$pidfile"
        fi
    done

    if ! ls "$PIDS_DIR"/*.pid &>/dev/null; then
        echo "  No services running."
    fi
}

case "${1:-all}" in
    all)
        activate_venv
        start_service memory "memory.service:app" 8003
        start_service animation "animation.service:app" 8004
        sleep 2
        start_service stt "stt.service:app" 8001
        start_service tts "tts.service:app" 8002
        echo "  Waiting for STT/TTS models to load..."
        sleep 5
        start_service bridge "bridge.server:app" 8000
        echo ""
        echo "All services started."
        echo "  Bridge:    http://localhost:8000"
        echo "  STT:       http://localhost:8001"
        echo "  TTS:       http://localhost:8002"
        echo "  Memory:    http://localhost:8003"
        echo "  Animation: http://localhost:8004"
        echo ""
        echo "Health check: curl http://localhost:8000/health (waiting up to ~10s)"
        for i in {1..10}; do
            if curl -fsS "http://localhost:8000/health" >/dev/null 2>&1; then
                echo "Health OK: $(curl -sS http://localhost:8000/health)"
                break
            fi
            echo "  waiting for bridge... ($i/10)"
            sleep 1
        done
        echo ""
        echo "Dashboard (click in terminal):"
        echo "  Monitor:   http://127.0.0.1:8000/monitor"
        echo "  Swagger:   http://127.0.0.1:8000/docs"
        echo "  Voice Chat: http://127.0.0.1:8000/"
        ;;
    stt)       activate_venv; start_service stt "stt.service:app" 8001 ;;
    tts)       activate_venv; start_service tts "tts.service:app" 8002 ;;
    memory)    activate_venv; start_service memory "memory.service:app" 8003 ;;
    animation) activate_venv; start_service animation "animation.service:app" 8004 ;;
    bridge)
        activate_venv
        start_service bridge "bridge.server:app" 8000
        echo ""
        echo "Swagger UI: http://127.0.0.1:8000/docs"
        ;;
    stop)   stop_all ;;
    status) status ;;
    *)
        echo "Usage: $0 {all|stt|tts|memory|animation|bridge|stop|status}"
        exit 1
        ;;
esac
