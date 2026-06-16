"""Greeting must be grounded in real items + never invent news (confab fix)."""

import asyncio
import importlib
import json

import pytest


@pytest.fixture()
def cur(tmp_path, monkeypatch):
    import autonomy.curiosity as c
    importlib.reload(c)
    monkeypatch.setattr(c, "_PATH", tmp_path / "curiosity_pool.json")
    return c


@pytest.fixture()
def eng():
    import autonomy.engine as e
    importlib.reload(e)
    return e


def test_peek_is_non_destructive(cur):
    pool = [{"title": "Ceasefire lifts stocks", "source": "schwab.com", "url": "u1"},
            {"title": "Ford slips on China", "source": "ts2.tech", "url": "u2"}]
    cur._PATH.write_text(json.dumps({"pool": pool, "seen": [], "last_refill": 0, "rotation": 0}))
    got = asyncio.run(cur.peek(3))
    assert [g["title"] for g in got] == ["Ceasefire lifts stocks", "Ford slips on China"]
    # Pool unchanged (peek must NOT pop, unlike next_item).
    after = json.loads(cur._PATH.read_text())["pool"]
    assert len(after) == 2


def test_reconnect_grounded_branch_lists_items_and_bans_invention(eng):
    fp = "- Ceasefire lifts stocks — schwab.com (you noticed this)"
    p = eng._internal_prompt_for_state("reconnect", "", findings_preview=fp)
    assert "Ceasefire lifts stocks" in p
    assert "permitted hooks" in p
    assert "HARD RULE" in p and "hallucinating" in p


def test_reconnect_empty_branch_forbids_news(eng):
    p = eng._internal_prompt_for_state("reconnect", "", findings_preview="")
    assert "NO current news" in p
    assert "Do NOT reference any news" in p
    assert "HARD RULE" in p


def test_idle_composers_have_no_facts_clause(eng):
    for state in ("bored", "lonely", "drift"):
        p = eng._internal_prompt_for_state(state, "lofi music")
        assert "do not" in p.lower() or "do NOT" in p
        # each explicitly bans news/company/number invention
        assert ("news" in p.lower()) and ("number" in p.lower() or "company" in p.lower())


def test_news_share_caps_extrapolation(eng):
    p = eng._news_share_prompt({"title": "X earnings", "snippet": "s", "source": "z.com", "kind": "desk"})
    assert "ONLY what's in the headline/snippet" in p
