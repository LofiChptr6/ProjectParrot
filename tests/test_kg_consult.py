"""Knowledge-graph consult in interpret_node — the structural cure for the
'Anthropic?' confabulation. The consult grounds a relationship question in the
desk's cited graph (or flags a gap) instead of guessing.

Pure-logic + stubbed-proxy tests (no live desk subprocess)."""

import asyncio
import json

import pytest


def _graph():
    import bridge.graph as g
    return g


# ── _kg_fact_from (pure) ─────────────────────────────────────────────────────

def test_kg_fact_connected_carries_quote_and_citation():
    g = _graph()
    obj = {"found": True, "entity": {"name": "AMAZON COM INC"},
           "related": {"known": True, "connected": True, "name": "Anthropic"},
           "edges": [{"rel": "invested_in", "evidence_id": 20378,
                      "quote": "Amazon has invested up to $8 billion in Anthropic."}]}
    fact = g._kg_fact_from(obj)
    assert fact
    assert "Amazon has invested up to $8 billion" in fact
    assert "#20378" in fact          # citation survives into the directive
    assert "KNOWLEDGE GRAPH" in fact


def test_kg_fact_known_but_unrelated_says_so():
    g = _graph()
    obj = {"found": True, "entity": {"name": "AMAZON COM INC"},
           "related": {"known": True, "connected": False, "name": "Tesla"},
           "edges": []}
    fact = g._kg_fact_from(obj)
    assert fact and "no known relationship" in fact.lower()


def test_kg_fact_gap_or_unknown_returns_none():
    g = _graph()
    assert g._kg_fact_from({"found": False}) is None
    assert g._kg_fact_from({"found": True, "related": {"known": False}}) is None
    assert g._kg_fact_from({"found": True, "related": None}) is None


# ── _kg_consult (stubbed proxy) ──────────────────────────────────────────────

def test_kg_consult_parses_proxy_json(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy
    seen = {}

    async def fake_call(tool, args, title):
        seen["tool"] = tool
        seen["args"] = args
        return json.dumps({"found": True, "related": {"known": True, "connected": True}})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    out = asyncio.run(g._kg_consult(["AMZN", "Anthropic"]))
    assert out and out["found"] is True
    assert seen["tool"] == "kg_query"
    assert seen["args"]["entity"] == "AMZN" and seen["args"]["related_to"] == "Anthropic"


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
                "related": {"known": True, "connected": True, "name": "Anthropic"},
                "edges": [{"rel": "invested_in", "evidence_id": 20378,
                           "quote": "Amazon has invested up to $8 billion in Anthropic."}]}

    monkeypatch.setattr(g, "_kg_consult", fake_consult)

    state = {"user_text": "Anthropic?", "reply_context": "Mocha shared: AMZN rose 3% today.",
             "realtime": False, "user_id": "ika"}
    out = asyncio.run(g.interpret_node(state))
    read = out["intent_read"]
    assert read.get("kg_fact") and "#20378" in read["kg_fact"]
    assert not read.get("kg_gap")


def test_interpret_node_flags_gap_when_unknown(monkeypatch):
    g = _graph()
    from bridge import server as S

    class FakeClient:
        async def chat(self, msgs, **kw):
            return {"content": json.dumps({
                "asking": "is AMZN related to Wakanda",
                "kg_pair": ["AMZN", "Wakanda"],
            })}

    monkeypatch.setattr(S, "llm_deep", FakeClient())
    monkeypatch.setattr(S, "_get_user_history", lambda uid: [])

    async def fake_consult(pair):
        return {"found": True, "entity": {"name": "AMAZON COM INC"},
                "related": {"query": "Wakanda", "known": False}, "edges": []}

    raised = {}

    async def fake_raise(pair, question):
        raised["pair"] = pair
        raised["question"] = question

    monkeypatch.setattr(g, "_kg_consult", fake_consult)
    monkeypatch.setattr(g, "_kg_raise_gap", fake_raise)
    state = {"user_text": "Wakanda?", "reply_context": "AMZN news", "realtime": False, "user_id": "ika"}
    out = asyncio.run(g.interpret_node(state))
    assert out["intent_read"].get("kg_gap") == ["AMZN", "Wakanda"]
    assert not out["intent_read"].get("kg_fact")
    # the gap was appended to the desk backlog (the one sanctioned Mocha write)
    assert raised.get("pair") == ["AMZN", "Wakanda"]


def test_kg_raise_gap_calls_proxy_append_only(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy
    seen = {}

    async def fake_call(tool, args, title):
        seen["tool"] = tool
        seen["args"] = args
        return json.dumps({"gap_id": 7, "deduped": False})

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    asyncio.run(g._kg_raise_gap(["AMZN", "Anthropic"], "is AMZN tied to Anthropic?"))
    assert seen["tool"] == "kg_raise_gap"           # the ONE write tool
    assert seen["args"]["src_entity"] == "AMZN"
    assert seen["args"]["dst_entity"] == "Anthropic"


def test_kg_raise_gap_bad_pair_is_noop(monkeypatch):
    g = _graph()
    import tools.custom._opus_proxy as proxy
    called = {"n": 0}

    async def fake_call(tool, args, title):
        called["n"] += 1
        return "{}"

    monkeypatch.setattr(proxy, "call_opus", fake_call)
    asyncio.run(g._kg_raise_gap(["AMZN"], "q"))      # only one entity
    asyncio.run(g._kg_raise_gap(None, "q"))
    assert called["n"] == 0


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
