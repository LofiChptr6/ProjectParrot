"""Task runtime — the cross-turn store of what Mocha is currently *doing*.

This is the persistence layer the conversational graph (``bridge/graph.py``) lacks:
``TurnState`` is per-turn and the graph runs with no checkpointer, so without this
an intent evaporates the moment a turn ends. Here we keep a tiny *continuation
pointer* — "parked at `ask` in play_media, slots={query: chopin}" — so the next
turn resumes the task instead of cold-reading it out of history.

Design (see docs/task_runtime_design.md):
- Per-``user_id`` (single operator today, but keep the partition like the scratchpad).
- In-RAM, process-scoped. Tasks live seconds-to-minutes; a bridge restart dropping
  a parked task is acceptable and *safer* than resuming a stale one.
- **v1: one active task + one suspend slot.** Interrupt pushes the active task into
  the slot; the new task runs; on its exit the slot is promoted back. A full agenda
  stack is a later upgrade — make ``_Slots.suspended`` a list when that day comes.
- This module holds NO LLM / IO. It is pure state, fully unit-testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace


@dataclass
class ActiveTask:
    """One thing Mocha is doing. A resume point, nothing more."""

    kind: str                      # "play_media" | "look_up" | … (registry key)
    template: str                  # "fulfill" | "slot_fill" | "confirm"
    state: str                     # current node within the template, e.g. "find" | "ask"
    slots: dict = field(default_factory=dict)   # accumulated args
    retry_count: int = 0           # fulfill-loop budget counter
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    cancellable: bool = True

    def touched(self) -> "ActiveTask":
        self.last_active_at = time.time()
        return self


@dataclass
class _Slots:
    active: ActiveTask | None = None
    suspended: ActiveTask | None = None   # v1: single slot (→ list for a full stack)


# Per-user registry, mirroring session_scratchpad's pattern.
_slots: dict[str, _Slots] = {}


def _get(user_id: str | None) -> _Slots:
    key = user_id or "_default"
    if key not in _slots:
        _slots[key] = _Slots()
    return _slots[key]


# ── reads ────────────────────────────────────────────────────────────────────

def get_active(user_id: str | None) -> ActiveTask | None:
    return _get(user_id).active


def has_active(user_id: str | None) -> bool:
    return _get(user_id).active is not None


def get_suspended(user_id: str | None) -> ActiveTask | None:
    return _get(user_id).suspended


# ── writes ───────────────────────────────────────────────────────────────────

def start(user_id: str | None, task: ActiveTask) -> ActiveTask:
    """Begin a task as the active one. Overwrites any current active task without
    suspending it — callers that want interrupt-and-resume must call ``suspend``
    first (interrupt) rather than ``start`` (fresh)."""
    _get(user_id).active = task.touched()
    return task


def update_active(user_id: str | None, **changes) -> ActiveTask | None:
    """Mutate fields of the active task (state, slots, retry_count, …) and stamp
    last_active_at. Returns the updated task, or None if there is no active task."""
    s = _get(user_id)
    if s.active is None:
        return None
    if changes:
        s.active = replace(s.active, **changes)
    s.active.last_active_at = time.time()
    return s.active


def suspend(user_id: str | None) -> ActiveTask | None:
    """Interrupt: push the active task into the suspend slot (dropping whatever was
    parked there — v1 is single-depth) and clear active. Returns the suspended task."""
    s = _get(user_id)
    s.suspended = s.active.touched() if s.active else s.suspended
    parked = s.active
    s.active = None
    return parked


def complete_active(user_id: str | None) -> ActiveTask | None:
    """Finish the active task (succeed / give-up) and promote any suspended task
    back to active. Returns the *resumed* task (so the caller can pick it back up),
    or None when there was nothing parked."""
    s = _get(user_id)
    s.active = s.suspended.touched() if s.suspended else None
    s.suspended = None
    return s.active


def clear_all(user_id: str | None) -> None:
    """Drop everything for a user (active + suspended). Used on hard reset / give-up
    of the whole stack."""
    _slots[user_id or "_default"] = _Slots()


def sweep_timeouts(ttl_seconds: float, now: float | None = None) -> int:
    """Drop parked tasks idle longer than ``ttl_seconds`` so a stale goal can't
    haunt her. A task counts as parked when it is waiting on the user (any state
    that ends a turn — e.g. fulfill's "ask"). Returns the number dropped.

    ``now`` is injectable for tests; defaults to wall clock."""
    t = time.time() if now is None else now
    dropped = 0
    for s in _slots.values():
        for attr in ("active", "suspended"):
            task = getattr(s, attr)
            if task is not None and (t - task.last_active_at) > ttl_seconds:
                setattr(s, attr, None)
                dropped += 1
    return dropped


def _reset_for_tests() -> None:
    _slots.clear()
