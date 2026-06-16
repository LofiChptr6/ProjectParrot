"""News ledger — relative-age parsing + message_id↔article round-trip."""

import asyncio
import importlib
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    import autonomy.news_ledger as nl
    importlib.reload(nl)
    # Point the on-disk ledger at a temp file.
    monkeypatch.setattr(nl, "_PATH", tmp_path / "news_ledger.json")
    return nl


def test_parse_published_at_relative(ledger):
    ref = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    got = ledger.parse_published_at("3 hours ago", now=ref)
    assert got == datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc).isoformat()
    assert ledger.parse_published_at("2 days ago", now=ref) == \
        datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc).isoformat()
    assert ledger.parse_published_at("yesterday", now=ref) == \
        datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc).isoformat()


def test_parse_published_at_garbage(ledger):
    assert ledger.parse_published_at("") is None
    assert ledger.parse_published_at("sometime") is None


def test_record_and_get_roundtrip(ledger, monkeypatch):
    # Don't hit mem0 in the unit test — capture the add() call instead.
    captured = {}

    async def _fake_add(text, **kw):
        captured["text"] = text
        captured["meta"] = kw.get("metadata")
        captured["infer"] = kw.get("infer")

    import memory.mem0_store as ms
    monkeypatch.setattr(ms, "add", _fake_add)

    item = {
        "title": "Micron memory chip bottleneck drives earnings",
        "url": "https://example.com/mu",
        "source": "example.com",
        "snippet": "MU up on tight supply.",
        "topic": "MU stock news",
        "kind": "desk",
        "date": "3 hours ago",
        "fetched_at": "2026-06-15T09:00:00+00:00",
    }
    asyncio.run(ledger.record_shared(4242, item, "no cyclical downturn in sight for MU"))

    rec = ledger.get(4242)
    assert rec is not None
    assert rec["headline"].startswith("Micron")
    assert rec["url"] == "https://example.com/mu"
    assert rec["take"].startswith("no cyclical")
    assert rec["published_at"]  # resolved from "3 hours ago"
    # mem0 write happened verbatim (infer=False) with shared_news kind + citation.
    assert captured["infer"] is False
    assert captured["meta"]["kind"] == "shared_news"
    assert captured["meta"]["telegram_message_id"] == 4242
    assert captured["meta"]["url"] == "https://example.com/mu"


def test_get_missing_is_none(ledger):
    assert ledger.get(999999) is None
    assert ledger.get(None) is None


def test_web_share_without_message_id_still_writes_mem0(ledger, monkeypatch):
    calls = []

    async def _fake_add(text, **kw):
        calls.append(kw.get("metadata"))

    import memory.mem0_store as ms
    monkeypatch.setattr(ms, "add", _fake_add)

    item = {"title": "A space thing", "url": "https://x.com/a", "source": "x.com",
            "kind": "self", "date": "just now"}
    asyncio.run(ledger.record_shared(None, item, "huh, neat"))
    # No keyed row (no message_id), but a mem0 record for semantic recall.
    assert ledger.recent() == [] or all(r.get("message_id") is None for r in ledger.recent())
    assert len(calls) == 1
    assert "telegram_message_id" not in calls[0]
