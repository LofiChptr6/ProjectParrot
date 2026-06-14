#!/usr/bin/env bash
# Install project mocha as a systemd USER service (NO sudo).
# It binds into trading-desk.target, so starting the desk starts Mocha too.
#   scripts/install-service.sh          # install + enable (joins the desk) + start now
#   scripts/install-service.sh remove   # stop + disable + remove
set -euo pipefail

UNIT="project-mocha.service"
SRC="$(cd "$(dirname "$0")" && pwd)/systemd/${UNIT}"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DEST="${DEST_DIR}/${UNIT}"

if [ "${1:-install}" = "remove" ]; then
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$DEST"
    systemctl --user daemon-reload
    echo "Removed ${UNIT} (user service). Mocha will no longer start with the desk."
    exit 0
fi

[ -f "$SRC" ] || { echo "unit file not found: $SRC"; exit 1; }
mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST"
systemctl --user daemon-reload
# enable → joins trading-desk.target (auto-on with the desk); --now → start now.
systemctl --user enable --now "$UNIT"
# Survive logout / start on boot without an interactive login (best-effort; the
# desk already relies on this, so it's usually already on).
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo
systemctl --user --no-pager --full status "$UNIT" 2>&1 | head -15 || true
echo
echo "✅ Mocha is a USER service now — no sudo. It auto-starts with the desk:"
echo "     systemctl --user start trading-desk.target     # 'start Corvus' → Mocha comes up too"
echo "     systemctl --user stop  trading-desk.target     # stops the desk → stops Mocha (PartOf)"
echo
echo "   Manage Mocha directly:  systemctl --user {start,stop,restart,status} project-mocha"
echo "   Don't auto-start with the desk (the 'unless specified' opt-out):"
echo "     systemctl --user disable project-mocha          # still manually startable"
echo "   Logs:  journalctl --user -u project-mocha -f   (+ project_mocha/logs/*.log)"
