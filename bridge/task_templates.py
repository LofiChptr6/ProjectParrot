"""Task runtime — the template library + the pure decision logic.

A *template* is a tiny state machine plus a contract for which node calls which
tool. Capabilities are instances of a template (see ``TASK_KINDS``), so the whole
toolkit collapses onto ~3 shapes rather than a bespoke graph per feature.

Stage 1 ships exactly one template (``fulfill``) and one capability (``play_media``).
Stage 2 adds capabilities as data; stage 3 adds ``slot_fill``; stage 4 ``confirm``.

Everything here is pure (no LLM, no IO, no graph) so the control logic is fully
unit-testable. ``bridge/graph.py:task_step`` does the IO around these decisions.
See docs/task_runtime_design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge.task_store import ActiveTask

# How many times ``fulfill`` retries the primary tool before degrading to a
# clarifying question (NOT to silent give-up — the tail is where things hide).
FULFILL_RETRY_BUDGET = 2


# ── capability registry ───────────────────────────────────────────────────────
# Each entry is data: which template, which tool, how to build its args from slots,
# which slots are required to act, and the template's entry state. Adding a stage-2
# capability is one entry here, no new control flow.
TASK_KINDS: dict[str, dict] = {
    "play_media": {
        "template": "fulfill",
        "primary_tool": "video_player",
        "required_slots": ["query"],
        "initial_state": "find",
        # video_player finds AND verifies the YouTube video itself from a query —
        # NO web_search first. This is the deterministic call that replaces the
        # model's tool-choice (the empty-web_search loop the runtime exists to kill).
        "build_args": lambda slots: {"action": "open", "query": (slots.get("query") or "").strip()},
        # Spoken question when a required slot is missing and can't be guessed.
        "ask_hint": "what they want to hear",
        # One retry only: the call is deterministic for a given query, so a re-try
        # buys nothing but covering a transient blip. After that, ask (don't spin).
        "retry_budget": 1,
    },
}


def is_task_kind(kind: str | None) -> bool:
    return bool(kind) and kind in TASK_KINDS


def make_task(kind: str, slots: dict | None = None) -> ActiveTask:
    """Instantiate a fresh task for ``kind`` in its template's entry state."""
    spec = TASK_KINDS[kind]
    return ActiveTask(
        kind=kind,
        template=spec["template"],
        state=spec["initial_state"],
        slots=dict(slots or {}),
    )


# ── the decision (pure) ───────────────────────────────────────────────────────

@dataclass
class Decision:
    """What ``task_step`` should do next. The runtime turns this into IO."""

    action: str                     # "act" | "ask" | "succeed" | "give_up"
    tool: str | None = None         # for "act"
    tool_args: dict | None = None   # for "act"
    ask_for: str | None = None      # for "ask": short hint of what's missing
    reason: str = ""                # for logs


def _missing_required(spec: dict, slots: dict) -> list[str]:
    out = []
    for s in spec.get("required_slots", []):
        v = slots.get(s)
        if not (isinstance(v, str) and v.strip()):
            out.append(s)
    return out


def decide_entry(task: ActiveTask) -> Decision:
    """Decide the next move when (re-)entering a task — a fresh start, or a resume
    after the user answered an ``ask``. Pure; ``task_step`` executes the result."""
    spec = TASK_KINDS[task.kind]
    if task.template == "fulfill":
        missing = _missing_required(spec, task.slots)
        if missing:
            return Decision("ask", ask_for=spec.get("ask_hint") or missing[0],
                            reason=f"missing {missing}")
        return Decision("act", tool=spec["primary_tool"],
                        tool_args=spec["build_args"](task.slots),
                        reason=f"fulfill {task.kind}")
    # Other templates land here in later stages.
    return Decision("give_up", reason=f"no template logic for {task.template}")


def decide_after_result(task: ActiveTask, ok: bool) -> Decision:
    """Decide what to do once the primary tool has returned. ``ok`` is the runtime's
    read of whether the tool succeeded. On failure: retry within budget, else ask
    (degrade to a question, never silent death)."""
    spec = TASK_KINDS[task.kind]
    if task.template == "fulfill":
        if ok:
            return Decision("succeed", reason="tool ok")
        budget = int(spec.get("retry_budget", FULFILL_RETRY_BUDGET))
        if task.retry_count + 1 <= budget:
            return Decision("act", tool=spec["primary_tool"],
                            tool_args=spec["build_args"](task.slots),
                            reason=f"retry {task.retry_count + 1}/{budget}")
        return Decision("ask", ask_for=spec.get("ask_hint") or "which one",
                        reason="retries spent")
    return Decision("give_up", reason=f"no template logic for {task.template}")


def apply_continuation(task: ActiveTask, slots: dict | None) -> ActiveTask:
    """Fold a continuation (the user's answer to an ``ask``) back into the task and
    return it to its acting state. For fulfill: merge slots, return to ``find``."""
    if slots:
        task.slots.update({k: v for k, v in slots.items() if v not in (None, "")})
    if task.template == "fulfill":
        task.state = "find"
    return task
