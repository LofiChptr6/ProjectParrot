"""History persistence round-trip (bridge/history_store.py)."""

import json

from bridge import history_store


def test_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path)
    entries = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "hey Ika"}]
    history_store._persist_sync("ika", entries, "They greeted each other.")
    loaded, summary = history_store.load_sync("ika")
    assert loaded == entries
    assert summary == "They greeted each other."


def test_load_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path)
    assert history_store.load_sync("nobody") == ([], "")


def test_load_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path)
    p = history_store._path_for("ika")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert history_store.load_sync("ika") == ([], "")


def test_malformed_entries_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path)
    p = history_store._path_for("ika")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "summary": "s",
        "entries": [{"role": "user", "content": "ok"}, {"bad": 1}, "junk",
                    {"role": "assistant", "content": ""}],
    }), encoding="utf-8")
    loaded, _ = history_store.load_sync("ika")
    assert loaded == [{"role": "user", "content": "ok"}]


def test_uid_sanitized_for_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(history_store, "HISTORY_DIR", tmp_path)
    p = history_store._path_for("we/ird user!")
    assert p.parent == tmp_path
    assert "/" not in p.name.replace(".json", "")
