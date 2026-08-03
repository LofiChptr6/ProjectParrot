"""Knowledge-graph consult in interpret_node — the structural cure for the
'Anthropic?' confabulation. The consult grounds a relationship question in the
desk's cited graph (or flags a gap) instead of guessing.

Since 2026-08-02 (standalone split) the consult uses kg_neighbors — the desk's
surviving KG read surface — and derives the two-entity relation by scanning the
returned edges. There is no Mocha→desk write anymore (kg_raise_gap was deleted
desk-side 2026-07-20).

Pure-logic + stubbed-proxy tests (no live desk subprocess)."""

import asyncio
import json

import pytest


def _graph():
    import bridge.graph as g
    return g


# ── _kg_fact_from (pure) ─────────────────────────────────────────────────────

def test_kg_fact_matched_edge_carries_quote_and_citation():
    g = _graph()
    obj = {"found": True, "entity": {"name": "AMAZON COM INC"},
           "match": {"subject": "AMAZON COM INC", "rel": "invested_in",
                     "object": "Anthropic", "evidence_id": 20378,
                     "quote": "Amazon has invested up to $8 billion in Anthropic."},
           "other": "Anthropic"}
    fact = g._kg_fact_from(obj)
    assert fact
    assert "Amazon has invested up to $8 billion" in fact
    assert "#20378" in fact          # citation survives into the directive
    assert "KNOWLEDGE GRAPH" in fact


def test_kg_fact_matched_edge_without_quote_falls_back_to_triple():
    g = _graph()
    obj = {"found": True, "entity": {"name": "AMAZON COM INC"},
           "match": {"subject": "AMAZON COM INC", "rel": "invested_in",
                     "object": "Anthropic", "quote": ""},
           "other": "Anthropic"}
    fact = g._kg_fact_from(obj)
    assert fact and "invested_in" in fact and "Anthropic" in fact


def test_kg_fact_known_entity_no_match_says_no_relationship():
    g = _graph()
    obj = {"found": True, "entity": {"name": "AMAZON COM INC"},
           "match": None, "other": "Tesla"}
    fact = g._kg_fact_from(obj)
    assert fact and "no known relationship" in fact.lower()
    assert "Tesla" in fact and "Don't invent" in fact


def test_kg_fact_unknown_entity_returns_none():
    g = _graph()
    assert g._kg_fact_from({"found": False, "match": None, "other": "X"}) is None
    assert g._kg_fact_from(None) is None


# ── _kg_consult (stubbed proxy) ──────────────────────────────────────────────

def test_kg_consult_calls_kg_neighbors_and_scans_edges(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy
    seen = {}

    async def fake_call(tool, args, title):
        seen["tool"] = tool
        seen["args"] = args
        return json.dumps({
            "found": True, "entity": {"name": "AMAZON COM INC"},
            "edges": [
                {"subject": "AMAZON COM INC", "rel": "competes_with",
                 "object": "Walmart", "quote": "…", "evidence_id": 1},
                {"subject": "AMAZON COM INC", "rel": "invested_in",
                 "object": "Anthropic", "evidence_id": 20378,
                 "quote": "Amazon has invested up to $8 billion in Anthropic."},
            ],
        })

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    out = asyncio.run(g._kg_consult(["AMZN", "Anthropic"]))
    assert seen["tool"] == "kg_neighbors"
    assert seen["args"]["entity"] == "AMZN"
    assert out and out["found"] is True
    assert out["match"] and out["match"]["evidence_id"] == 20378
    assert out["other"] == "Anthropic"


def test_kg_consult_no_matching_edge_gives_match_none(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy

    async def fake_call(tool, args, title):
        return json.dumps({
            "found": True, "entity": {"name": "AMAZON COM INC"},
            "edges": [{"subject": "AMAZON COM INC", "rel": "competes_with",
                       "object": "Walmart", "quote": "…"}],
        })

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    out = asyncio.run(g._kg_consult(["AMZN", "Tesla"]))
    assert out and out["found"] is True and out["match"] is None


def test_kg_consult_unknown_entity_reports_not_found(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy

    async def fake_call(tool, args, title):
        return json.dumps({"found": False, "entity": {"query": "Wakanda"},
                           "edges": [], "related": None,
                           "note": "'Wakanda' is not in the knowledge graph yet (a gap)."})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    out = asyncio.run(g._kg_consult(["Wakanda", "AMZN"]))
    assert out and out["found"] is False and out["match"] is None


def test_kg_consult_error_envelope_returns_none(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy

    async def fake_panel(tool, args, title):
        return json.dumps({"__panel__": "create_presentation"})

    async def fake_err(tool, args, title):
        return json.dumps({"error": "boom"})

    monkeypatch.setattr(proxy, "call_opus", fake_panel)
    assert asyncio.run(g._kg_consult(["AMZN", "Anthropic"])) is None
    monkeypatch.setattr(proxy, "call_opus", fake_err)
    assert asyncio.run(g._kg_consult(["AMZN", "Anthropic"])) is None


def test_kg_consult_bad_pair_short_circuits():
    g = _graph()
    assert asyncio.run(g._kg_consult(["AMZN"])) is None
    assert asyncio.run(g._kg_consult([])) is None
    assert asyncio.run(g._kg_consult(["AMZN", ""])) is None
    assert asyncio.run(g._kg_consult(None)) is None


# ── interpret_node wiring ────────────────────────────────────────────────────

def test_interpret_node_attaches_kg_fact(monkeypatch):
    g = _graph()
    from bridge import server as S

    class FakeClient:
        async def chat(self, msgs, **kw):
            return {"content": json.dumps({
                "asking": "is AMZN up because of Anthropic",
                "refers_to": "the AMZN news item",
                "kg_pair": ["AMZN", "Anthropic"],
            })}

    monkeypatch.setattr(S, "llm_deep", FakeClient())
    monkeypatch.setattr(S, "_get_user_history", lambda uid: [])

    async def fake_consult(pair):
        return {"found": True, "entity": {"name": "AMAZON COM INC"},
                "match": {"subject": "AMAZON COM INC", "rel": "invested_in",
                          "object": "Anthropic", "evidence_id": 20378,
                          "quote": "Amazon has invested up to $8 billion in Anthropic."},
                "other": "Anthropic"}

    monkeypatch.setattr(g, "_kg_consult", fake_consult)

    state = {"user_text": "Anthropic?", "reply_context": "Mocha shared: AMZN rose 3% today.",
             "realtime": False, "user_id": "ika"}
    out = asyncio.run(g.interpret_node(state))
    read = out["intent_read"]
    assert read.get("kg_fact") and "#20378" in read["kg_fact"]
    assert not read.get("kg_gap")


def test_interpret_node_flags_gap_when_entity_unknown(monkeypatch):
    g = _graph()
    from bridge import server as S

    class FakeClient:
        async def chat(self, msgs, **kw):
            return {"content": json.dumps({
                "asking": "is AMZN related to Wakanda",
                "kg_pair": ["Wakanda", "AMZN"],
            })}

    monkeypatch.setattr(S, "llm_deep", FakeClient())
    monkeypatch.setattr(S, "_get_user_history", lambda uid: [])

    async def fake_consult(pair):
        return {"found": False, "entity": {"query": "Wakanda"},
                "match": None, "other": "AMZN"}

    monkeypatch.setattr(g, "_kg_consult", fake_consult)
    state = {"user_text": "Wakanda?", "reply_context": "AMZN news", "realtime": False, "user_id": "ika"}
    out = asyncio.run(g.interpret_node(state))
    assert out["intent_read"].get("kg_gap") == ["Wakanda", "AMZN"]
    assert not out["intent_read"].get("kg_fact")


def test_interpret_node_skips_consult_on_realtime(monkeypatch):
    """Web (realtime) path must NOT spawn the desk subprocess — snappiness."""
    g = _graph()
    from bridge import server as S

    class FakeClient:
        async def chat(self, msgs, **kw):
            return {"content": json.dumps({"asking": "x", "kg_pair": ["AMZN", "Anthropic"]})}

    monkeypatch.setattr(S, "llm_client", FakeClient())
    monkeypatch.setattr(S, "_get_user_history", lambda uid: [])
    called = {"n": 0}

    async def boom(pair):
        called["n"] += 1
        return None

    monkeypatch.setattr(g, "_kg_consult", boom)
    state = {"user_text": "Anthropic?", "realtime": True, "user_id": "ika"}
    asyncio.run(g.interpret_node(state))
    assert called["n"] == 0
