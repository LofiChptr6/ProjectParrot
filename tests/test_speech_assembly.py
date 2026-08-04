"""Speech-text assembly fixes from the 2026-08 conversation audit.

Covers the chunk glue bug ("I'm Mocha.Got a feeling…"), stall-ack formatting,
and the emoji scrub on the TTS path.
"""

from bridge.graph import _format_ack, _join_speech


# ── _join_speech: the "Mocha.Got" glue bug ────────────────────────────────────

def test_join_speech_inserts_spaces_between_stripped_chunks():
    assert _join_speech(["Hi there, I'm Mocha.", "Got a feeling you're curious?"]) \
        == "Hi there, I'm Mocha. Got a feeling you're curious?"


def test_join_speech_skips_empty_and_whitespace_parts():
    assert _join_speech(["  a  ", "", "   ", "b."]) == "a b."


def test_join_speech_empty_and_none():
    assert _join_speech([]) == ""
    assert _join_speech(None) == ""


# ── _format_ack: stall line normalized for history/echo prepending ────────────

def test_format_ack_adds_terminal_period():
    assert _format_ack("on it") == "on it."


def test_format_ack_keeps_existing_terminator():
    assert _format_ack("one sec…") == "one sec…"
    assert _format_ack("checking the desk.") == "checking the desk."


def test_format_ack_empty():
    assert _format_ack("") == ""
    assert _format_ack("   ") == ""


# ── emoji never reach TTS (display text keeps them) ───────────────────────────

def test_sanitize_for_tts_strips_emoji_keeps_words():
    from bridge.server import _sanitize_for_tts
    out = _sanitize_for_tts("nice work 🎉🚀 seriously ✨")
    assert "🎉" not in out and "🚀" not in out and "✨" not in out
    assert "nice work" in out and "seriously" in out


def test_sanitize_for_tts_keeps_cjk_and_accents():
    from bridge.server import _sanitize_for_tts
    assert _sanitize_for_tts("好久不见, café time") == "好久不见, café time"
