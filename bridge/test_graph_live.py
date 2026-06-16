"""LIVE end-to-end routing/behaviour tests for the conversational graph.

Unlike `test_graph_routing.py` (pure, deterministic), this drives the REAL graph
through a running bridge (`/admin/eval` with a `source` override) and asserts the
runtime behaviour that needs the LLM/GPU.

HARD assertions (reliable — keyed off the PG call log joined by conversation_id,
and off artifact-free output, never exact wording):
  - model routing      first pass runs on the model `_route_model` picked
  - verifier gating     pass_number=2 present on telegram, absent on the webapp
  - output cleanliness  no leaked <think>/JSON/tags reach the user (any surface)
  - non-empty           where it's reliable (telegram rescues; 3B chat answers)

SOFT observations (reported, not asserted — flaky by nature on a shared GPU):
  - tool escalation     synthesis model trail after a tool fires
  - empty replies       webapp realtime turns can think-truncate to "" (no rescue)
  - doubled units       known weather-handle bug, visible on the verifier-free webapp

Requires the bridge on :8090 and Postgres reachable. Skips cleanly if the bridge
is down.   Run:  python3 -u bridge/test_graph_live.py

IMPORTANT — run this SERIALLY (it already is) and do NOT fire other evals against
the bridge while it runs: the `num:` handle registry is process-global, so
concurrent turns cross-resolve each other's handles (a weather turn can render a
stale stock price as its temperature). It also shares the deep Qwen (max
concurrency 1), so parallel load just queues and slows everything down.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import httpx

BRIDGE = os.environ.get("MOCHA_BRIDGE", "http://127.0.0.1:8090")
PG = dict(host="127.0.0.1", user="mocha", db="mocha", password="5369")
LOG_FLUSH_S = 3.0
EVAL_TIMEOUT_S = 130

_ARTIFACTS = ('<think', '</think', '{"segments', "{'segments", '<emotion',
              '<gesture', '<reads', '<tool_call', '</tool_call', '```')
_DOUBLED = ('°c°c', '%%', 'km/h km/h', 'km/h kph', 'km/h k')


def is_qwen(m): return "qwen" in (m or "").lower()
def is_llama(m): return "llama" in (m or "").lower()


def _eval(text, source, history=None):
    payload = {"text": text, "source": source}
    if history:
        payload["history"] = history
    r = httpx.post(f"{BRIDGE}/admin/eval", json=payload, timeout=EVAL_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _passes(cid):
    sql = ("SELECT coalesce(json_agg(json_build_object("
           "'model',model,'pass',pass_number,'finish',finish_reason) ORDER BY id),'[]') "
           f"FROM llm_call_log WHERE conversation_id='{cid}';")
    env = {**os.environ, "PGPASSWORD": PG["password"]}
    out = subprocess.run(
        ["psql", "-h", PG["host"], "-U", PG["user"], "-d", PG["db"], "-t", "-A", "-c", sql],
        capture_output=True, text=True, env=env, timeout=20)
    try:
        return json.loads(out.stdout.strip() or "[]")
    except Exception:
        return []


def _ctx(text, source, history):
    res = _eval(text, source, history)
    time.sleep(LOG_FLUSH_S)
    passes = _passes(res.get("conversation_id", ""))
    full = (res.get("full_text") or "").strip()
    synth = [p["model"] for p in passes if p.get("pass") == 1]
    return {
        "full": full, "low": full.lower(),
        "first_model": passes[0]["model"] if passes else "",
        "synth_models": synth,
        "has_verify": any(p.get("pass") == 2 for p in passes),
        "n_passes": len(passes),
        "errors": res.get("errors") or [],
    }


PREDICATES = {
    "first_qwen":  lambda c: is_qwen(c["first_model"]),
    "first_llama": lambda c: is_llama(c["first_model"]),
    "verify_ran":  lambda c: c["has_verify"],
    "verify_skip": lambda c: not c["has_verify"],
    "clean":       lambda c: not any(a in c["low"] for a in _ARTIFACTS),
    "nonempty":    lambda c: bool(c["full"]),
}


# (label, text, source, history, [hard predicates])
SPECS = [
    # — model routing → Qwen (deep). nonempty NOT required: a deep webapp turn can
    #   think-truncate to empty (no rescue on realtime); that's a reported finding. —
    ("route weather→Qwen", "what's the weather in tokyo right now", "web", None, ["first_qwen", "clean"]),
    ("route pnl→Qwen",     "how is the pnl looking today",          "web", None, ["first_qwen", "clean"]),
    ("route ticker→Qwen",  "any quick thoughts on $NVDA",           "web", None, ["first_qwen", "clean"]),
    ("route news→Qwen",    "what's the latest space news",          "web", None, ["first_qwen", "clean"]),

    # — model routing → Llama (fast). 3B reliably answers chat, so nonempty holds. —
    ("route hi→Llama",      "hey, how are you",                       "web", None, ["first_llama", "clean", "nonempty"]),
    ("route joke→Llama",    "tell me a joke",                         "web", None, ["first_llama", "clean", "nonempty"]),
    ("route feeling→Llama", "i feel kind of low today",               "web", None, ["first_llama", "clean", "nonempty"]),
    ("route bored→Llama",   "do you ever get bored living on a screen","web", None, ["first_llama", "clean", "nonempty"]),

    # — verifier gating: telegram verifies (pass 2) + rescues (nonempty); web skips —
    ("verify ON  tg weather",  "what's the weather in paris", "telegram", None, ["verify_ran", "clean", "nonempty"]),
    ("verify OFF web weather",  "what's the weather in paris", "web",      None, ["verify_skip", "clean"]),
    ("verify ON  tg chat", "tell me something genuinely interesting", "telegram", None, ["verify_ran", "clean", "nonempty"]),
    ("verify OFF web chat", "tell me something genuinely interesting", "web",      None, ["verify_skip", "clean"]),

    # — cleanliness on surfaces that leaked before —
    ("clean tg loneliness", "what do you think about loneliness", "telegram", None, ["clean", "nonempty", "verify_ran"]),
    ("clean web sanitizer", "what's the weather in berlin",       "web",      None, ["clean"]),

    # — conversational / multi-turn (fast route) —
    ("multiturn empathy", "my manager took credit for my work again", "web",
     [{"role": "user", "content": "i had a rough day at work"},
      {"role": "assistant", "content": "ugh. what happened?"}],
     ["first_llama", "clean", "nonempty"]),
]


def run():
    try:
        httpx.get(f"{BRIDGE}/health", timeout=5).raise_for_status()
    except Exception as e:
        print(f"SKIP: bridge not reachable at {BRIDGE} ({e})")
        return True

    total = passed = spec_fail = 0
    empties = doubled = escalations = 0
    t0 = time.time()
    # MOCHA_LIVE_LIMIT=N runs only the first N specs (handy on a busy GPU).
    limit = int(os.environ.get("MOCHA_LIVE_LIMIT", "0") or "0")
    specs = SPECS[:limit] if limit else SPECS
    for label, text, source, history, must in specs:
        try:
            c = _ctx(text, source, history)
        except Exception as e:
            print(f"\n### {label}: EVAL ERROR — {e}")
            spec_fail += 1
            continue

        fails = [n for n in must if not PREDICATES[n](c)]
        total += len(must)
        passed += len(must) - len(fails)
        if fails:
            spec_fail += 1

        # soft observations
        is_empty = not c["full"]
        is_doubled = any(d in c["low"] for d in _DOUBLED)
        escalated = len(c["synth_models"]) >= 2 and is_qwen(c["synth_models"][-1])
        empties += is_empty
        doubled += is_doubled
        escalations += escalated
        obs = []
        if is_empty: obs.append("EMPTY")
        if is_doubled: obs.append("doubled-units")
        if escalated: obs.append("escalated→Qwen")

        print(f"\n### {label} [{source}] — {'ok' if not fails else 'FAILED ' + str(fails)}")
        print(f"    first={c['first_model'].split('/')[-1] or '-'} "
              f"synth={[m.split('/')[-1] for m in c['synth_models']]} "
              f"verify={c['has_verify']} obs={obs or '-'}")
        print(f"    reply: {c['full'][:140]!r}")
        if c["errors"]:
            print(f"    errors: {c['errors']}")

    dt = time.time() - t0
    print(f"\n{'='*66}")
    print(f"{passed}/{total} HARD assertions passed across {len(specs)} specs "
          f"({spec_fail} spec(s) with a failure) in {dt:.0f}s")
    print(f"observations: {escalations} escalated→Qwen, {empties} empty (web think-trunc), "
          f"{doubled} doubled-units (web weather bug)")
    return spec_fail == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
