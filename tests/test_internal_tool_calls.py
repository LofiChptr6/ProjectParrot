"""Internal tool calls must stay invisible to the user.

Regression: the held-ticker cache refresh (a silent background warm) called
execute_tool("get_positions"), the desk was unreachable, the proxy returned a
__panel__ error envelope, and the executor broadcast it — painting a
full-screen "Positions — Unavailable" modal on Ika's screen every TTL and
telling Mocha's prompt that panel was on screen.
"""

import asyncio
import json

import pytest

from tools import executor


PANEL_RESULT = json.dumps({
    "__panel__": "create_presentation",
    "__payload__": {"id": "p1", "title": "Positions — Unavailable"},
})


@pytest.fixture
def fake_panel_tool(monkeypatch):
    async def _tool(_args):
        return PANEL_RESULT
    monkeypatch.setitem(executor._CUSTOM_EXECUTORS, "fake_panel_tool", _tool)


@pytest.fixture
def spy_broadcast(monkeypatch):
    """Capture the executor's UI side effects without importing the server."""
    calls = {"broadcast": [], "modal": []}

    async def _broadcast(msg):
        calls["broadcast"].append(msg)

    def _set_open_modal(kind, info):
        calls["modal"].append((kind, info))

    import bridge.server as S
    monkeypatch.setattr(S, "_broadcast_clients", _broadcast, raising=False)
    monkeypatch.setattr(S, "_set_open_modal", _set_open_modal, raising=False)
    return calls


def test_internal_call_broadcasts_no_panel(fake_panel_tool, spy_broadcast):
    out = asyncio.run(executor.execute_tool("fake_panel_tool", {}, internal=True))
    assert spy_broadcast["broadcast"] == []
    assert spy_broadcast["modal"] == []
    # The caller still gets the payload — only the UI side effects are skipped.
    assert "__panel__" in out


def test_user_facing_call_still_broadcasts(fake_panel_tool, spy_broadcast):
    asyncio.run(executor.execute_tool("fake_panel_tool", {}))
    assert len(spy_broadcast["broadcast"]) == 1
    assert spy_broadcast["broadcast"][0]["action"] == "create_presentation"
    assert spy_broadcast["modal"] == [
        ("presentation", {"id": "p1", "title": "Positions — Unavailable"})
    ]


def test_internal_call_skips_scratchpad(fake_panel_tool, spy_broadcast, monkeypatch):
    added = []
    import bridge.session_scratchpad as sp
    monkeypatch.setattr(sp, "add", lambda *a, **k: added.append(a), raising=False)

    asyncio.run(executor.execute_tool("fake_panel_tool", {}, internal=True))
    assert added == []
    asyncio.run(executor.execute_tool("fake_panel_tool", {}))
    assert len(added) == 1


def test_held_ticker_refresh_is_internal():
    """The background cache warm must pass internal=True (pins the fix site)."""
    import inspect
    import bridge.server as S
    src = inspect.getsource(S._refresh_held_tickers)
    assert "internal=True" in src
