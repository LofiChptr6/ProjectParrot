"""Custom tool: get_position_brief — one-call VISUAL desk briefing for ONE holding.

Builds a multi-slide deck (snapshot → price trend → valuation scenarios → thesis
→ trades → news), broadcasts it to the webapp, and returns a SHORT spoken
narration (one beat per slide). The deck carries the dense numbers so TTS stays
light — Mocha is a visual terminal, not a number-reader. Falls back to a plain
combined text brief when there's no screen data (e.g. a non-held ticker, or the
deck build fails). Both underlying opus tools are READ-ONLY; fetched in parallel.

The combined call also removes a reliability problem: the model makes one tool
call per strategy turn but won't reliably chain a second.
"""

from __future__ import annotations

import asyncio
import re

from tools.custom._opus_raw import call_opus_raw, summarize_valuation

_SYMBOL_RE = re.compile(r"[A-Z0-9.\-]{1,12}\Z")
_DOSSIER_MAX_CHARS = 4000


TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "get_position_brief",
        "description": (
            "The full desk briefing for ONE holding — puts a slide deck on screen "
            "(snapshot, 3-month price trend, valuation scenarios, thesis, recent "
            "trades, latest news) and returns a short narration to speak. Call this "
            "for any strategy / thesis / \"what is X\" / \"what's our plan with X\" / "
            "\"show me X\" question about a position. Read-only — never trades."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol of the holding, e.g. BIRK (case-insensitive).",
                },
            },
            "required": ["symbol"],
        },
    },
}


async def _text_brief(symbol: str) -> str:
    """Plain combined dossier + valuation text — fallback when there's no deck."""
    (dok, dtext), (vok, vtext) = await asyncio.gather(
        call_opus_raw("get_position_dossier", {"symbol": symbol}),
        call_opus_raw("get_ticker_valuation", {"symbol": symbol}),
    )
    parts = [f"Desk briefing for {symbol} — weave these into one conversational "
             "answer (why we hold it, how we got here, what it's worth):"]
    if dok:
        if len(dtext) > _DOSSIER_MAX_CHARS:
            dtext = dtext[:_DOSSIER_MAX_CHARS].rstrip() + " …"
        parts.append("[POSITION DATA — convictions, OCAP rationale, fills, P&L]\n" + dtext)
    else:
        parts.append(f"[POSITION DATA unavailable: {dtext}]")
    if vok:
        parts.append("[DESK VALUATION — our Sonnet/Damodaran verdict]\n"
                     + summarize_valuation(symbol, vtext))
    else:
        parts.append(f"[DESK VALUATION unavailable: {vtext}]")
    return "\n\n".join(parts)


async def execute(arguments: dict) -> str:
    symbol = str((arguments or {}).get("symbol", "")).strip().upper()
    if not _SYMBOL_RE.match(symbol):
        return "Which holding did you want the briefing for? I need a ticker symbol."

    # Build + show the visual deck; speak the short narration it returns.
    payload = None
    narration = ""
    try:
        from tools.custom._position_deck import build_position_deck
        payload, narration = await build_position_deck(symbol)
    except Exception:
        payload, narration = None, ""

    if payload and narration:
        try:
            from bridge.server import _broadcast_clients, _set_open_modal
            await _broadcast_clients({
                "type": "ui_command",
                "action": "create_presentation",
                "presentation": payload,
            })
            try:
                _set_open_modal("presentation", {"id": payload.get("id"),
                                                 "title": payload.get("title")})
            except Exception:
                pass
        except Exception:
            pass  # no webapp / broadcast unavailable — still return the narration
        return narration

    # No deck data (non-held ticker, opus offline, etc.) — plain text brief.
    return await _text_brief(symbol)
