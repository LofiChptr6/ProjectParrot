"""Phase D Mocha hook: _kg_news_annotation garnishes a news share with a cited
KG relationship via the read-only proxy. Stubbed proxy; fail-silent contract."""
import asyncio
import json

import pytest


def _eng():
    import autonomy.engine as e
    return e


def test_annotation_uses_symbol_and_formats_with_citation(monkeypatch):
    eng = _eng()
    import tools.custom._opus_proxy as proxy
    seen = {}

    async def fake_call(tool, args, title):
        seen["tool"] = tool
        seen["entity"] = args.get("entity")
        return json.dumps({"found": True, "entity": {"name": "AMAZON COM INC"}, "edges": [
            {"subject": "AMAZON COM INC", "subject_ticker": "AMZN", "rel": "invested_in",
             "object": "Anthropic", "object_ticker": None, "evidence_id": 20378}]})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    out = asyncio.run(eng._kg_news_annotation({"title": "Amazon news", "symbol": "AMZN"}))
    assert seen["tool"] == "kg_neighbors" and seen["entity"] == "AMZN"
    assert "AMZN invested_in Anthropic" in out and "#20378" in out


def test_annotation_regex_fallback_skips_stopwords(monkeypatch):
    eng = _eng()
    import tools.custom._opus_proxy as proxy
    called = {"n": 0}

    async def fake_call(tool, args, title):
        called["n"] += 1
        return json.dumps({"found": False})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    # title has only stopword-ish all-caps (AI, CEO) → no entity → no proxy call
    out = asyncio.run(eng._kg_news_annotation({"title": "The new AI CEO speaks"}))
    assert out is None and called["n"] == 0


def test_annotation_fail_silent_on_error_envelope(monkeypatch):
    eng = _eng()
    import tools.custom._opus_proxy as proxy

    async def fake_err(tool, args, title):
        return json.dumps({"error": "boom"})

    monkeypatch.setattr(proxy, "call_opus", fake_err)
    assert asyncio.run(eng._kg_news_annotation({"symbol": "AMZN", "title": "x"})) is None


def test_annotation_none_when_no_edges(monkeypatch):
    eng = _eng()
    import tools.custom._opus_proxy as proxy

    async def fake_call(tool, args, title):
        return json.dumps({"found": True, "edges": []})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    assert asyncio.run(eng._kg_news_annotation({"symbol": "ZZZZ", "title": "x"})) is None
