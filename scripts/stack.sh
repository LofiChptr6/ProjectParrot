#!/usr/bin/env bash
# ProjectParrot — Docker Compose: bring up, restart, status, health.
# Run from anywhere:  ./scripts/stack.sh health
# Or:                 bash scripts/stack.sh up

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose)

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }

usage() {
    cat <<'EOF'
Usage: stack.sh <command> [args]

Docker (docker-compose.yml: ollama, openclaw)
  up              Start all services (detached)
  down            Stop and remove containers (keeps volumes)
  restart [svc]   Restart all, or one service (e.g. ollama)
  pull            Pull latest images then up
  ps              docker compose ps
  logs [svc]      Tail logs (all services if svc omitted)

Checks
  health          Containers + HTTP checks (Ollama, OpenClaw, optional parrot-assistant bridge)

Browser (Swagger / UIs)
  docs            Open parrot-assistant Swagger UI (http://127.0.0.1:8000/docs) — bridge must be running
  openclaw-ui     Open OpenClaw in the browser (http://localhost:3000)

Examples:
  ./scripts/stack.sh up
  ./scripts/stack.sh restart ollama
  ./scripts/stack.sh health
  cd parrot-assistant && ./start.sh all && cd - && ./scripts/stack.sh docs
EOF
}

# Open default browser (WSL → Windows friendly).
open_browser() {
    local url="$1"
    if command -v wslview >/dev/null 2>&1; then
        wslview "$url" 2>/dev/null && return 0
    fi
    if [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi microsoft /proc/version 2>/dev/null; then
        if command -v cmd.exe >/dev/null 2>&1; then
            cmd.exe /c start "" "$url" 2>/dev/null && return 0
        fi
        if command -v powershell.exe >/dev/null 2>&1; then
            powershell.exe -NoProfile -Command "Start-Process '$url'" 2>/dev/null && return 0
        fi
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" 2>/dev/null && return 0
    fi
    yellow "Could not launch a browser. Open this URL manually:"
    echo "$url"
    return 1
}

ollama_urls_to_try() {
    # WSL: Ollama may only be reachable via host.docker.internal; Windows: localhost works
    echo "http://127.0.0.1:11434"
    echo "http://host.docker.internal:11434"
    if command -v ip >/dev/null 2>&1; then
        gw="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
        if [[ -n "${gw:-}" ]]; then
            echo "http://${gw}:11434"
        fi
    fi
}

check_ollama_http() {
    local ok=1
    for base in $(ollama_urls_to_try); do
        if curl -sf --connect-timeout 3 "${base}/api/tags" >/dev/null 2>&1; then
            green "Ollama API: OK  ${base}"
            ok=0
            break
        fi
    done
    if [[ "$ok" -ne 0 ]]; then
        red "Ollama API: unreachable (tried 127.0.0.1, host.docker.internal, default gateway)"
    fi
    return "$ok"
}

cmd_health() {
    echo "=== Docker Compose: ps ==="
    "${COMPOSE[@]}" ps -a 2>/dev/null || red "docker compose failed — is Docker running?"
    echo

    echo "=== Ollama (HTTP) ==="
    check_ollama_http || true
    echo

    echo "=== OpenClaw (http://localhost:3000) ==="
    if curl -sf --connect-timeout 3 http://localhost:3000 >/dev/null 2>&1; then
        green "OpenClaw: OK"
    else
        red "OpenClaw: unreachable"
    fi
    echo

    echo "=== parrot-assistant bridge (optional, :8000) ==="
    if curl -sf --connect-timeout 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
        green "Bridge health:"
        curl -sS http://127.0.0.1:8000/health 2>/dev/null | python3 -m json.tool 2>/dev/null || curl -sS http://127.0.0.1:8000/health
        echo "Swagger UI: http://127.0.0.1:8000/docs  (./scripts/stack.sh docs)"
    else
        yellow "Bridge not running (start with: cd parrot-assistant && ./start.sh all)"
    fi
    echo

    echo "=== GPU (optional) ==="
    nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader 2>/dev/null || yellow "nvidia-smi not available"
}

case "${1:-}" in
    up|start)
        "${COMPOSE[@]}" up -d
        green "Up. Run: $0 health"
        ;;
    down|stop)
        "${COMPOSE[@]}" down
        ;;
    restart)
        if [[ -n "${2:-}" ]]; then
            "${COMPOSE[@]}" restart "$2"
        else
            "${COMPOSE[@]}" restart
        fi
        ;;
    pull)
        "${COMPOSE[@]}" pull
        "${COMPOSE[@]}" up -d
        ;;
    ps|status)
        "${COMPOSE[@]}" ps -a
        ;;
    logs)
        shift
        "${COMPOSE[@]}" logs -f --tail=100 "$@"
        ;;
    health|check)
        cmd_health
        ;;
    docs|swagger)
        if ! curl -sf --connect-timeout 2 http://127.0.0.1:8000/docs >/dev/null 2>&1; then
            yellow "Bridge does not respond on :8000 — start it first: cd parrot-assistant && ./start.sh all"
        fi
        green "Opening Swagger UI…"
        open_browser "http://127.0.0.1:8000/docs"
        ;;
    openclaw-ui)
        green "Opening OpenClaw…"
        open_browser "http://localhost:3000"
        ;;
    ""|-h|--help|help)
        usage
        ;;
    *)
        red "Unknown command: $1"
        usage
        exit 1
        ;;
esac
