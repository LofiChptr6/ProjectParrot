"""P&L → mood mapping (the pure bucketing + message formatting)."""

import importlib

import pytest


@pytest.fixture()
def pnl_mood():
    import bridge.pnl_mood as m
    importlib.reload(m)
    return m


def test_bucket_by_pct(pnl_mood):
    assert pnl_mood._bucket({"pct": 0.02, "combined": 5000})["label"] == "buoyant"
    assert pnl_mood._bucket({"pct": 0.006, "combined": 800})["label"] == "pleased"
    assert pnl_mood._bucket({"pct": 0.0, "combined": 10})["label"] == "steady"
    assert pnl_mood._bucket({"pct": -0.006, "combined": -800})["label"] == "uneasy"
    assert pnl_mood._bucket({"pct": -0.05, "combined": -9000})["label"] == "worried"


def test_bucket_absolute_fallback_when_no_nav(pnl_mood):
    # No pct (NAV unknown) → coarse absolute bands, sign still registers.
    assert pnl_mood._bucket({"pct": None, "combined": 5000})["label"] == "buoyant"
    assert pnl_mood._bucket({"pct": None, "combined": -50})["label"] == "steady"
    assert pnl_mood._bucket({"pct": None, "combined": -5000})["label"] == "worried"


def test_bucket_none_when_no_signal(pnl_mood):
    assert pnl_mood._bucket({"pct": None, "combined": None}) is None


def test_mood_message_is_soft_and_numberless(pnl_mood):
    msg = pnl_mood._format_mood_message({"label": "worried", "gist": "the desk's down hard today"})
    assert "worried" in msg
    assert "mood, not content" in msg
    # It must NOT contain a literal P&L figure — it's a tone prior, not a readout.
    assert "$" not in msg
    # The desk is his work, not her identity — the co-runner framing is gone.
    assert "run the trading desk together" not in msg


def test_steady_mood_injects_nothing(pnl_mood):
    """A flat day is not a feeling: no inner-state line at all (the old
    every-turn 'roughly flat' line primed desk-talk on unrelated turns)."""
    b = pnl_mood._bucket({"pct": 0.0, "combined": 10})
    assert b["label"] == "steady"
    assert pnl_mood._format_mood_message(b) is None


def test_current_mood_uses_cache(pnl_mood, monkeypatch):
    monkeypatch.setattr(pnl_mood, "_maybe_kick_refresh", lambda: None)
    pnl_mood._cache["data"] = {"pct": 0.02, "combined": 5000, "nav": 250000}
    assert pnl_mood.current_mood() == "buoyant"
    assert "buoyant" in pnl_mood.mood_system_message()
