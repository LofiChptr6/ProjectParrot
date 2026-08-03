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

from tools.custom._opus_proxy import OPUS_DIR, OPUS_PY  # single source of desk-root truth
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
    "get_pnl_attribution",
    "get_upcoming_catalysts",
    # 2026-07-01 trim: get_agent_overview / get_trade_activity / get_risk_overview /
    # get_agent_disagreement / get_position_history / get_changes_since /
    # get_manual_overrides were REMOVED from the desk MCP (near-zero usage; two
    # narrated the vestigial always-empty agent_allocations table). Every menu
    # entry costs prompt tokens on the 3B every turn — keep this list to tools
    # that exist and get used.
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
    # Shared knowledge graph (read-only) — cited entity relationships.
    # interpret_node consults kg_neighbors to ground "is AMZN up because of
    # Anthropic?"-style questions instead of confabulating. (kg_query and
    # kg_raise_gap were deleted desk-side 2026-07-20; kg_neighbors is the
    # surviving read surface.)
    "kg_neighbors",           # 1-hop neighborhood of an entity
    # Market + news read-outs (added 2026-08-02 with the standalone split —
    # assistant-role awareness; all read-only, all plain JSON).
    "get_quote",              # live quote for a symbol
    "get_bars",               # OHLCV bars for a symbol
    "get_recent_news",        # latest ingested market news
    "semantic_news_recall",   # semantic search over the news store
    "get_ticker_dossier",     # one-stop dossier for any ticker (not just held)
}

# ── Runtime proxy allowlist (PRIME DIRECTIVE enforcement) ────────────────────
# EXPOSED_TOOLS above only governs the LLM's tool *menu*. It is NOT a runtime
# gate: call_opus()/call_opus_raw() f-string the tool name into
# `from mcp_server import {tool}`, and mcp_server re-exports EVERY desk tool
# (place_order, kill-switch, submit_conviction, …). So the proxies MUST gate the
# tool name against the sets below BEFORE spawning the subprocess — that is the
# structural boundary that keeps Mocha read-only, not the menu.
#
# Read tools served via the RAW-text proxy (_opus_raw) that are intentionally
# NOT in the JSON menu (they return prose distilled by hand-written wrappers).
_RAW_READ_EXTRA: frozenset[str] = frozenset({
    "get_ticker_valuation",   # Damodaran/Sonnet report → summarize_valuation()
})

# Mocha→desk writes: NONE. The historical exception (kg_raise_gap, an
# append-only research-backlog row) was deleted desk-side 2026-07-20, so since
# the 2026-08-02 standalone split the proxy boundary is fully read-only.
WRITE_ALLOWLIST: frozenset[str] = frozenset()

# The complete set of opus tools either proxy may dispatch. Default-deny:
# anything not here (place_order, set_kill_switch, …) is refused at the proxy.
ALLOWED_PROXY_TOOLS: frozenset[str] = EXPOSED_TOOLS | _RAW_READ_EXTRA | WRITE_ALLOWLIST


def is_tool_allowed(tool: str) -> bool:
    """Runtime gate for the subprocess proxies. True iff `tool` is an
    explicitly-allowlisted read-only tool."""
    return tool in ALLOWED_PROXY_TOOLS


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
