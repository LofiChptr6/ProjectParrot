"""Presence state machine — is Ika actively conversing, or idle?

Two states, gating the proactive autonomy loop:

    IDLE  ──(Ika sends a message / a turn starts)──▶  CONVERSING
    CONVERSING  ──(idle_after_s of silence, no turn in flight)──▶  IDLE

The idle autonomy loop (``autonomy/engine.decide_tick``) only does proactive work
in IDLE. The instant Ika speaks — or a conversational turn is in flight — we flip
to CONVERSING and Mocha holds her tongue (no unprompted news, no check-ins). After
``idle_after_s`` of true silence she drifts back to IDLE and may surface news again.

This replaces the old, never-armed ``last_tool_at_monotonic`` guard with an
explicit, inspectable mechanism. State lives in module globals — the bridge is a
single process, so that's sufficient and lock-free.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("autonomy.presence")

CONVERSING = "conversing"
IDLE = "idle"

# Last inbound user activity (a message, or a turn boundary). 0.0 at boot means
# "no one has spoken yet" → elapsed is huge → we start IDLE, which is correct.
_last_activity_monotonic: float = 0.0
# Wall-clock twin of the stamp above (0.0 = nothing since boot). The monotonic
# clock is right for elapsed-time gating but useless for anything persisted:
# autonomy's unanswered-shares gate compares this against share timestamps that
# live on disk and survive restarts, so it needs epoch seconds.
_last_activity_epoch: float = 0.0
# Conversational turns currently being served. While >0 we are unconditionally
# CONVERSING regardless of the clock (a long tool round must not get talked over).
_turns_in_flight: int = 0
_state: str = IDLE


def note_user_activity() -> None:
    """Ika just sent something. Enter CONVERSING and reset the silence clock."""
    global _last_activity_monotonic, _last_activity_epoch, _state
    _last_activity_monotonic = time.monotonic()
    _last_activity_epoch = time.time()
    if _state != CONVERSING:
        log.info("presence: %s → %s (Ika spoke)", _state, CONVERSING)
        _state = CONVERSING


def turn_started() -> None:
    """A conversational turn began — hold CONVERSING until it ends."""
    global _turns_in_flight, _last_activity_monotonic, _last_activity_epoch, _state
    _turns_in_flight += 1
    _last_activity_monotonic = time.monotonic()
    _last_activity_epoch = time.time()
    _state = CONVERSING


def turn_ended() -> None:
    """A conversational turn finished. The silence clock starts from here."""
    global _turns_in_flight, _last_activity_monotonic, _last_activity_epoch
    _turns_in_flight = max(0, _turns_in_flight - 1)
    _last_activity_monotonic = time.monotonic()
    _last_activity_epoch = time.time()


def state(idle_after_s: float) -> str:
    """Resolve (and memoize) the current presence state, logging transitions.

    A turn in flight forces CONVERSING. Otherwise we are CONVERSING until
    ``idle_after_s`` of silence has elapsed, then IDLE.
    """
    global _state
    if _turns_in_flight > 0:
        new = CONVERSING
    else:
        elapsed = time.monotonic() - _last_activity_monotonic
        new = CONVERSING if elapsed < idle_after_s else IDLE
    if new != _state:
        log.info("presence: %s → %s (idle_after_s=%.0f, idle=%.0fs)",
                 _state, new, idle_after_s, seconds_since_activity())
        _state = new
    return _state


def is_idle(idle_after_s: float) -> bool:
    return state(idle_after_s) == IDLE


def is_conversing(idle_after_s: float) -> bool:
    return state(idle_after_s) == CONVERSING


def seconds_since_activity() -> float:
    return time.monotonic() - _last_activity_monotonic


def last_activity_epoch() -> float:
    """Wall-clock time of the last user activity (0.0 = none since boot).

    This is the source of truth for "when did Ika last say anything" in a form
    that persisted consumers can use — autonomy's engagement gate compares it
    against on-disk share timestamps, which a monotonic stamp can't do."""
    return _last_activity_epoch


def snapshot() -> dict:
    """Introspection helper for admin/debug endpoints. Never raises."""
    return {
        "state": _state,
        "turns_in_flight": _turns_in_flight,
        "seconds_since_activity": round(seconds_since_activity(), 1),
        "last_activity_epoch": _last_activity_epoch,
    }
