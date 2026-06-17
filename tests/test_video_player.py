"""video_player — YouTube id extraction from Brave results + query fallback."""

import asyncio
import importlib

import pytest


@pytest.fixture()
def vp():
    import tools.custom.video_player as m
    importlib.reload(m)
    return m


def test_extracts_watch_short_and_youtu_be(vp):
    data = {"web": {"results": [
        {"url": "https://example.com/article", "title": "Top ambient", "description": "no video"},
        {"url": "https://www.youtube.com/watch?v=ABCDEFGHIJK", "title": "Ambient mix"},
        {"url": "https://youtu.be/zzzzzzzzzzz", "title": "Lofi"},
        {"url": "https://www.youtube.com/shorts/SHORT123456", "title": "short"},
    ]}}
    assert vp._yt_ids_from_brave(data) == ["ABCDEFGHIJK", "zzzzzzzzzzz", "SHORT123456"]


def test_dedupes_and_scans_description(vp):
    data = {"web": {"results": [
        {"url": "https://x.com", "title": "t", "description": "watch https://youtu.be/DUP00000001 now"},
        {"url": "https://www.youtube.com/watch?v=DUP00000001", "title": "dup"},
    ]}}
    assert vp._yt_ids_from_brave(data) == ["DUP00000001"]


def test_no_youtube_results(vp):
    data = {"web": {"results": [{"url": "https://example.com", "title": "x", "description": "y"}]}}
    assert vp._yt_ids_from_brave(data) == []


def test_open_without_id_or_query_errors(vp):
    out = asyncio.run(vp.execute({"action": "open"}))
    assert "query" in out.lower() and "video_id" in out.lower()


def test_resolve_youtube_id_no_brave_key(vp, monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    r = asyncio.run(vp._resolve_youtube_id("ambient music"))
    assert r["ok"] is False and "BRAVE_API_KEY" in r["reason"]


def test_candidates_carry_titles(vp):
    data = {"web": {"results": [
        {"url": "https://www.youtube.com/watch?v=ABCDEFGHIJK", "title": "Ambient mix"},
        {"url": "https://youtu.be/zzzzzzzzzzz", "title": "Lofi radio LIVE"},
    ]}}
    assert vp._yt_candidates_from_brave(data) == [
        ("ABCDEFGHIJK", "Ambient mix"),
        ("zzzzzzzzzzz", "Lofi radio LIVE"),
    ]


def test_ranking_pushes_live_last_and_vod_first(vp):
    # Brave order: live first, plain second, VOD-ish ("1 hour mix") third.
    cands = [
        ("liveAAAAAAA", "lofi hip hop radio 📚 - beats to relax/study to"),
        ("plainBBBBBB", "lofi hip hop"),
        ("vodCCCCCCCC", "lofi hip hop 1 hour mix"),
    ]
    # Stable reorder → VOD first, plain middle, live last.
    assert vp._rank_candidates_avoiding_live(cands) == [
        "vodCCCCCCCC", "plainBBBBBB", "liveAAAAAAA",
    ]


def test_music_loop_intent_detection(vp):
    assert vp._MUSIC_LOOP_RE.search("play some lofi hip hop radio")
    assert vp._MUSIC_LOOP_RE.search("ambient study beats")
    # A specific song/clip is not a continuous-music request → no query rewrite.
    assert not vp._MUSIC_LOOP_RE.search("Rick Astley Never Gonna Give You Up")
