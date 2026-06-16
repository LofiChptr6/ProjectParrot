"""Speech chunker must not split a number at its decimal point ($70.3 million)."""

import importlib

import pytest


@pytest.fixture()
def ir():
    import bridge.inline_route as m
    importlib.reload(m)
    return m


def test_sentence_end_skips_decimal_at_buffer_end(ir):
    # Mid-stream buffer ending on a decimal point — NOT a sentence end.
    assert ir._sentence_end("revenue was $70.") == -1
    # Decimal with a digit after — also not a sentence end.
    assert ir._sentence_end("revenue was $70.3 million") == -1


def test_sentence_end_finds_real_terminator(ir):
    assert ir._sentence_end("Hello. World") == 6          # after "Hello."
    assert ir._sentence_end("up 19%. Next") == 7          # "%." is a real end
    # An abbreviation-ish "C3.ai" (dot followed by a letter) is not an end.
    assert ir._sentence_end("C3.ai is cloud") == -1


def test_chunker_keeps_decimal_whole_across_token_boundary(ir):
    ch = ir._SpeechChunker()
    out = []
    # Simulate the streaming split that caused "$70." | "3 million".
    out += ch.add("C3.ai's Q1 revenue was $70.")   # token boundary right on the decimal
    out += ch.add("3 million, down 19%. Next.")
    out += ch.flush_final()
    joined = "".join(out)
    assert "$70.3 million" in joined
    # And it was NOT emitted as a chunk ending in "$70."
    assert not any(c.rstrip().endswith("$70.") for c in out)


def test_chunker_still_splits_real_sentences(ir):
    ch = ir._SpeechChunker()
    out = ch.add("First sentence here. ") + ch.add("Second one now. ") + ch.flush_final()
    assert len([c for c in out if c.strip()]) >= 2
