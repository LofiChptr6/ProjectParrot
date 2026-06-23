"""Unit tests for the task runtime's pure layers: bridge.task_store + task_templates.

The graph node (task_step) does IO and is validated live behind the
TASK_RUNTIME_ENABLED flag; these cover the control logic that must be correct
regardless. Run: ./.venv/bin/python -m pytest bridge/test_task_runtime.py -q
"""

from __future__ import annotations

import bridge.task_store as ts
from bridge.task_store import ActiveTask
from bridge import task_templates as tt


def setup_function(_):
    ts._reset_for_tests()


# ── store ─────────────────────────────────────────────────────────────────────

def test_start_and_get():
    assert ts.get_active("u") is None
    t = ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="find"))
    assert ts.get_active("u") is t
    assert ts.has_active("u")


def test_update_active_merges_and_stamps():
    ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="find"))
    upd = ts.update_active("u", state="ask", slots={"query": "chopin"})
    assert upd.state == "ask"
    assert ts.get_active("u").slots == {"query": "chopin"}


def test_update_active_no_task_is_noop():
    assert ts.update_active("nobody", state="x") is None


def test_suspend_then_complete_resumes():
    # Active "play_media" gets interrupted by a "look_up", which then completes
    # and the parked play_media is promoted back to active.
    play = ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="ask"))
    ts.suspend("u")
    assert ts.get_active("u") is None
    assert ts.get_suspended("u") is play

    ts.start("u", ActiveTask(kind="look_up", template="fulfill", state="find"))
    resumed = ts.complete_active("u")          # look_up done → resume play_media
    assert resumed is play
    assert ts.get_active("u") is play
    assert ts.get_suspended("u") is None


def test_complete_with_nothing_suspended_clears():
    ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="find"))
    assert ts.complete_active("u") is None
    assert ts.get_active("u") is None


def test_single_suspend_slot_drops_older():
    # v1 is single-depth: a second suspend overwrites the slot (documented).
    a = ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="ask"))
    ts.suspend("u")
    b = ts.start("u", ActiveTask(kind="look_up", template="fulfill", state="ask"))
    ts.suspend("u")
    assert ts.get_suspended("u") is b and a is not b


def test_sweep_timeouts_drops_idle_parked_tasks():
    t = ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="ask"))
    t.last_active_at = 1000.0
    # now=1000+700 with ttl=600 → dropped
    assert ts.sweep_timeouts(ttl_seconds=600, now=1700.0) == 1
    assert ts.get_active("u") is None
    # fresh task survives
    ts.start("u", ActiveTask(kind="play_media", template="fulfill", state="ask")).last_active_at = 1690.0
    assert ts.sweep_timeouts(ttl_seconds=600, now=1700.0) == 0


def test_users_are_partitioned():
    ts.start("a", ActiveTask(kind="play_media", template="fulfill", state="find"))
    assert ts.get_active("a") is not None
    assert ts.get_active("b") is None


# ── templates ─────────────────────────────────────────────────────────────────

def test_registry():
    assert tt.is_task_kind("play_media")
    assert not tt.is_task_kind("not_a_kind")
    assert not tt.is_task_kind(None)


def test_make_task_enters_initial_state():
    t = tt.make_task("play_media", {"query": "chopin concerto"})
    assert t.kind == "play_media" and t.template == "fulfill" and t.state == "find"
    assert t.slots == {"query": "chopin concerto"}


def test_decide_entry_acts_with_deterministic_video_player_args():
    t = tt.make_task("play_media", {"query": "chopin concerto no 2"})
    d = tt.decide_entry(t)
    assert d.action == "act"
    assert d.tool == "video_player"
    # Deterministic args — action=open + query, NO web_search. This is the fix.
    assert d.tool_args == {"action": "open", "query": "chopin concerto no 2"}


def test_decide_entry_asks_when_query_missing():
    t = tt.make_task("play_media", {})        # no query
    d = tt.decide_entry(t)
    assert d.action == "ask"


def test_decide_after_result_succeeds_on_ok():
    t = tt.make_task("play_media", {"query": "x"})
    assert tt.decide_after_result(t, ok=True).action == "succeed"


def test_decide_after_result_play_media_budget_one():
    # play_media sets retry_budget=1 (a same-query retry only covers a transient
    # blip). 1st failure → one retry; once that's spent → ask, don't spin.
    t = tt.make_task("play_media", {"query": "x"})
    t.retry_count = 0
    assert tt.decide_after_result(t, ok=False).action == "act"
    t.retry_count = 1
    assert tt.decide_after_result(t, ok=False).action == "ask"


def test_apply_continuation_merges_and_returns_to_find():
    # The Chopin case: parked at ask, user says "No. 2" → slot refines, back to find.
    t = tt.make_task("play_media", {"query": "chopin concerto"})
    t.state = "ask"
    tt.apply_continuation(t, {"query": "chopin concerto no 2"})
    assert t.state == "find"
    assert t.slots["query"] == "chopin concerto no 2"


# ── routing glue (bridge.graph._apply_task_routing) ───────────────────────────
# This is the cross-turn intent-carry logic: classifier route → store ops. Tested
# without a live LLM by feeding the read dict the classifier would have produced.

from bridge import graph as G   # noqa: E402  (import after store reset helper)


def _route(read, user_id="u"):
    state = {"user_id": user_id, "job_id": 1}
    G._apply_task_routing(state, read)
    return state


def test_routing_start_creates_active_task():
    st = _route({"route": "start", "task_kind": "play_media",
                 "task_slots": {"query": "chopin concerto"}})
    assert st["task_route"] == "task"
    assert st["route"] == "deep"           # tools forced on
    act = ts.get_active("u")
    assert act.kind == "play_media" and act.state == "find"
    assert act.slots == {"query": "chopin concerto"}


def test_routing_chat_leaves_no_task():
    st = _route({"route": "chat", "asking": "how are you"})
    assert st["task_route"] == "chat"
    assert not ts.has_active("u")


def test_routing_unknown_kind_falls_back_to_chat():
    st = _route({"route": "start", "task_kind": "teleport", "task_slots": {}})
    assert st["task_route"] == "chat"
    assert not ts.has_active("u")


def test_routing_continue_refines_parked_task():
    # Turn 1: start with no query → (task_step would ask) park at ask.
    _route({"route": "start", "task_kind": "play_media", "task_slots": {}})
    ts.update_active("u", state="ask")
    # Turn 2: "No. 2" continues — classifier resolved the full query from history.
    st = _route({"route": "continue",
                 "task_slots": {"query": "Chopin Piano Concerto No. 2"}})
    assert st["task_route"] == "task"
    act = ts.get_active("u")
    assert act.state == "find"             # back to acting
    assert act.slots["query"] == "Chopin Piano Concerto No. 2"


def test_routing_interrupt_suspends_then_resumes():
    # play_media #1 parked → interrupt with play_media #2 → #1 suspended.
    _route({"route": "start", "task_kind": "play_media",
            "task_slots": {"query": "chopin"}})
    ts.update_active("u", state="ask")
    first = ts.get_active("u")
    _route({"route": "interrupt", "task_kind": "play_media",
            "task_slots": {"query": "debussy"}})
    assert ts.get_active("u").slots["query"] == "debussy"
    assert ts.get_suspended("u") is first
    # #2 completes → #1 resumes (complete_active promotes the suspended task).
    resumed = ts.complete_active("u")
    assert resumed is first


def test_routing_give_up_clears_and_resumes_suspended():
    # With a suspended task, give-up of the active one resumes the parked one.
    _route({"route": "start", "task_kind": "play_media", "task_slots": {"query": "a"}})
    ts.update_active("u", state="ask")
    parked = ts.get_active("u")
    _route({"route": "interrupt", "task_kind": "play_media", "task_slots": {"query": "b"}})
    st = _route({"route": "give_up"})      # abandon "b" → "a" resumes
    assert ts.get_active("u") is parked
    assert st["task_route"] == "task"

    # And give-up with nothing parked just clears → back to chat.
    ts._reset_for_tests()
    _route({"route": "start", "task_kind": "play_media", "task_slots": {"query": "x"}})
    st2 = _route({"route": "give_up"})
    assert not ts.has_active("u")
    assert st2["task_route"] == "chat"


def test_task_step_act_queues_deterministic_video_player():
    # The actual graph node (not just the pure helper): an active play_media task
    # in "find" must queue video_player(action=open, query=…) — deterministically,
    # with NO web_search. This is what kills the empty-search loop.
    import asyncio
    import json
    ts.start("u", tt.make_task("play_media", {"query": "chopin concerto no 2"}))
    state = {"user_id": "u", "job_id": 7, "tool_round": 0}
    out = asyncio.run(G.task_step_node(state))
    assert out["task_acting"] is True
    tc = out["pending_tool_calls"][0]
    assert tc["name"] == "video_player"
    assert json.loads(tc["arguments"]) == {"action": "open", "query": "chopin concerto no 2"}


# ── stage 1.1: after-tools advance (retry / ask / succeed) + routing ──────────

def _state_with_tool_result(content):
    return {"user_id": "u", "job_id": 1, "task_acting": True,
            "messages": [{"role": "tool", "content": content}]}


def test_advance_success_completes_task():
    ts.start("u", tt.make_task("play_media", {"query": "x"}))
    st = _state_with_tool_result('{"video_id":"abc123","title":"Chopin Concerto"}')
    G._advance_task_after_tools(st)
    assert st["task_outcome"] == "succeed"
    assert st["task_acting"] is False
    assert not ts.has_active("u")          # cleared


def test_advance_failure_retries_once_then_asks_and_parks():
    ts.start("u", tt.make_task("play_media", {"query": "x"}))
    # 1st failure → retry (budget 1), task stays active back in "find".
    st1 = _state_with_tool_result("Tool error: youtube lookup failed")
    G._advance_task_after_tools(st1)
    assert st1["task_outcome"] == "retry"
    act = ts.get_active("u")
    assert act.retry_count == 1 and act.state == "find"
    # 2nd failure → budget spent → ask, task parked at "ask" for the next turn.
    st2 = _state_with_tool_result("couldn't find that video")
    G._advance_task_after_tools(st2)
    assert st2["task_outcome"] == "ask"
    assert st2.get("task_ask_for")
    assert ts.get_active("u").state == "ask"   # parked, not cleared


def test_after_run_tools_routing():
    assert G._after_run_tools({"task_outcome": "retry"}) == "task_step"
    assert G._after_run_tools({"task_outcome": "ask"}) == "task_ask"
    assert G._after_run_tools({"task_outcome": "succeed"}) == "llm_pass"
    # Non-task tool turns never set task_outcome → unchanged ReAct loop-back.
    assert G._after_run_tools({}) == "llm_pass"


def test_sweep_only_drops_idle_parked_tasks():
    # The background sweep (wired into the idle heartbeat) drops a task parked at
    # "ask" once it's idle past the TTL — but not a freshly-touched one.
    t = ts.start("u", tt.make_task("play_media", {"query": "x"}))
    t.state = "ask"
    t.last_active_at = 1000.0
    assert ts.sweep_timeouts(ttl_seconds=600, now=1601.0) == 1
    assert not ts.has_active("u")


# ── stall line: DERIVED from the action, never hallucinated ───────────────────

def test_stall_phrase_video_player_never_says_stocks():
    # The exact bug: finding another nocturne must NOT yield a stock-quote stall.
    line = G._stall_phrase("video_player", "chopin nocturne op 9 no 2", seed=0)
    low = line.lower()
    assert "stock" not in low and "price" not in low and "tesla" not in low
    assert line in ["finding that", "queuing that up", "pulling up a video", "finding a track"]


def test_stall_phrase_stock_uses_the_real_ticker():
    assert "TSLA" in G._stall_phrase("get_stock_data", "TSLA", seed=0)


def test_stall_phrase_stock_no_hint_fabricates_nothing():
    line = G._stall_phrase("get_stock_data", "", seed=0)
    assert line and "TSLA" not in line and "NVIDIA" not in line


def test_stall_phrase_weather_and_news_use_hint():
    assert "Tokyo" in G._stall_phrase("get_weather", "Tokyo", seed=0)
    assert "mars rover" in G._stall_phrase("get_news", "mars rover", seed=0)


def test_stall_phrase_cold_is_a_safe_fixed_line():
    assert G._stall_phrase("", "", seed=0) in G._COLD_STALL_LINES


def test_stall_phrase_desk_family_fallback_is_deskish():
    assert "desk" in G._stall_phrase("get_pnl_summary", "", seed=0).lower()


def test_stall_phrase_unknown_tool_with_hint_names_it():
    assert "weasels" in G._stall_phrase("frobnicate", "weasels", seed=0)


def test_stall_phrase_unknown_tool_no_hint_is_vague():
    assert G._stall_phrase("frobnicate", "", seed=0) in G._COLD_STALL_LINES


def test_stall_phrase_seed_rotates_variants():
    # 4 video_player variants → adjacent seeds shouldn't collide.
    assert G._stall_phrase("video_player", "", 0) != G._stall_phrase("video_player", "", 1)


def test_pending_action_parts_extracts_tool_and_hint():
    import json
    pending = [{"id": "t1", "name": "video_player",
                "arguments": json.dumps({"action": "open", "query": "chopin nocturne"})}]
    name, hint = G._pending_action_parts(pending)
    assert name == "video_player" and hint == "chopin nocturne"
    assert G._pending_action_parts([]) == ("", "")
