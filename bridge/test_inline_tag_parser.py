"""Unit tests for bridge.inline_tag_parser.

Run: python -m bridge.test_inline_tag_parser
"""

from __future__ import annotations

from bridge.inline_tag_parser import InlineTagParser


def _collect(p: InlineTagParser, text: str, chunk_size: int | None = None) -> list[dict]:
    events: list[dict] = []
    if chunk_size is None:
        events.extend(p.feed(text))
    else:
        for i in range(0, len(text), chunk_size):
            events.extend(p.feed(text[i : i + chunk_size]))
    events.extend(p.finish())
    return events


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def _only_text(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["kind"] == "text_delta")


# --------------------------------------------------------------------------
#  Plain text
# --------------------------------------------------------------------------

def test_plain_text_no_tags():
    p = InlineTagParser()
    ev = _collect(p, "Hello world, how are you?")
    assert _only_text(ev) == "Hello world, how are you?"
    # One flush for the "?" sentence terminator
    assert "flush" in _kinds(ev)


def test_multi_sentence_flushes():
    p = InlineTagParser()
    ev = _collect(p, "One. Two! Three?")
    flushes = [e for e in ev if e["kind"] == "flush"]
    assert len(flushes) == 3


def test_streaming_by_char():
    p = InlineTagParser()
    ev = _collect(p, "Hi there.", chunk_size=1)
    assert _only_text(ev) == "Hi there."


def _chunks(events: list[dict]) -> list[str]:
    """Reconstruct flushed speech chunks (text between flush boundaries)."""
    out: list[str] = []
    cur = ""
    for e in events:
        if e["kind"] == "text_delta":
            cur += e["text"]
        elif e["kind"] == "flush":
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out


def test_abbreviation_keeps_number_in_same_chunk():
    # "No. 2" / "Op. 21" must NOT split — the bug was the user hearing
    # "…Concerto No." then "2" as separate bubbles. Tested at realistic token
    # sizes (whole-string and 3-char); chunk_size=1 is excluded because the
    # parser pre-existingly can't flush any internal boundary at 1 char/feed
    # (terminator + trailing space never share a scan), unrelated to this guard.
    for chunk_size in (None, 3):
        p = InlineTagParser()
        ev = _collect(p, "Chopin Piano Concerto No. 2 is beautiful. Want it?",
                      chunk_size=chunk_size)
        ch = _chunks(ev)
        assert ch[0] == "Chopin Piano Concerto No. 2 is beautiful.", (chunk_size, ch)
        assert ch[1] == "Want it?", (chunk_size, ch)

    p = InlineTagParser()
    assert _chunks(_collect(p, "It is Op. 21 in F minor. A gem.")) == [
        "It is Op. 21 in F minor.", "A gem."]

    p = InlineTagParser()
    assert _chunks(_collect(p, "Mr. Bond is here. Good.")) == [
        "Mr. Bond is here.", "Good."]


def test_non_abbreviation_still_splits():
    # "cats." is a real sentence end even though a digit follows — the
    # abbreviation set (not a blanket digit-follows rule) keeps this correct.
    for chunk_size in (None, 3):
        p = InlineTagParser()
        assert _chunks(_collect(p, "I have 3 cats. 2 are black.", chunk_size)) == [
            "I have 3 cats.", "2 are black."], chunk_size


def test_lowercase_word_no_still_splits():
    # The ordinary word "no." must still end a sentence — case-sensitivity is
    # what separates it from the numbering abbreviation "No.".
    for chunk_size in (None, 3):
        p = InlineTagParser()
        assert _chunks(_collect(p, "I told him no. Then I left.", chunk_size)) == [
            "I told him no.", "Then I left."], chunk_size


def test_decimal_still_protected():
    p = InlineTagParser()
    assert _chunks(_collect(p, "The price is $202.06 today. Nice.")) == [
        "The price is $202.06 today.", "Nice."]


# --------------------------------------------------------------------------
#  Simple tags
# --------------------------------------------------------------------------

def test_single_emotion_tag():
    p = InlineTagParser()
    ev = _collect(p, "<emotion>happy</emotion>Hi there.")
    kinds = _kinds(ev)
    assert "emotion" in kinds
    em = next(e for e in ev if e["kind"] == "emotion")
    assert em["id"] == "happy"
    assert _only_text(ev) == "Hi there."


def test_single_gesture_tag():
    p = InlineTagParser()
    ev = _collect(p, "<gesture>wave</gesture>Hello!")
    g = next(e for e in ev if e["kind"] == "gesture")
    assert g["name"] == "wave"
    assert _only_text(ev) == "Hello!"


def test_back_to_back_tags_before_text():
    """User's example — two gestures back-to-back, no text between."""
    p = InlineTagParser()
    ev = _collect(p, "<gesture>pointing_finger</gesture><gesture>dance</gesture>Look here!")
    gestures = [e for e in ev if e["kind"] == "gesture"]
    assert [g["name"] for g in gestures] == ["pointing_finger", "dance"]
    assert _only_text(ev) == "Look here!"


def test_mixed_emotion_and_gesture():
    p = InlineTagParser()
    ev = _collect(p,
        "<emotion>neutral</emotion><gesture>speak_pointing</gesture>"
        "Look here, it's a profit opportunity."
        "<emotion>thinking</emotion><gesture>speak_arms_crossed</gesture>"
        "Let me find details."
    )
    kinds = _kinds(ev)
    emotions = [e["id"] for e in ev if e["kind"] == "emotion"]
    gestures = [e["name"] for e in ev if e["kind"] == "gesture"]
    assert emotions == ["neutral", "thinking"]
    assert gestures == ["speak_pointing", "speak_arms_crossed"]
    txt = _only_text(ev)
    assert "profit opportunity" in txt
    assert "find details" in txt


# --------------------------------------------------------------------------
#  Tool call
# --------------------------------------------------------------------------

def test_tool_call_with_body():
    p = InlineTagParser()
    ev = _collect(p, '<tool_call name="get_stock_data">caveats in investing in AAPL</tool_call>')
    tc = next(e for e in ev if e["kind"] == "tool_call")
    assert tc["name"] == "get_stock_data"
    assert tc["arguments"] == "caveats in investing in AAPL"
    assert tc["id"].startswith("tc_")


def test_reads_tag():
    p = InlineTagParser()
    ev = _collect(p, "<reads>venting</reads>Hi.")
    kinds = _kinds(ev)
    assert "reads" in kinds
    reads = next(e for e in ev if e["kind"] == "reads")
    assert reads["state"] == "venting"


def test_reads_first_then_emotion():
    """<reads> as first token, before <emotion>."""
    p = InlineTagParser()
    ev = _collect(p, "<reads>curious</reads><emotion>happy</emotion><gesture>speak_normal</gesture>So.")
    assert any(e["kind"] == "reads" and e["state"] == "curious" for e in ev)
    assert any(e["kind"] == "emotion" and e["id"] == "happy" for e in ev)


# --------------------------------------------------------------------------
#  Think passthrough
# --------------------------------------------------------------------------

def test_think_block_emits_thinking_delta():
    p = InlineTagParser()
    ev = _collect(p, "<think>let me reason...</think>Answer.")
    tdeltas = [e for e in ev if e["kind"] == "thinking_delta"]
    assert len(tdeltas) >= 1
    assert "reason" in "".join(e["text"] for e in tdeltas)
    assert _only_text(ev) == "Answer."


# --------------------------------------------------------------------------
#  Streaming edge cases
# --------------------------------------------------------------------------

def test_tag_split_across_chunks():
    p = InlineTagParser()
    ev: list[dict] = []
    ev.extend(p.feed("<emot"))
    ev.extend(p.feed("ion>ha"))
    ev.extend(p.feed("ppy</emo"))
    ev.extend(p.feed("tion>Hi!"))
    ev.extend(p.finish())
    em = next(e for e in ev if e["kind"] == "emotion")
    assert em["id"] == "happy"
    assert _only_text(ev) == "Hi!"


def test_tool_call_split_across_chunks():
    p = InlineTagParser()
    parts = ['<tool_call na', 'me="get_stock_data', '">find', ' AAPL caveats</tool_call>']
    ev: list[dict] = []
    for part in parts:
        ev.extend(p.feed(part))
    ev.extend(p.finish())
    tc = next(e for e in ev if e["kind"] == "tool_call")
    assert tc["name"] == "get_stock_data"
    assert tc["arguments"] == "find AAPL caveats"


# --------------------------------------------------------------------------
#  Malformed / edge cases
# --------------------------------------------------------------------------

def test_decimals_are_not_sentence_boundaries():
    """Ensure '$202.06' and '0.53%' don't get split as two sentences."""
    p = InlineTagParser()
    ev = _collect(p, "NVIDIA is at $202.06 right now, up 0.53% today.")
    # Should flush exactly ONCE at the final period
    flushes = [e for e in ev if e["kind"] == "flush"]
    assert len(flushes) == 1
    # Reassembled text should equal original
    txt = _only_text(ev)
    assert "$202.06" in txt
    assert "0.53%" in txt


def test_sentence_not_split_by_tag_mid_phrase():
    """LLM emits a tag mid-sentence — parser should NOT flush until terminator.

    Real-world case: Qwen3 emits
      <emotion>neutral</emotion>I can't list the cron jobs because the
      scheduler isn't <emotion>neutral</emotion>running.
    Without the guard this splits "isn't" and "running." across bubbles.
    """
    p = InlineTagParser()
    text = (
        "<emotion>neutral</emotion>I can't list the cron jobs because the "
        "scheduler isn't <emotion>neutral</emotion>running."
    )
    ev = _collect(p, text)
    flushes = [e for e in ev if e["kind"] == "flush"]
    # Exactly one flush — at the final period, not mid-sentence
    assert len(flushes) == 1, f"expected 1 flush, got {len(flushes)}"
    txt = _only_text(ev)
    assert "isn't running" in txt


def test_decimal_not_split_by_tag_boundary():
    """Reproduces the SOXL bug — LLM emits a tag mid-decimal.

    Input: "at $39.<gesture>speak_normal</gesture>75 today."
    Expected: one continuous chunk "at $39.75 today." (tag fires, no split).
    """
    p = InlineTagParser()
    text = "at $39.<gesture>speak_normal</gesture>75 today."
    ev = _collect(p, text)
    # There should be exactly ONE flush (end of sentence), not two
    flushes = [e for e in ev if e["kind"] == "flush"]
    assert len(flushes) == 1, f"expected 1 flush, got {len(flushes)}"
    # Check the gesture event did fire (parser recognised the tag)
    gestures = [e for e in ev if e["kind"] == "gesture"]
    assert len(gestures) == 1
    # Concat all text_deltas and verify "$39.75" appears intact
    txt = _only_text(ev)
    assert "$39.75" in txt, f"expected $39.75 intact, got: {txt!r}"


def test_streaming_decimal_split():
    """Decimal split across feed() chunks — must not emit flush mid-decimal."""
    p = InlineTagParser()
    ev: list[dict] = []
    # Simulate token streaming: "NVIDIA's stock is $202." then "06 right now."
    ev.extend(p.feed("NVIDIA's stock is $202."))
    ev.extend(p.feed("06 right now."))
    ev.extend(p.finish())
    # Exactly one flush — at the real sentence end
    flushes = [e for e in ev if e["kind"] == "flush"]
    assert len(flushes) == 1
    txt = _only_text(ev)
    assert "$202.06" in txt


def test_stray_lt_in_prose():
    """'2 < 3' should be treated as literal text, not a tag."""
    p = InlineTagParser()
    ev = _collect(p, "The answer is 2 < 3.")
    txt = _only_text(ev)
    # The '<' must appear in the output
    assert "<" in txt
    assert "2" in txt and "3" in txt


def test_unclosed_emotion_implicit_close():
    """LLM forgot </emotion> — treat body as the emotion ID."""
    p = InlineTagParser()
    ev = _collect(p, "<emotion>happy")
    kinds = _kinds(ev)
    assert "emotion" in kinds
    em = next(e for e in ev if e["kind"] == "emotion")
    assert em["id"] == "happy"


def test_unclosed_gesture_implicit_close():
    """LLM forgot </gesture> — treat body as the gesture name."""
    p = InlineTagParser()
    ev = _collect(p, "<gesture>wave")
    g = next(e for e in ev if e["kind"] == "gesture")
    assert g["name"] == "wave"


def test_unclosed_tool_call_fires_anyway():
    """LLM forgot </tool_call> — still fire the tool with what we have."""
    p = InlineTagParser()
    ev = _collect(p,
        '<tool_call name="get_stock_data">Current AAPL stock price and chart.'
    )
    tcs = [e for e in ev if e["kind"] == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0]["name"] == "get_stock_data"
    assert "AAPL" in tcs[0]["arguments"]
    # Crucially, the body must NOT appear as literal text
    txt = _only_text(ev)
    assert "AAPL" not in txt
    assert "<tool_call" not in txt


def test_unclosed_tool_call_with_preceding_speech():
    """Reproduces the NVIDIA bug — speech, then tool_call without close."""
    p = InlineTagParser()
    text = (
        "<emotion>thinking</emotion><gesture>speak_calm</gesture>"
        "One sec — checking what's new with Nvidia today."
        '<tool_call name="get_stock_data">Current performance and recent news on NVIDIA stock. Include a chart.'
    )
    ev = _collect(p, text)
    kinds = _kinds(ev)
    assert "tool_call" in kinds
    tc = next(e for e in ev if e["kind"] == "tool_call")
    assert tc["name"] == "get_stock_data"
    assert "NVIDIA" in tc["arguments"]
    # The speech before the tool call should be preserved
    txt = _only_text(ev)
    assert "Nvidia today" in txt
    # But the tool_call body must NOT appear as speech
    assert "Include a chart" not in txt


def test_auto_close_on_same_tag_reopen():
    """<emotion>a<emotion>b</emotion> — 'a' is auto-closed when second <emotion> arrives."""
    p = InlineTagParser()
    ev = _collect(p, "<emotion>happy<emotion>sad</emotion>Hi.")
    emotions = [e["id"] for e in ev if e["kind"] == "emotion"]
    assert "happy" in emotions
    assert "sad" in emotions


def test_empty_body_skipped():
    p = InlineTagParser()
    ev = _collect(p, "<emotion></emotion>Hi.")
    emotions = [e for e in ev if e["kind"] == "emotion"]
    assert len(emotions) == 0  # empty body → skipped
    assert _only_text(ev) == "Hi."


def test_whitespace_in_body_stripped():
    p = InlineTagParser()
    ev = _collect(p, "<gesture>  wave  </gesture>Hi.")
    g = next(e for e in ev if e["kind"] == "gesture")
    assert g["name"] == "wave"


# --------------------------------------------------------------------------
#  User's canonical example
# --------------------------------------------------------------------------

def test_users_canonical_example():
    p = InlineTagParser()
    text = (
        "<emotion>neutral</emotion><gesture>pointing_finger_at_board</gesture>"
        "<emotion>happy</emotion><gesture>dance</gesture>"
        "Look here! it's a profit opportunity. Let me find in details "
        '<tool_call name="get_stock_data">caveats in investing in this stock</tool_call>'
    )
    ev = _collect(p, text)
    emotions = [e["id"] for e in ev if e["kind"] == "emotion"]
    gestures = [e["name"] for e in ev if e["kind"] == "gesture"]
    tcs = [e for e in ev if e["kind"] == "tool_call"]
    assert emotions == ["neutral", "happy"]
    assert gestures == ["pointing_finger_at_board", "dance"]
    assert len(tcs) == 1
    assert tcs[0]["name"] == "get_stock_data"
    assert "caveats" in tcs[0]["arguments"]
    txt = _only_text(ev)
    assert "profit opportunity" in txt
    assert "Let me find in details" in txt


# --------------------------------------------------------------------------
#  Runner
# --------------------------------------------------------------------------

def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  OK  {t.__name__}")
        except AssertionError as e:
            failed.append((t.__name__, str(e) or "AssertionError"))
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:
            failed.append((t.__name__, f"{type(e).__name__}: {e}"))
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    if failed:
        print("Failures:")
        for name, msg in failed:
            print(f"  - {name}: {msg}")
        return 1
    return 0


def test_selfclosing_tool_call_fires():
    """<tool_call name="X"/> (self-closing, no body) should still fire.

    Stock Qwen3 emits this shape when calling a zero-arg tool like
    ``show_diary``. Before the fix, the parser silently ignored self-closing
    tool_call tags → diary never opened, no-arg tools never fired.
    """
    p = InlineTagParser()
    ev = _collect(p, '<emotion>neutral</emotion><tool_call name="show_diary"/>')
    tcs = [e for e in ev if e["kind"] == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0]["name"] == "show_diary"
    assert tcs[0]["arguments"] == ""


if __name__ == "__main__":
    import sys
    sys.exit(_run_all())
