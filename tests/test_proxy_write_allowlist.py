"""Guard: the opus subprocess proxy is a STRUCTURAL read-only boundary.

call_opus/call_opus_raw f-string the tool name into `from mcp_server import {tool}`,
which can reach any desk tool (place_order, kill-switch). EXPOSED_TOOLS only
filters the LLM's menu — so the proxies MUST gate the tool name at runtime. This
test pins that gate: only allowlisted read tools may be dispatched; everything
else is refused before any subprocess spawns. Since the 2026-08-02 standalone
split there are NO sanctioned writes (kg_raise_gap was deleted desk-side
2026-07-20), so the boundary is fully read-only.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent

# Desk tools that mutate state — none of these may ever pass the proxy gate.
_MUTATING = {
    "place_order", "cancel_order", "modify_order", "set_kill_switch",
    "toggle_kill_switch", "submit_conviction_view", "record_thesis",
    "rebalance", "upsert_entity", "upsert_edge", "stamp_evidence",
    "kg_raise_gap",  # the historical one-write exception — retired, stays denied
}


def test_write_allowlist_is_empty():
    from tools.custom._opus_introspect import WRITE_ALLOWLIST
    assert set(WRITE_ALLOWLIST) == set(), "Mocha is fully read-only — no desk writes"


def test_gate_allows_reads_denies_all_mutations():
    from tools.custom._opus_introspect import is_tool_allowed
    for t in ("kg_neighbors", "get_balances", "get_ticker_valuation",
              "get_position_dossier", "get_quote", "get_recent_news",
              "get_ticker_dossier"):
        assert is_tool_allowed(t), f"{t} should be allowed"
    for t in _MUTATING:
        assert not is_tool_allowed(t), f"{t} must be denied by the proxy gate"


def test_exposed_tools_contains_no_mutating_tool():
    from tools.custom._opus_introspect import EXPOSED_TOOLS
    leaked = _MUTATING & set(EXPOSED_TOOLS)
    assert not leaked, f"EXPOSED_TOOLS must be read-only; found mutating: {sorted(leaked)}"


def test_call_opus_refuses_unallowlisted_tool_without_spawning():
    """A denied tool returns an error envelope and never reaches the subprocess."""
    from tools.custom._opus_proxy import call_opus
    out = asyncio.run(call_opus("place_order", {"symbol": "AAPL", "qty": 1}, "x"))
    obj = json.loads(out)
    # error envelope (panel) whose reason mentions the allowlist
    assert "allowlist" in json.dumps(obj).lower()


def test_call_opus_raw_refuses_unallowlisted_tool():
    from tools.custom._opus_raw import call_opus_raw
    ok, msg = asyncio.run(call_opus_raw("set_kill_switch", {"active": True}))
    assert ok is False and "allowlist" in msg.lower()


def _func_src(path: Path, name: str) -> str:
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def test_both_proxies_reference_the_gate_in_source():
    """Pin that the gate exists in source so it can't be silently removed."""
    proxy = _func_src(_REPO / "tools" / "custom" / "_opus_proxy.py", "call_opus")
    raw = _func_src(_REPO / "tools" / "custom" / "_opus_raw.py", "call_opus_raw")
    assert "is_tool_allowed" in proxy, "call_opus lost its allowlist gate"
    assert "is_tool_allowed" in raw, "call_opus_raw lost its allowlist gate"
