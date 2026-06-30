"""Greetings are gated by a single, restart-proof debounce — no daily cap.

The old 4/day cap existed only to backstop the debounce across bridge restarts
(its clock used to live in memory). Persisting the debounce as wall-clock makes
the cap redundant, so it's gone: an active day of real logins always gets a
hello, while rapid refreshes inside the 30-min window stay quiet — even if the
bridge restarts between them.
"""

import importlib
import time

import pytest


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    import autonomy.engine as e
    importlib.reload(e)
    monkeypatch.setattr(e, "_STATE_PATH", tmp_path / "autonomy_state.json")
    e._persist = None  # start from a clean on-disk state
    return e


CFG = {"reconnect_debounce_s": 1800}


def test_no_daily_cap_knob_remains(eng):
    # The cap is fully retired — no config knob, no gate.
    assert "reconnect_max_per_day" not in eng._DEFAULTS


def test_fresh_state_allows_a_greeting(eng):
    assert eng._greeting_debounced(CFG) is False


def test_greeting_then_immediate_retry_is_debounced(eng):
    eng._stamp_greeting()
    assert eng._greeting_debounced(CFG) is True


def test_debounce_survives_a_bridge_restart(eng):
    eng._stamp_greeting()
    assert eng._greeting_debounced(CFG) is True
    # Simulate a restart: drop in-memory state so the next read reloads from disk.
    eng._persist = None
    assert eng._greeting_debounced(CFG) is True  # persisted → still debounced


def test_greeting_allowed_again_once_window_elapses(eng):
    eng._stamp_greeting()
    # Backdate the stamp just past the window.
    eng._get_persist()["last_hello_epoch"] = time.time() - (CFG["reconnect_debounce_s"] + 1)
    assert eng._greeting_debounced(CFG) is False


def test_many_real_returns_in_a_day_are_never_capped(eng):
    # Ten separate returns, each well outside the debounce window → all greet.
    for n in range(10):
        eng._get_persist()["last_hello_epoch"] = time.time() - (CFG["reconnect_debounce_s"] + 60)
        assert eng._greeting_debounced(CFG) is False  # allowed every time
        eng._stamp_greeting()                          # greet, then move on
