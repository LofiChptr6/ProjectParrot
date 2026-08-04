"""Variety & engagement fixes for the autonomy news-share loop (2026-08-03).

The production audit of llm_call_log found three failure modes:
  1. The same SUBJECT pushed repeatedly in one day (DBC 4×, CoStar's CFO 2×) —
     the curiosity ledger dedups by article URL, not by entity.
  2. ~10 shares/day for weeks while Ika replied to almost none — caps bound
     volume, but nothing adapted to being ignored.
  3. Every share ended on the same "Want to guess…?" hook — the ban + stripper
     exist, but an 8B model needs a concrete per-share shape directive.

These tests cover the pure decision layer: subject-key extraction, the daily
dedup check, the unanswered-shares suppression predicate, and the closer-shape
rotation + question enforcement.
"""

import importlib
import time

import pytest

import autonomy.engine as engine


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    import autonomy.engine as e
    importlib.reload(e)
    monkeypatch.setattr(e, "_STATE_PATH", tmp_path / "autonomy_state.json")
    e._persist = None  # start from a clean on-disk state
    return e


# ---------------------------------------------------------------------------
#  Subject-key extraction
# ---------------------------------------------------------------------------

def test_ticker_key_extracted_dbc_example():
    # The audit's worst offender: four different DBC articles in one day.
    keys = engine._extract_subject_keys(
        "Invesco DB Commodity Index Tracking Fund (DBC) Shares Sold by Cetera",
        "Cetera Investment Advisers trimmed its position in DBC in the first quarter.")
    assert "dbc" in keys


def test_dollar_cashtag_converges_with_bare_ticker():
    assert "dbc" in engine._extract_subject_keys("Why $DBC keeps sliding", "")


def test_stoplist_blocks_common_acronyms():
    keys = engine._extract_subject_keys(
        "AI boom lifts US GDP as CEO pay hits ETF records", "")
    for bad in ("ai", "us", "gdp", "ceo", "etf", "the"):
        assert bad not in keys


def test_camelcase_names_extracted():
    assert "costar" in engine._extract_subject_keys(
        "CoStar's CFO resigns after expense probe", "")
    assert "deepseek" in engine._extract_subject_keys(
        "DeepSeek quietly updates its flagship model", "")


def test_repeated_plain_name_is_salient():
    keys = engine._extract_subject_keys(
        "Tesla slides after hours on delivery miss",
        "Tesla shares fell 4% after the company reported deliveries below estimates.")
    assert "tesla" in keys


def test_possessive_capitalized_word_is_a_key():
    assert "nvidia" in engine._extract_subject_keys(
        "Nvidia's data-center run isn't slowing", "")


def test_title_case_headline_words_do_not_become_keys():
    # Every word is capitalized in a headline; none repeat → no plain-name key.
    keys = engine._extract_subject_keys("Fed Holds Rates Steady Into June Meeting", "")
    assert keys == set()


def test_empty_item_yields_no_keys():
    assert engine._extract_subject_keys("", "") == set()


# ---------------------------------------------------------------------------
#  Daily dedup — skip an item whose subject was already shared today
# ---------------------------------------------------------------------------

def test_subject_conflict_detects_same_entity_across_articles():
    item = {"title": "DBC ETF sees unusual options volume", "snippet": ""}
    assert engine._news_subject_conflict(item, shared={"dbc"}) == {"dbc"}
    assert engine._news_subject_conflict(item, shared={"nvda"}) == set()


def test_note_news_share_records_subjects_for_the_day(eng):
    eng._note_news_share({"title": "CoStar CFO resigns", "snippet": ""})
    assert "costar" in eng._subjects_shared_today()
    # A DIFFERENT article about the same entity now conflicts → gets skipped.
    item2 = {"title": "CoStar Group names interim CFO", "snippet": ""}
    assert eng._news_subject_conflict(item2)
    # Survives a bridge restart (state is persisted).
    eng._persist = None
    assert "costar" in eng._subjects_shared_today()


def test_share_counter_advances_with_each_recorded_share(eng):
    assert int(eng._get_persist().get("share_count", 0)) == 0
    eng._note_news_share({"title": "DBC slides again", "snippet": ""})
    eng._note_news_share({"title": "Tesla's deliveries miss", "snippet": ""})
    assert int(eng._get_persist().get("share_count", 0)) == 2


def test_fresh_state_has_variety_fields(eng):
    p = eng._get_persist()
    assert p["shared_subjects"] == []
    assert p["share_times"] == []
    assert p["share_count"] == 0
    assert p["last_user_epoch"] == 0.0


# ---------------------------------------------------------------------------
#  Engagement-adaptive quiet — 3 unanswered shares → silence until Ika speaks
# ---------------------------------------------------------------------------

def test_three_unanswered_shares_suppress():
    now = time.time()
    shares = [now - 3000, now - 2000, now - 1000]
    assert engine._should_suppress_for_disengagement(shares, now - 4000) is True


def test_user_reply_after_newest_share_lifts_suppression():
    now = time.time()
    shares = [now - 3000, now - 2000, now - 1000]
    # Ika spoke AFTER the newest share → not all unanswered → she may speak.
    assert engine._should_suppress_for_disengagement(shares, now - 500) is False


def test_user_reply_between_shares_prevents_suppression():
    now = time.time()
    shares = [now - 3000, now - 2000, now - 1000]
    # Ika spoke between share 2 and share 3 → only one share is unanswered.
    assert engine._should_suppress_for_disengagement(shares, now - 1500) is False


def test_fewer_than_three_shares_never_suppress():
    now = time.time()
    assert engine._should_suppress_for_disengagement([], now - 100) is False
    assert engine._should_suppress_for_disengagement([now - 200, now - 100], 0.0) is False


def test_no_user_activity_ever_and_three_shares_suppress():
    now = time.time()
    shares = [now - 300, now - 200, now - 100]
    assert engine._should_suppress_for_disengagement(shares, 0.0) is True


def test_suppression_handles_unsorted_stamps():
    now = time.time()
    shares = [now - 1000, now - 3000, now - 2000]  # shuffled
    assert engine._should_suppress_for_disengagement(shares, now - 4000) is True
    assert engine._should_suppress_for_disengagement(shares, now - 1500) is False


def test_only_the_most_recent_shares_count():
    now = time.time()
    # Ancient unanswered shares + a recent reply + only 2 shares since → free.
    shares = [now - 90000, now - 80000, now - 70000, now - 2000, now - 1000]
    assert engine._should_suppress_for_disengagement(shares, now - 3000) is False


def test_presence_tracks_wall_clock_epoch():
    import autonomy.presence as presence
    importlib.reload(presence)
    assert presence.last_activity_epoch() == 0.0
    presence.note_user_activity()
    assert abs(presence.last_activity_epoch() - time.time()) < 5.0


# ---------------------------------------------------------------------------
#  Closer-shape rotation + question enforcement
# ---------------------------------------------------------------------------

def test_shape_rotation_cycles_through_four_shapes():
    ids = [engine._share_shape(i)[0] for i in range(8)]
    assert ids == ["statement", "take", "callback", "question"] * 2


def test_shape_directives_are_nonempty_and_distinct():
    directives = {engine._share_shape(i)[1] for i in range(4)}
    assert len(directives) == 4
    assert all(d.strip() for d in directives)


def test_only_question_shape_allows_a_question_close():
    assert engine._shape_allows_question("question") is True
    for shape in ("statement", "take", "callback"):
        assert engine._shape_allows_question(shape) is False


def test_enforce_drops_trailing_question_after_statement():
    segs = [{"text": ("Copper just hit a record. Feels like the grid buildout "
                      "is the quiet story. What do you think?"),
             "emotion": "curious", "gesture": ""}]
    out = engine._enforce_statement_close(segs)
    assert len(out) == 1
    assert out[0]["text"].endswith("story.")
    assert "What do you think" not in out[0]["text"]


def test_enforce_keeps_lone_question():
    # Imperfect beats silent: a share that is ONLY a question survives.
    segs = [{"text": "What would it take for copper to crack?",
             "emotion": "curious", "gesture": ""}]
    assert engine._enforce_statement_close(segs) == segs


def test_enforce_drops_lone_question_segment_when_statement_precedes():
    segs = [{"text": "Copper hit a record today.", "emotion": "neutral", "gesture": ""},
            {"text": "Want to know my theory?", "emotion": "playful", "gesture": ""}]
    out = engine._enforce_statement_close(segs)
    assert len(out) == 1
    assert out[0]["text"] == "Copper hit a record today."


def test_enforce_leaves_statement_closers_alone():
    segs = [{"text": ("SK Hynix's chief just called the memory cycle different. "
                      "Executives never say that unless they're nervous or right."),
             "emotion": "thoughtful", "gesture": ""}]
    assert engine._enforce_statement_close(segs) == segs


def test_enforce_leaves_double_question_alone():
    # A question preceded by another question isn't the tacked-on-hook shape.
    segs = [{"text": "Why now? Why copper?", "emotion": "curious", "gesture": ""}]
    assert engine._enforce_statement_close(segs) == segs


def test_strip_question_closer_requires_preceding_statement():
    assert engine._strip_question_closer("It doubled in a month. Weird, right?") == \
        "It doubled in a month."
    assert engine._strip_question_closer("Weird, right?") == "Weird, right?"
