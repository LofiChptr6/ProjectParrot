"""Guard: the opus subprocess proxy is a STRUCTURAL read-only boundary.

call_opus/call_opus_raw f-string the tool name into `from mcp_server import {tool}`,
which can reach any desk tool (place_order, kill-switch). EXPOSED_TOOLS only
filters the LLM's menu — so the proxies MUST gate the tool name at runtime. This
test pins that gate: only allowlisted reads + the one sanctioned append-only
write (kg_raise_gap) may be dispatched; everything else is refused before any
subprocess spawns.
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
}


def test_write_allowlist_is_exactly_kg_raise_gap():
    from tools.custom._opus_introspect import WRITE_ALLOWLIST
    assert set(WRITE_ALLOWLIST) == {"kg_raise_gap"}


def test_kg_raise_gap_is_internal_only_not_on_llm_menu():
    """kg_raise_gap must NOT be in EXPOSED_TOOLS (the LLM cannot choose to write);
    only interpret_node's deterministic gap detection calls it."""
    from tools.custom._opus_introspect import EXPOSED_TOOLS, WRITE_ALLOWLIST
    assert "kg_raise_gap" not in EXPOSED_TOOLS
    assert "kg_raise_gap" in WRITE_ALLOWLIST


def test_gate_allows_reads_and_the_one_write_denies_mutations():
    from tools.custom._opus_introspect import is_tool_allowed
    for t in ("kg_query", "kg_neighbors", "get_balances", "get_ticker_valuation",
              "get_position_dossier", "kg_raise_gap"):
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
