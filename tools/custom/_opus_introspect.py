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
#
# SAFETY: every name here MUST be a READ-ONLY opus tool. Never add anything that
# places/cancels/modifies orders, toggles kill switches, writes notes/memory,
# rebalances, or sends messages — that would break the "Mocha can never change
# the desk" guarantee. When in doubt, leave it out.
EXPOSED_TOOLS: set[str] = {
    # Pre-wrapped panel envelope (handled by tools/custom/get_trading_briefing.py
    # — kept as a hand-written file for the envelope path)
    "get_trading_briefing",
    # Curated companion views (raw data + analyst_note) — auto-registered.
    "get_position_dossier",
    # NOTE: "get_ticker_valuation" (the desk's Damodaran/Sonnet valuation report,
    # read-only, by ticker) is deliberately NOT in this allowlist. opus DOES expose
    # it (mcp_server.py imports mcp_tools.ticker_valuation), but it returns raw
    # markdown prose, not JSON — the generic _opus_proxy path does json.loads() on
    # every result and would reject it as "non-JSON output". It also needs
    # distilling (the full report is ~16 KB, far too big for a voice turn). So it's
    # served by a hand-written wrapper instead: tools/custom/get_ticker_valuation.py
    # (raw-text subprocess + TL;DR/Verdict extraction), allowlisted in config.yaml
    # tools.allowed and wired into the desk_block in context.py.
    "get_agent_overview",
    "get_pnl_attribution",
    "get_trade_activity",
    "get_risk_overview",
    "get_agent_disagreement",
    "get_position_history",
    "get_changes_since",
    "get_upcoming_catalysts",
    "get_manual_overrides",
    # Desk-state read-outs (added 2026-06-14 — current opus exposes these and
    # they let Mocha speak to "what's interesting right now"). All read-only.
    "get_pnl_summary",        # desk-wide combined P&L by agent
    "get_positions",          # all open positions (qty, avg cost, unrealized)
    "get_balances",           # NAV, cash, buying power, realized/unrealized P&L
    "get_agent_pnl_windows",  # per-agent P&L over 1d / WTD / month / 3mo
    "get_trade_blotter",      # fill history
    "get_agent_list",         # configured agents + allocation + enabled status
    "get_market_status",      # NYSE hours: is_open, session bounds, next_open
    "get_kill_switch_status", # is the desk trading or halted (status only)
    # Shared knowledge graph (read-only, 2026-06-16) — cited entity
    # relationships. interpret_node consults kg_query to ground "is AMZN up
    # because of Anthropic?"-style questions instead of confabulating. Both
    # return plain JSON, so the generic _opus_proxy path serves them.
    "kg_query",               # relationships of an entity / between two entities
    "kg_neighbors",           # 1-hop neighborhood of an entity
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
