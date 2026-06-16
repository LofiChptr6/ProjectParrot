"""Presence state machine — IDLE ⇄ CONVERSING gating for the idle autonomy loop."""

import importlib
import time

import pytest


@pytest.fixture()
def presence():
    # Fresh module state per test (module-global state).
    import autonomy.presence as p
    importlib.reload(p)
    return p


def test_starts_idle(presence):
    # No activity yet → elapsed is huge → IDLE.
    assert presence.is_idle(600) is True
    assert presence.state(600) == presence.IDLE


def test_user_activity_enters_conversing(presence):
    presence.note_user_activity()
    assert presence.is_conversing(600) is True
    assert presence.is_idle(600) is False


def test_returns_to_idle_after_threshold(presence, monkeypatch):
    presence.note_user_activity()
    assert presence.is_idle(600) is False
    # Simulate 11 minutes passing by rewinding the recorded activity time.
    presence._last_activity_monotonic -= 660
    assert presence.is_idle(600) is True


def test_turn_in_flight_forces_conversing(presence):
    presence.turn_started()
    # Even if the clock says it's been ages, a turn in flight pins CONVERSING.
    presence._last_activity_monotonic -= 10_000
    assert presence.is_idle(600) is False
    presence.turn_ended()
    # Turn ended now resets the silence clock → CONVERSING until threshold.
    assert presence.is_idle(600) is False
    presence._last_activity_monotonic -= 660
    assert presence.is_idle(600) is True


def test_nested_turns_balance(presence):
    presence.turn_started()
    presence.turn_started()
    presence.turn_ended()
    presence._last_activity_monotonic -= 10_000
    # One turn still in flight → CONVERSING.
    assert presence.is_idle(600) is False
    presence.turn_ended()
    presence._last_activity_monotonic -= 660
    assert presence.is_idle(600) is True
