"""The 'Want to guess…?' template-closer stripper (2026-08-02).

llm_call_log showed 10 of 12 consecutive autonomy shares ending in the same
'Want to guess/bet/check…?' hook — prompt rules alone didn't kill it, so
_post_filter strips a trailing hook sentence deterministically."""

from autonomy.engine import _strip_template_closer, _post_filter


def test_strips_trailing_want_to_guess():
    t = ("Alphabet beat expectations, Tesla didn't — AI spending is pulling "
         "in different directions. Want to guess why?")
    out = _strip_template_closer(t)
    assert out.endswith("directions.")
    assert "Want to guess" not in out


def test_strips_wanna_bet_variant():
    t = "The same warning has repeated since the 70s. Wanna bet which decade's version was right?"
    out = _strip_template_closer(t)
    assert out.endswith("70s.")


def test_keeps_hook_when_it_is_the_only_sentence():
    t = "Want to guess why?"
    assert _strip_template_closer(t) == t  # near-dup ledger handles repeats


def test_leaves_statement_closers_alone():
    t = ("SK Hynix's chief just called the memory cycle different. "
         "Executives never say that unless they're nervous or right.")
    assert _strip_template_closer(t) == t


def test_leaves_genuine_questions_alone():
    t = "Kwak Noh-Jung's comment feels unusually direct. What do you make of the tone?"
    assert _strip_template_closer(t) == t


def test_post_filter_applies_stripper():
    segs = [{"text": "Tesla slid after hours on the delivery miss. Want to check the chart?",
             "emotion": "curious", "gesture": "speak_normal"}]
    out = _post_filter(segs)
    assert len(out) == 1
    assert "Want to check" not in out[0]["text"]
    assert out[0]["text"].endswith("miss.")
