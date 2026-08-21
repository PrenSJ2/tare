"""Console tests.

The console reads a developer's private working history — transcripts, file
paths, shell commands — so the binding and the degrade path matter as much as
the payload.
"""

from __future__ import annotations

import json

from tare import console, db


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
    html = (console.Path(__file__).parent.parent / "src" / "tare" / "web" / "console.html").read_text()
    assert "<canvas" in html and "/api/data" in html
    for bad in ("http://cdn", "https://cdn", "unpkg.com", "googleapis.com"):
        assert bad not in html


def test_is_up_is_false_on_a_free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert console.is_up(free) is False


def test_orchestration_skips_sessions_with_no_agents(fake_home, monkeypatch):
    """Slicing the newest N sessions catches idle ones and returns an empty
    tree while a rich session sits just outside the window."""
    class FakeOrch:
        def __init__(self, n):
            self.parent_of = {f"a{i}": "" for i in range(n)}
            self.depth = {f"a{i}": 0 for i in range(n)}
            self.files = [("/x", 3)] if n else []
            self.tools = [("Bash", 5)] if n else []

    class FakeReader:
        @staticmethod
        def all_sessions():
            return [("p", "empty1"), ("p", "empty2"), ("p", "rich")]
        @staticmethod
        def orchestration(session, redact=False):
            return FakeOrch(4 if session == "rich" else 0)

    monkeypatch.setattr(console, "_reader", lambda: FakeReader)
    out = console._orchestration(False)
    assert len(out["edges"]) == 4, "the rich session must be reached past the empty ones"


def test_orchestration_is_empty_without_swarm(fake_home, monkeypatch):
    monkeypatch.setattr(console, "_reader", lambda: None)
    assert console._orchestration(False)["edges"] == []


def test_cors_is_granted_only_to_loopback(fake_home):
    """The socket is loopback-bound already, so this widens nothing -- but it
    must never echo an arbitrary Origin back."""
    import inspect
    src = inspect.getsource(console._Handler._send)
    assert 'startswith("http://127.0.0.1:")' in src
    assert '"*"' not in src


def test_the_payload_is_cached_between_polls(fake_home, monkeypatch):
    """Two panels poll independently. Uncached, assembling this walked
    thousands of transcripts per request and took over a minute, which showed
    in the UI as panels stuck on "reading" forever."""
    calls = []
    real = console._build_payload
    monkeypatch.setattr(console, "_build_payload",
                        lambda **kw: (calls.append(1), real(**kw))[1])
    console._PAYLOAD_CACHE = None
    console.payload()
    console.payload()
    console.payload()
    assert len(calls) == 1, "repeated polls must not rebuild the payload"


def test_fresh_bypasses_the_cache(fake_home, monkeypatch):
    calls = []
    real = console._build_payload
    monkeypatch.setattr(console, "_build_payload",
                        lambda **kw: (calls.append(1), real(**kw))[1])
    console._PAYLOAD_CACHE = None
    console.payload()
    console.payload(fresh=True)
    assert len(calls) == 2


def test_redact_is_not_served_from_a_non_redacted_cache(fake_home, monkeypatch):
    """Serving cached unredacted data to a redacted request would leak exactly
    what redaction exists to withhold."""
    console._PAYLOAD_CACHE = None
    console.payload(redact=False)
    calls = []
    real = console._build_payload
    monkeypatch.setattr(console, "_build_payload",
                        lambda **kw: (calls.append(kw), real(**kw))[1])
    console.payload(redact=True)
    assert calls and calls[0]["redact"] is True
