"""Discover the opus_trading MCP tool surface at load time.

Spawns a one-shot Python subprocess in the opus_trading venv, calls
``mcp.list_tools()``, and returns the parsed schemas. The result feeds
``_opus_tools.register()`` so we never hand-maintain TOOL_DEFs that
mirror opus signatures — schemas always match exactly.

Module name starts with ``_`` so the custom-tool loader skips it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("tools.custom._opus_introspect")

OPUS_DIR = Path(__file__).resolve().parents[3]  # project_mocha/tools/custom -> opus trading root
assert (OPUS_DIR / "mcp_server.py").exists(), f"opus root not found at {OPUS_DIR}"
OPUS_PY = OPUS_DIR / ".venv" / "bin" / "python"
DISCOVER_TIMEOUT_SEC = 15.0

# Read-only personal-trading tools we expose to Mocha. Opus has 60+ tools
# total including order placement, kill switches, etc. — explicit allowlist
# is the safety boundary. Add here when opus ships a new read-only tool we
# want surfaced.
EXPOSED_TOOLS: set[str] = {
    # Pre-wrapped panel envelope (handled by tools/custom/get_trading_briefing.py
    # — kept as a hand-written file for the envelope path)
    "get_trading_briefing",
    # Raw data + analyst_note tools — auto-registered via _opus_tools.py
    "get_position_dossier",
    "get_agent_overview",
    "get_pnl_attribution",
    "get_trade_activity",
    "get_risk_overview",
    "get_agent_disagreement",
    "get_position_history",
    "get_changes_since",
    "get_upcoming_catalysts",
    "get_manual_overrides",
}

_BOOTSTRAP = """
import asyncio, json
from mcp_server import mcp
async def main():
    tools = await mcp.list_tools()
    out = []
    for t in tools:
        out.append({
            'name': t.name,
            'description': (t.description or '').strip(),
            'parameters': t.parameters or {'type': 'object', 'properties': {}},
        })
    print(json.dumps(out))
asyncio.run(main())
"""


def discover_opus_tools() -> list[dict]:
    """Return [{name, description, parameters}, ...] for tools in EXPOSED_TOOLS.

    Always returns a list — empty on any failure so the bridge keeps starting
    even when opus is offline.
    """
    if not OPUS_PY.exists():
        log.warning("opus_trading venv not found at %s — no MCP tools registered", OPUS_PY)
        return []
    try:
        proc = subprocess.run(
            [str(OPUS_PY), "-c", _BOOTSTRAP],
            cwd=str(OPUS_DIR),
            capture_output=True,
            text=True,
            timeout=DISCOVER_TIMEOUT_SEC,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.home()),
            },
        )
    except Exception as exc:
        log.warning("opus introspection subprocess failed: %s", exc)
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        tail = proc.stderr.strip().splitlines()[-3:] if proc.stderr else []
        log.warning("opus introspection exited %d: %s", proc.returncode, " | ".join(tail))
        return []

    try:
        all_tools = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log.warning("opus introspection returned non-JSON: %s", exc)
        return []

    return [t for t in all_tools if t.get("name") in EXPOSED_TOOLS]
