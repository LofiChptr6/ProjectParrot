"""Custom tool: get_ticker_valuation — read-only proxy to opus_trading MCP.

Returns a COMPACT, speakable summary of the desk's nightly Damodaran/Sonnet
valuation report for one ticker (provenance header + TL;DR / Verdict block),
not the full ~16 KB report. Distilling matters: dumping 16 KB into a voice turn
blows TTFT and makes Mocha ramble. Read-only — never trades.

The subprocess plumbing + distiller live in tools/custom/_opus_raw.py (shared
with get_position_brief.py).
"""

from __future__ import annotations

import asyncio
import json
import re

from tools.custom._opus_raw import call_opus_raw, summarize_valuation

_SYMBOL_RE = re.compile(r"[A-Z0-9.\-]{1,12}\Z")


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_ticker_valuation",
        "description": (
            "Returns the trading desk's own intrinsic-value verdict for one "
            "ticker — a compact summary (fair value vs. price, stance, key "
            "risk) distilled from the desk's nightly Damodaran-style valuation "
            "report written by Claude Sonnet. Call this when the user asks "
            "what the desk thinks a stock is worth, whether a name is cheap or "
            "expensive, or for the valuation/fundamentals behind a ticker. For "
            "a full strategy question about a HOLDING, prefer get_position_brief "
            "(it bundles this with the position data). Read-only — never trades."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. BIRK, ALB, NVDA (case-insensitive).",
                },
            },
            "required": ["symbol"],
        },
    },
}


def _error_envelope(symbol: str, message: str) -> str:
    """Render an error as a single-slide panel so Mocha can speak it gracefully."""
    payload = {
        "__panel__": "create_presentation",
        "__payload__": {
            "id": f"pres_valuation_error_{int(asyncio.get_event_loop().time() * 1000)}",
            "title": f"Valuation ({symbol}) — Unavailable",
            "slides": [{
                "type": "markdown",
                "narration": f"I couldn't pull the desk's valuation for {symbol} right now. {message}",
                "title": "Valuation Unavailable",
                "content": f"**Reason:** {message}",
            }],
        },
    }
    return json.dumps(payload)


async def execute(arguments: dict) -> str:
    symbol = str((arguments or {}).get("symbol", "")).strip().upper()
    if not _SYMBOL_RE.match(symbol):
        return "I need a ticker symbol to pull the desk's valuation — which company did you mean?"

    ok, text = await call_opus_raw("get_ticker_valuation", {"symbol": symbol})
    if not ok:
        return _error_envelope(symbol, text)
    return summarize_valuation(symbol, text)
