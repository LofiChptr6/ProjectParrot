"""A welcome-back greeting fires the instant the web tab connects — before the
user has interacted this session — so the 2h _web_attended() idle window is
always lapsed at that moment. Without an override the greeting falls through to
Telegram and the avatar stays silent (the "Mocha doesn't greet me on the website"
bug). connect_triggered=True must deliver such greetings to the just-connected
tab, while leaving ambient idle news routing untouched.
"""

import asyncio
import importlib

import pytest


@pytest.fixture()
def cr():
    import bridge.channel_router as m
    importlib.reload(m)
    return m


def _patch_channels(cr, monkeypatch, *, web_attended, web_present):
    sent: dict[str, str] = {}

    async def fake_web(payload):
        sent["web"] = payload["text"]

    async def fake_tg(text, chat_id, app_user_id=None):
        sent["telegram"] = text
        return 4242

    monkeypatch.setattr(cr, "_web_attended", lambda: web_attended)
    monkeypatch.setattr(cr, "_web_clients_present", lambda: web_present)
    monkeypatch.setattr(cr, "_deliver_to_web", fake_web)
    monkeypatch.setattr(cr, "_deliver_to_telegram", fake_tg)
    monkeypatch.setattr(cr, "load_primary_user", lambda: {"telegram_user_id": "999"})
    return sent


def test_connect_triggered_greeting_lands_on_web_when_idle_window_lapsed(cr, monkeypatch):
    # Tab connected, but no recent interaction → _web_attended() is False.
    sent = _patch_channels(cr, monkeypatch, web_attended=False, web_present=True)

    where = asyncio.run(cr.route_autonomous({
        "text": "Hey, welcome back!",
        "kind": "autonomy_reconnect",
        "source": "autonomy:reconnect",
        "connect_triggered": True,
    }))

    assert "web" in where
    assert sent.get("web") == "Hey, welcome back!"
    # Greeting reached the avatar, so it must NOT also be pushed to the phone.
    assert "telegram" not in where
    assert "telegram" not in sent


def test_ambient_news_still_falls_through_to_telegram_when_idle(cr, monkeypatch):
    # Same idle conditions, but NOT connect-triggered → preserve the 6/16 behavior
    # (don't speak to an unwatched tab; route ambient news to the phone).
    sent = _patch_channels(cr, monkeypatch, web_attended=False, web_present=True)

    where = asyncio.run(cr.route_autonomous({
        "text": "Random ambient news.",
        "kind": "autonomy_lonely",
        "source": "autonomy:lonely",
    }))

    assert where == "telegram"
    assert "web" not in sent
    assert sent.get("telegram") == "Random ambient news."


def test_connect_triggered_with_no_tab_present_still_uses_telegram(cr, monkeypatch):
    # Edge: greeting flagged connect_triggered but no socket actually present
    # (e.g. it dropped between connect and compose) → fall back to Telegram.
    sent = _patch_channels(cr, monkeypatch, web_attended=False, web_present=False)

    where = asyncio.run(cr.route_autonomous({
        "text": "Welcome back!",
        "kind": "autonomy_reconnect",
        "source": "autonomy:reconnect",
        "connect_triggered": True,
    }))

    assert where == "telegram"
    assert "web" not in sent
    assert sent.get("telegram") == "Welcome back!"
