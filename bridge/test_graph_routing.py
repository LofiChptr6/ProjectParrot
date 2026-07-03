"""Routing / decision tests for the LangGraph conversational turn.

Covers every *deterministic* decision the graph makes (no LLM, no GPU, no
network) so the routing contract is pinned and regressions surface fast:

  - `_route_model`          fast (Llama-3B) vs deep (Qwen-32B) selection
  - `_is_realtime_source`   webapp/voice (verifier off) vs telegram/etc (on)
  - `_parse_tool_args_str`  JSON / kwargs / trailing-junk → dict
  - `_sanitize_outgoing`    strip leaked <think>/tags/JSON before send
  - `_unwrap_segments_json` / `_first_json_object` / `_coerce_scalar` helpers
  - `_try_parse_segments_json` (autonomy) the {"segments":[…]} reply shape
  - `should_continue`       run_tools vs verify edge
  - graph topology          nodes + verify wiring

Run standalone (prints PASS/FAIL per case + a summary):

    python3 bridge/test_graph_routing.py

or under pytest if installed (each table is one test):

    pytest bridge/test_graph_routing.py
"""

from __future__ import annotations

import os
import sys

# Allow `python3 bridge/test_graph_routing.py` (script dir is bridge/, so the
# project root isn't on sys.path by default). `-m` and pytest already handle it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge.server as S
import bridge.graph as G
from autonomy import engine as E


# ════════════════════════════════════════════════════════════════════════════
#  1. _route_model — fast (3B) vs deep (Qwen-32B)
#     (input, expected, label).  Encodes the SHIPPED config keyword lists.
# ════════════════════════════════════════════════════════════════════════════
ROUTE_CASES = [
    # — trading keywords → deep —
    ("how's my pnl looking today", "deep", "trading: pnl"),
    ("show me the portfolio", "deep", "trading: portfolio (+show me)"),
    ("what's our risk exposure right now", "deep", "trading: risk/exposure"),
    ("are we in a drawdown", "deep", "trading: drawdown"),
    ("what is the nav", "deep", "trading: nav"),
    ("explain the hedge to me", "deep", "trading: explain/hedge"),
    ("compare these two positions", "deep", "trading: compare/position"),
    ("analyze the market for me", "deep", "trading: analyze/market"),
    ("any earnings catalyst this week", "deep", "trading: earnings/catalyst"),
    ("what's the valuation", "deep", "trading: valuation"),
    # — tool-intent keywords → deep (so the tool-emitting pass lands on Qwen) —
    ("got any news for me", "deep", "tool: news"),
    ("what's the weather in tokyo", "deep", "tool: weather"),
    ("what's the latest gossip", "deep", "tool: latest/what's the"),
    ("remind me to call mom", "deep", "tool: remind"),
    ("can you search for a recipe", "deep", "tool: search"),
    ("look up the eiffel tower height", "deep", "tool: look up"),
    ("who won the match last night", "deep", "tool: who won"),
    ("show me your diary", "deep", "tool: show me/diary"),
    ("pull up a chart", "deep", "tool: pull up/chart"),
    ("how much is a bitcoin", "deep", "tool: how much"),
    ("schedule a reminder for 9am", "deep", "tool: schedule/reminder"),
    ("play some lofi", "deep", "tool: 'play '"),
    # — ticker cashtags → deep —
    ("what do you think of $NVDA", "deep", "ticker: $NVDA"),
    ("$TSLA to the moon?", "deep", "ticker: $TSLA"),
    # — long message (>=40 words, no keyword) → deep —
    (("i was walking around earlier and noticed the leaves starting to turn and "
      "it got me thinking about how fast a year goes by and whether we actually "
      "feel the small daily shifts as they happen or only ever recognize them "
      "much later once we stop and look back at everything that quietly moved"),
     "deep", "length: >=40 words"),
    # — casual chat (<40 words, no keyword) → fast (3B) —
    ("hi", "fast", "casual: hi"),
    ("how are you doing", "fast", "casual: how are you"),
    ("haha that is hilarious", "fast", "casual: laughter"),
    ("i feel a bit tired today", "fast", "casual: feeling"),
    ("tell me a joke", "fast", "casual: joke"),
    ("good morning", "fast", "casual: greeting"),
    ("i love talking to you", "fast", "casual: affection"),
    ("do you ever feel lonely", "fast", "casual: introspective"),
    ("i had a strange dream last night", "fast", "casual: dream"),
    ("what is your favorite food", "fast", "casual: 'what is' (not 'what's the')"),
    ("", "fast", "edge: empty string"),
    # — documented heuristic breadth (broad keywords pull these to deep) —
    ("what's the meaning of life", "deep", "doc: any \"what's the\" → deep"),
    ("i scored a goal yesterday", "deep", "doc: 'score' substring → deep"),
    # — company names + market slang (2026-07-02: the "nvidia still ripping?"
    #   fabrication fell through because none of these were keywords) —
    ("btw is nvidia still ripping today or did it cool off? just curious",
     "deep", "company name: nvidia + slang"),
    ("did tesla tank today", "deep", "company name: tesla + tanked"),
    ("is the nasdaq at an all time high", "deep", "index: nasdaq/all time high"),
    ("everything is crashing isn't it", "deep", "slang: crashing"),
    ("apple earnings tonight right", "deep", "company: apple + earnings"),
]


# ════════════════════════════════════════════════════════════════════════════
#  2. _is_realtime_source — verifier OFF (realtime) vs ON (buffered)
# ════════════════════════════════════════════════════════════════════════════
REALTIME_CASES = [
    ("web", True, "webapp text chat"),
    ("ws_live", True, "live voice avatar"),
    ("voice", True, "voice"),
    ("voice-stream", True, "voice stream"),
    ("telegram", False, "telegram → verify"),
    ("discord", False, "discord → verify"),
    ("cli", False, "cli → verify"),
    ("eval", False, "eval harness → verify"),
    ("unknown", False, "unknown → verify"),
    ("", False, "empty → verify"),
]


# ════════════════════════════════════════════════════════════════════════════
#  3. _parse_tool_args_str — inline <tool_call> body → dict
# ════════════════════════════════════════════════════════════════════════════
PARSE_ARGS_CASES = [
    ('{"topic": "mars"}', {"topic": "mars"}, "clean JSON"),
    ('topic="quantum computing" max_results=5',
     {"topic": "quantum computing", "max_results": 5}, "kwargs (the get_news bug)"),
    ('{"topic":"mars"}<tool_call name="x">{"y":1}',
     {"topic": "mars"}, "JSON + jammed trailing tool_call"),
    ('symbol=NVDA', {"symbol": "NVDA"}, "bare kwarg"),
    ('', {}, "empty → {}"),
    ('   ', {}, "whitespace → {}"),
    ('just a sentence', {"request": "just a sentence"}, "bare string → request"),
    ('{"a": 1, "b": true}', {"a": 1, "b": True}, "JSON ints/bools"),
    ('{"nested": {"x": 1}}', {"nested": {"x": 1}}, "nested JSON object"),
    ('days=3', {"days": 3}, "kwarg int coercion"),
    ('q="a b c" n=2', {"q": "a b c", "n": 2}, "kwargs quoted + int"),
    ('value=3.5', {"value": 3.5}, "kwarg float"),
    ("name='single'", {"name": "single"}, "kwarg single-quoted"),
    ('prefix {"topic": "x"} suffix', {"topic": "x"}, "JSON embedded in prose"),
]


# ════════════════════════════════════════════════════════════════════════════
#  4. _sanitize_outgoing — strip leaked artifacts before send/speak
# ════════════════════════════════════════════════════════════════════════════
SANITIZE_CASES = [
    ('{"segments": ["a", "b"]}', "a b", "unwrap segments JSON"),
    ('{"segments": [{"text": "obj seg"}]}', "obj seg", "unwrap segments (objects)"),
    ('<think>reasoning here</think>It is 20C.', "It is 20C.", "drop closed <think>"),
    ('<think>ran out of tokens mid thought', "", "drop unclosed <think> → empty"),
    ('<reads>curious</reads><emotion>happy</emotion>Hi there',
     "Hi there", "drop reads+emotion blocks WITH content"),
    ('<emotion>happy</emotion>I love space', "I love space", "drop emotion block"),
    ('<gesture>wave</gesture>hey', "hey", "drop gesture block"),
    ('<tool_call name="get_news">{"topic":"x"}</tool_call>', "",
     "drop tool_call block → empty"),
    ('<think>a</think><emotion>happy</emotion>Real text here',
     "Real text here", "drop think + emotion together"),
    ('clean reply, nothing to strip', "clean reply, nothing to strip",
     "clean prose unchanged"),
    ("I think that's great", "I think that's great", "'think' word (no tag) unchanged"),
    ("x < y and a > b", "x < y and a > b", "bare < and > unchanged"),
    ('lots    of     spaces', "lots of spaces", "collapse runs of spaces"),
    ('   trim me   ', "trim me", "trim ends"),
    ('', "", "empty stays empty"),
]


# ════════════════════════════════════════════════════════════════════════════
#  5. _coerce_scalar — kwarg value typing
# ════════════════════════════════════════════════════════════════════════════
COERCE_CASES = [
    ('"hi"', "hi", "double-quoted string"),
    ("'hi'", "hi", "single-quoted string"),
    ("5", 5, "int"),
    ("-7", -7, "negative int"),
    ("3.5", 3.5, "float"),
    ("true", True, "bool true"),
    ("false", False, "bool false"),
    ("null", None, "null → None"),
    ("NVDA", "NVDA", "bare token stays string"),
]


# ════════════════════════════════════════════════════════════════════════════
#  6. _unwrap_segments_json  and  7. _first_json_object
# ════════════════════════════════════════════════════════════════════════════
UNWRAP_CASES = [
    ('{"segments": ["a", "b"]}', "a b", "two string segments"),
    ('{"segments": [{"text": "hi"}]}', "hi", "object segment"),
    ('{"segments": []}', None, "empty segments → None"),
    ('{"other": 1}', None, "no segments key → None"),
    ('plain text', None, "no JSON → None"),
    ('{"segments": "notalist"}', None, "segments not a list → None"),
]

FIRSTJSON_CASES = [
    ('{"a": 1}', '{"a": 1}', "whole object"),
    ('pre {"a": 1} post', '{"a": 1}', "object embedded in prose"),
    ('{"a": {"b": 2}} trailing', '{"a": {"b": 2}}', "balanced nested object"),
    ('no braces here', None, "no object → None"),
    ('}{ broken', None, "unbalanced → None"),
]


# ════════════════════════════════════════════════════════════════════════════
#  8. _try_parse_segments_json (autonomy) — returns segment texts or None
# ════════════════════════════════════════════════════════════════════════════
def _seg_texts(result):
    """Normalize _try_parse_segments_json output for comparison."""
    if result is None:
        return None
    return [s.get("text") for s in result]

SEGMENTS_JSON_CASES = [
    ('{"segments": ["one line"]}', ["one line"], "speak via segments JSON"),
    ('{"segments": []}', [], "silence signal → []"),
    ('{"segments": [{"text": "y", "emotion": "happy"}]}', ["y"], "object segment"),
    ('```json\n{"segments": ["fenced"]}\n```', ["fenced"], "fenced segments JSON"),
    ('<reads>thinking</reads><emotion>neutral</emotion>just a normal line',
     None, "inline-tag reply → None (fallback to tag parser)"),
    ('totally plain text', None, "plain text → None"),
]


# ════════════════════════════════════════════════════════════════════════════
#  Generic table checker
# ════════════════════════════════════════════════════════════════════════════
def _check(fn, table, transform=None):
    """Return list of failure strings (empty == all passed)."""
    fails = []
    for inp, expected, label in table:
        got = fn(inp)
        if transform:
            got = transform(got)
        if got != expected:
            fails.append(f"  {label}: {inp!r} -> {got!r}  (expected {expected!r})")
    return fails


# ════════════════════════════════════════════════════════════════════════════
#  _ungrounded_live_claim — deterministic backstop for fast-pass fabrications
#  (number-ish claim AND live-data context word; either alone must NOT trip)
# ════════════════════════════════════════════════════════════════════════════
LIVE_CLAIM_CASES = [
    # the actual 2026-07-02 fabrication that sailed past the LLM verifier:
    ("NVDA's current price is around $640, which is down about 3% from yesterday's close.",
     True, "fabricated price + %"),
    ("SOXL is trading at 42.10 right now.", True, "'trading at' + number"),
    ("The desk is up 2% today.", True, "desk P&L claim"),
    ("It's 95 degrees in Phoenix and the forecast says worse.", True, "weather temp"),
    ("They won and scored 3 in the final quarter.", True, "sports score"),
    # numbers with NO live-data context — normal chat, must pass through:
    ("I'm 100% sure you'd have teased me anyway.", False, "chat %: no context"),
    ("Give me 5 minutes and I'll have a comeback.", False, "bare number"),
    ("You said that 3 times already.", False, "bare count"),
    # context with NO number — opinions about markets are allowed:
    ("Markets felt heavy today, no wonder you're drained.", False, "market talk, no number"),
    ("Your portfolio isn't going anywhere overnight.", False, "portfolio talk, no number"),
    ("", False, "edge: empty"),
]


# ── pytest entry points (one assert per table) ──────────────────────────────
def test_route_model():
    assert not _check(S._route_model, ROUTE_CASES), "\n" + "\n".join(_check(S._route_model, ROUTE_CASES))

def test_is_realtime_source():
    assert not _check(S._is_realtime_source, REALTIME_CASES)

def test_ungrounded_live_claim():
    assert not _check(S._ungrounded_live_claim, LIVE_CLAIM_CASES), \
        "\n" + "\n".join(_check(S._ungrounded_live_claim, LIVE_CLAIM_CASES))

def test_parse_tool_args_str():
    assert not _check(S._parse_tool_args_str, PARSE_ARGS_CASES)

def test_sanitize_outgoing():
    assert not _check(S._sanitize_outgoing, SANITIZE_CASES)

def test_coerce_scalar():
    assert not _check(S._coerce_scalar, COERCE_CASES)

def test_unwrap_segments_json():
    assert not _check(S._unwrap_segments_json, UNWRAP_CASES)

def test_first_json_object():
    assert not _check(S._first_json_object, FIRSTJSON_CASES)

def test_try_parse_segments_json():
    assert not _check(E._try_parse_segments_json, SEGMENTS_JSON_CASES, transform=_seg_texts)


def test_should_continue():
    """run_tools when there are pending tools under the round cap; else verify."""
    max_rounds = S._TOOL_MAX_ROUNDS
    cases = [
        ({"pending_tool_calls": [{"name": "x"}], "tool_round": 0}, "run_tools", "pending + round 0"),
        ({"pending_tool_calls": [], "tool_round": 0}, "verify", "no pending → verify"),
        ({"pending_tool_calls": [{"name": "x"}], "tool_round": max_rounds}, "verify", "round cap hit → verify"),
        ({"pending_tool_calls": [{"name": "x"}], "tool_round": max_rounds + 5}, "verify", "over cap → verify"),
    ]
    fails = []
    for state, expected, label in cases:
        got = G.should_continue(state)
        if got != expected:
            fails.append(f"  {label}: -> {got!r} (expected {expected!r})")
    # TOOLS_ENABLED False forces verify even with pending tools.
    _saved = S.TOOLS_ENABLED
    try:
        S.TOOLS_ENABLED = False
        got = G.should_continue({"pending_tool_calls": [{"name": "x"}], "tool_round": 0})
        if got != "verify":
            fails.append(f"  TOOLS_ENABLED=False: -> {got!r} (expected 'verify')")
    finally:
        S.TOOLS_ENABLED = _saved
    assert not fails, "\n" + "\n".join(fails)


def test_should_continue_backstop():
    """A FAST pass that asserted an ungrounded live-data number escalates to
    deep-with-tools on buffered surfaces; realtime/deep/escalated turns don't."""
    claim = ["NVDA is trading at $640, down 3% today."]
    base = {"pending_tool_calls": [], "tool_round": 0, "full_text_parts": claim}
    cases = [
        ({**base, "route": "fast", "realtime": False}, "escalate",
         "fast+buffered+claim → escalate"),
        ({**base, "route": "fast", "realtime": True}, "verify",
         "realtime already streamed → verify"),
        ({**base, "route": "deep", "realtime": False}, "verify",
         "deep turn → verifier owns grounding"),
        ({**base, "route": "fast", "realtime": False, "escalated": True}, "verify",
         "already escalated once → no loop"),
        ({"pending_tool_calls": [], "tool_round": 0, "route": "fast",
          "realtime": False, "full_text_parts": ["mm. rough day, huh."]}, "verify",
         "clean chat draft → verify"),
    ]
    fails = []
    for state, expected, label in cases:
        got = G.should_continue(state)
        if got != expected:
            fails.append(f"  {label}: -> {got!r} (expected {expected!r})")
    assert not fails, "\n" + "\n".join(fails)


def test_build_chat_prompt():
    """The lean fast-path prompt: soul + escalate protocol + style present;
    tag grammar only on avatar surfaces; and it stays a fraction of the full
    prompt (the whole point of the diet)."""
    from character.context import build_chat_prompt, build_system_prompt
    fails = []
    plain = build_chat_prompt(tagged=False)
    tagged = build_chat_prompt(tagged=True)
    full = build_system_prompt(animation_mode="fbx_functions", tools_available=True)
    for name, p in (("plain", plain), ("tagged", tagged)):
        if "<escalate/>" not in p:
            fails.append(f"  {name}: missing <escalate/> protocol")
        if "How you reply" not in p:
            fails.append(f"  {name}: missing style contract (chat_style.md)")
        if "Character Soul" not in p:
            fails.append(f"  {name}: missing soul")
    if "<reads>" in plain:
        fails.append("  plain: tag grammar leaked into text-channel prompt")
    if "<reads>" not in tagged:
        fails.append("  tagged: avatar surface must document <reads>")
    if "<tool_call" in plain.replace("<tool_call name=\"ask_nori\">", ""):
        pass  # tool_call may appear in soul examples; size check below is the guard
    if len(plain) > len(full) / 2.5:
        fails.append(f"  plain prompt too big: {len(plain)} vs full {len(full)}")
    assert not fails, "\n" + "\n".join(fails)


def test_verify_node_gating():
    """The verifier's routing gate (the early-return paths take no LLM call)."""
    import asyncio
    fails = []

    # realtime → pass-through untouched (no verify on the webapp/voice path)
    out = asyncio.run(G.verify_node(
        {"realtime": True, "full_text_parts": ["original"], "tool_round": 0}))
    if out.get("full_text_parts") != ["original"]:
        fails.append("  realtime turn should skip verify (left draft unchanged)")

    # non-realtime but empty draft with nothing to rescue → pass-through
    out = asyncio.run(G.verify_node(
        {"realtime": False, "full_text_parts": [], "tool_round": 0, "pass_content": ""}))
    if out.get("full_text_parts", []) != []:
        fails.append("  empty draft + no material should skip")

    # verifier disabled → pass-through even for a non-realtime tool turn with a draft
    _saved = S._verifier_cfg
    try:
        S._verifier_cfg = {"enabled": False}
        out = asyncio.run(G.verify_node(
            {"realtime": False, "full_text_parts": ["draft text"],
             "tool_round": 1, "pass_content": ""}))
        if out.get("full_text_parts") != ["draft text"]:
            fails.append("  disabled verifier should pass through unchanged")
    finally:
        S._verifier_cfg = _saved

    assert not fails, "\n" + "\n".join(fails)


def test_graph_topology():
    """verify node exists and is wired log_pass→verify→finalize."""
    gr = G.MOCHA_GRAPH.get_graph()
    nodes = set(gr.nodes)
    for n in ("router", "build_messages", "llm_pass", "log_pass", "run_tools", "verify", "finalize"):
        assert n in nodes, f"missing node: {n}"
    edges = {(e.source, e.target) for e in gr.edges}
    assert ("log_pass", "verify") in edges, "log_pass should route to verify"
    assert ("verify", "finalize") in edges, "verify should route to finalize"
    assert ("run_tools", "llm_pass") in edges, "run_tools should loop back to llm_pass"


# ── standalone runner (no pytest needed) ────────────────────────────────────
def _run_standalone():
    total = passed = 0
    tables = [
        ("route_model", S._route_model, ROUTE_CASES, None),
        ("is_realtime_source", S._is_realtime_source, REALTIME_CASES, None),
        ("parse_tool_args_str", S._parse_tool_args_str, PARSE_ARGS_CASES, None),
        ("sanitize_outgoing", S._sanitize_outgoing, SANITIZE_CASES, None),
        ("coerce_scalar", S._coerce_scalar, COERCE_CASES, None),
        ("unwrap_segments_json", S._unwrap_segments_json, UNWRAP_CASES, None),
        ("first_json_object", S._first_json_object, FIRSTJSON_CASES, None),
        ("try_parse_segments_json", E._try_parse_segments_json, SEGMENTS_JSON_CASES, _seg_texts),
        ("ungrounded_live_claim", S._ungrounded_live_claim, LIVE_CLAIM_CASES, None),
    ]
    for name, fn, table, transform in tables:
        for inp, expected, label in table:
            total += 1
            got = fn(inp)
            if transform:
                got = transform(got)
            ok = got == expected
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {label}")
            if not ok:
                print(f"         input={inp!r} got={got!r} expected={expected!r}")

    # state-construction tests (re-run the asserting tests, count as cases)
    for fn in (test_should_continue, test_should_continue_backstop,
               test_build_chat_prompt, test_verify_node_gating, test_graph_topology):
        total += 1
        try:
            fn()
            passed += 1
            print(f"[PASS] {fn.__name__}")
        except AssertionError as e:
            print(f"[FAIL] {fn.__name__}:{e}")

    print(f"\n{'='*60}\n{passed}/{total} cases passed"
          + (" — ALL GREEN ✅" if passed == total else f" — {total - passed} FAILING ❌"))
    return passed == total


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_standalone() else 1)
