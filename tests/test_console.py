"""Console tests.

The console reads a developer's private working history — transcripts, file
paths, shell commands — so the binding and the degrade path matter as much as
the payload.
"""

from __future__ import annotations

import json

from harness import console, db


def test_it_binds_loopback_only():
    """There is deliberately no flag to change this."""
    assert console.HOST == "127.0.0.1"
    assert "0.0.0.0" not in (console.__doc__ or "")


def test_payload_has_all_three_views(fake_home):
    db.connect()
    p = console.payload()
    assert {"nodes", "edges", "memory", "fleet", "totals"} <= set(p)
    assert {"learned", "projects", "instructions"} <= set(p["memory"])


def test_payload_is_json_serialisable(fake_home):
    db.connect()
    json.dumps(console.payload())


def test_it_degrades_without_swarm(fake_home, monkeypatch):
    """harness must work when swarm is absent — the agent view goes away, the
    other two do not."""
    monkeypatch.setattr(console, "_reader", lambda: None)
    p = console.payload()
    assert p["fleet"]["projects"] == []
    assert "swarm is not installed" in p["fleet"]["unavailable"]
    assert p["nodes"] is not None


def test_edges_with_a_missing_endpoint_are_dropped(fake_home):
    """A prune can outrun an edge rebuild, and a dangling edge would silently
    vanish a node from the layout."""
    conn = db.connect()
    conn.execute("INSERT INTO nodes (id,kind,name,state) VALUES ('skill:a','skill','a','live')")
    conn.execute("INSERT INTO edges (src,dst,type) VALUES ('skill:a','skill:gone','routes-to')")
    conn.execute("INSERT INTO edges (src,dst,type) VALUES ('skill:a','skill:a','routes-to')")
    conn.commit()
    graph = console._graph(conn)
    assert [e["d"] for e in graph["edges"]] == ["skill:a"]


def test_the_page_is_packaged_and_self_contained():
    """A strict local page: no CDN, no external fetch."""
    html = (console.Path(__file__).parent.parent / "src" / "harness" / "web" / "console.html").read_text()
    assert "<canvas" in html and "/api/data" in html
    for bad in ("http://cdn", "https://cdn", "unpkg.com", "googleapis.com"):
        assert bad not in html


def test_is_up_is_false_on_a_free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert console.is_up(free) is False
