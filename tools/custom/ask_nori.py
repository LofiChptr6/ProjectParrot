"""Custom tool: ask_nori — delegate research to Nori, the analyst sub-agent.

Mocha calls this when the user wants data, visuals, charts, news, weather,
stocks, or any information that requires research. Nori handles everything
and returns narration guidance for Mocha.
"""

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "ask_nori",
        "description": (
            "Ask Nori, your research analyst, to look into something. Nori will "
            "fetch data, build charts/presentations/cards in the UI, and tell you "
            "what to say. Use this for ANY request involving: current data (stocks, "
            "news, weather), visual presentations (charts, slides, tables), video "
            "lookups, calculations, or research. Nori handles all the heavy work — "
            "you just present what she prepares."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "Tell Nori what to research, in plain language. "
                        "Translate the user's intent clearly. "
                        "E.g., 'User wants current stock price and chart for TSLA' "
                        "or 'User wants weather forecast for Arizona today with visuals'"
                    ),
                },
            },
            "required": ["request"],
        },
    },
}


_JUNK_AFFIRMS = {
    "yeah", "yes", "yup", "yep", "ok", "okay", "sure", "cool", "nice",
    "great", "thanks", "thank you", "thanks!", "no", "nope", "nah",
    "not really", "maybe", "yeah of course", "of course", "yeah of course!",
    "yes of course", "yes please", "no thanks",
}


def _is_junk_request(request: str) -> bool:
    """Catch obvious non-research requests before we spin up a ~20s Nori loop."""
    r = (request or "").strip()
    if len(r) < 12:
        return True
    low = r.lower().rstrip("?.!").strip()
    if low in _JUNK_AFFIRMS:
        return True
    # Affirmation followed by a fragment ("yeah of course man")
    for aff in _JUNK_AFFIRMS:
        if low == aff or low.startswith(aff + " ") or low.startswith(aff + "!"):
            # Affirmation is the whole sentence and has no research markers.
            if not any(m in low for m in ("?", "what", "when", "how ", "where",
                                           "who ", "which", "tell me", "show me",
                                           "find ", "search ", "look up",
                                           "look into", "check ")):
                return True
            break
    return False


async def execute(arguments: dict) -> str:
    """Delegate to Nori and return her narration guidance."""
    from nori.agent import process_request

    request = arguments.get("request", "")
    if not request:
        return "Error: no request provided."

    if _is_junk_request(request):
        # Refuse without running a full Nori loop — prevents hallucinated
        # welcome decks from ambiguous affirmations.
        return (
            "Refused: request is too ambiguous to research "
            "(looks like an affirmation or fragment, not a research task). "
            "Clarify with the user what they actually want to know."
        )

    try:
        result = await process_request(request)
        return result
    except Exception as e:
        # Make sure Nori light goes idle on error
        try:
            from bridge.server import _broadcast_monitor
            await _broadcast_monitor({"type": "thread_status", "thread": "nori", "status": "idle"})
        except Exception:
            pass
        return f"Nori encountered an error: {e}"
