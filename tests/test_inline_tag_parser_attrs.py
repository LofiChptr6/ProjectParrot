"""tool_call tags with stray attributes must parse (and fold into args), not leak."""

import importlib
import json

import pytest


@pytest.fixture()
def P():
    import bridge.inline_tag_parser as m
    importlib.reload(m)
    return m.InlineTagParser


def _run(P, text):
    p = P()
    evs = p.feed(text) + p.finish()
    return evs


def _tool_calls(evs):
    return [e for e in evs if e.get("kind") == "tool_call"]


def _text(evs):
    return "".join(e["text"] for e in evs if e.get("kind") == "text_delta")


def test_stray_action_attr_folded_into_args(P):
    # The exact shape from the bug report: action="open" as a tag attribute.
    evs = _run(P, '<tool_call name="video_player" action="open">{"query": "lofi"}</tool_call>')
    tcs = _tool_calls(evs)
    assert len(tcs) == 1
    assert tcs[0]["name"] == "video_player"
    assert json.loads(tcs[0]["arguments"]) == {"action": "open", "query": "lofi"}
    # And nothing leaked as spoken text.
    assert "tool_call" not in _text(evs) and "<" not in _text(evs)


def test_self_closing_with_extra_attr(P):
    evs = _run(P, '<tool_call name="show_diary" date="2026-06-15"/>')
    tcs = _tool_calls(evs)
    assert len(tcs) == 1 and tcs[0]["name"] == "show_diary"
    assert json.loads(tcs[0]["arguments"]) == {"date": "2026-06-15"}


def test_explicit_json_key_wins_over_attr(P):
    evs = _run(P, '<tool_call name="video_player" action="open">{"action": "close"}</tool_call>')
    assert json.loads(_tool_calls(evs)[0]["arguments"]) == {"action": "close"}


def test_plain_tool_call_unchanged(P):
    evs = _run(P, '<tool_call name="get_news">{"topic": "mars"}</tool_call>')
    tcs = _tool_calls(evs)
    assert len(tcs) == 1 and tcs[0]["name"] == "get_news"
    assert json.loads(tcs[0]["arguments"]) == {"topic": "mars"}


def test_simple_tags_still_work(P):
    evs = _run(P, "<reads>playful</reads><emotion>happy</emotion><gesture>wave</gesture>Hi.")
    kinds = {e["kind"]: e for e in evs}
    assert kinds["reads"]["state"] == "playful"
    assert kinds["emotion"]["id"] == "happy"
    assert kinds["gesture"]["name"] == "wave"
    assert "Hi." in _text(evs)
