"""Curiosity topic rotation — companion-first mix (2 self : 1 desk).

Flipped 2026-08-04 from 2 desk : 1 self: the old ratio made the idle share
feed read like a finance wire.
"""

from autonomy import curiosity


def test_rotation_prefers_self_topics(monkeypatch):
    monkeypatch.setattr(curiosity, "_cfg", lambda: {
        "topics_self": ["space a", "brain b", "ocean c", "physics d"],
        "topics_desk": ["market x", "fed y"],
    })
    monkeypatch.setattr(curiosity, "_held_ticker_topics", lambda: [])
    rot = curiosity._topic_rotation()
    kinds = [t["kind"] for t in rot]
    # Leads with her taste, and self outnumbers desk overall.
    assert kinds[0] == "self"
    assert kinds.count("self") > kinds.count("desk")
    # First three follow the 2 self : 1 desk pattern.
    assert kinds[:3] == ["self", "self", "desk"]
    # Nothing dropped.
    assert len(rot) == 6


def test_rotation_survives_empty_desk_topics(monkeypatch):
    monkeypatch.setattr(curiosity, "_cfg", lambda: {
        "topics_self": ["space a", "brain b"],
        "topics_desk": [],
    })
    monkeypatch.setattr(curiosity, "_held_ticker_topics", lambda: [])
    rot = curiosity._topic_rotation()
    assert [t["kind"] for t in rot] == ["self", "self"]
