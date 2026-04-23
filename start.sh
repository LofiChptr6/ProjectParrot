#!/usr/bin/env bash
# Parrot Assistant — Start all services
# Usage: ./start.sh [all|stt|tts|memory|animation|bridge|stop|end|restart|status]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
PIDS_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"
CONFIG="$SCRIPT_DIR/config.yaml"

mkdir -p "$PIDS_DIR" "$LOG_DIR"

# Read network addresses from config.yaml (single source of truth).
EXTERNAL_HOST=$(python3 -c "
import yaml, pathlib
c = yaml.safe_load(pathlib.Path('$CONFIG').read_text())
print(c.get('network',{}).get('external_host','127.0.0.1'))
" 2>/dev/null || echo "127.0.0.1")
INTERNAL_HOST=$(python3 -c "
import yaml, pathlib
c = yaml.safe_load(pathlib.Path('$CONFIG').read_text())
print(c.get('network',{}).get('internal_host','127.0.0.1'))
" 2>/dev/null || echo "127.0.0.1")

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
    local timeout_s=6

    # First: stop by pidfiles
    for pidfile in "$PIDS_DIR"/*.pid; do
        [ -f "$pidfile" ] || continue
        name="$(basename "$pidfile" .pid)"
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[$name] Stopping (PID $pid)..."
            kill "$pid" 2>/dev/null || true
            # Give it a moment to shut down cleanly
            for _ in $(seq 1 "$timeout_s"); do
                if ! kill -0 "$pid" 2>/dev/null; then
                    break
                fi
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[$name] Force killing (PID $pid)..."
                kill -9 "$pid" 2>/dev/null || true
            fi
            echo "[$name] Stopped."
        fi
        rm -f "$pidfile"
    done

    # Second: kill anything still listening on known ports (stale processes / no pidfile)
    for port in 8000 8001 8002 8003 8004; do
        squatter="$(ss -tlnp "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
        if [ -n "$squatter" ] && kill -0 "$squatter" 2>/dev/null; then
            echo "[port:$port] Killing leftover process $squatter"
            kill -9 "$squatter" 2>/dev/null || true
        fi
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
        echo "All services started.  (external_host=$EXTERNAL_HOST)"
        echo "  Bridge:    http://$EXTERNAL_HOST:8000"
        echo "  STT:       http://$INTERNAL_HOST:8001  (internal)"
        echo "  TTS:       http://$INTERNAL_HOST:8002  (internal)"
        echo "  Memory:    http://$INTERNAL_HOST:8003  (internal)"
        echo "  Animation: http://$INTERNAL_HOST:8004  (internal)"
        echo ""
        echo "Health check: curl http://$INTERNAL_HOST:8000/health (waiting up to ~10s)"
        for i in {1..10}; do
            if curl -fsS "http://$INTERNAL_HOST:8000/health" >/dev/null 2>&1; then
                echo "Health OK: $(curl -sS "http://$INTERNAL_HOST:8000/health")"
                break
            fi
            echo "  waiting for bridge... ($i/10)"
            sleep 1
        done
        echo ""
        echo "Channels (configured in config.yaml):"
        echo "  Telegram, Discord — set enabled: true + bot_token"
        echo "  CLI REPL — enabled by default (if bridge runs in foreground)"
        echo ""
        echo "Dashboard: http://$EXTERNAL_HOST:8000/monitor"
        ;;
    stt)       activate_venv; start_service stt "stt.service:app" 8001 ;;
    tts)       activate_venv; start_service tts "tts.service:app" 8002 ;;
    memory)    activate_venv; start_service memory "memory.service:app" 8003 ;;
    animation) activate_venv; start_service animation "animation.service:app" 8004 ;;
    bridge)
        activate_venv
        start_service bridge "bridge.server:app" 8000
        echo ""
        echo "Dashboard: http://$EXTERNAL_HOST:8000/monitor"
        ;;
    stop)   stop_all ;;
    end)    stop_all ;;
    restart)
        stop_all
        # Small delay helps ports fully release.
        sleep 1
        exec "$0" all
        ;;
    status) status ;;
    *)
        echo "Usage: $0 {all|stt|tts|memory|animation|bridge|stop|end|restart|status}"
        exit 1
        ;;
esac
