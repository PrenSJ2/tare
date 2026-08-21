"""Viewer supervision tests.

`ensure()` runs from a SessionStart hook on every session of every project, so
the tests that matter are the ones proving it stays out of the way: never
spawns a second server, never blocks, never speaks, never raises.
"""

from __future__ import annotations

import socket

from harness import viewer


def test_ensure_does_not_spawn_when_something_is_already_listening(monkeypatch):
    """Two servers racing for one port fail exactly when someone is trying to
    see what is going on."""
    spawned = []
    monkeypatch.setattr(viewer, "is_up", lambda port=viewer.DEFAULT_PORT: True)
    monkeypatch.setattr(viewer, "_spawn", lambda *a, **k: spawned.append(1) or True)
    assert viewer.ensure() is True
    assert spawned == []


def test_ensure_spawns_when_the_port_is_free(monkeypatch):
    spawned = []
    monkeypatch.setattr(viewer, "is_up", lambda port=viewer.DEFAULT_PORT: False)
    monkeypatch.setattr(viewer, "available", lambda: True)
    monkeypatch.setattr(viewer, "_spawn", lambda *a, **k: spawned.append(1) or True)
    assert viewer.ensure() is True
    assert spawned == [1]


def test_ensure_does_nothing_without_npx(monkeypatch):
    """Never install anything implicitly from a hook."""
    monkeypatch.setattr(viewer, "is_up", lambda port=viewer.DEFAULT_PORT: False)
    monkeypatch.setattr(viewer, "available", lambda: False)
    monkeypatch.setattr(viewer, "_spawn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert viewer.ensure() is False


def test_the_disable_switch_is_honoured(monkeypatch):
    monkeypatch.setenv(viewer.DISABLE_ENV, "1")
    monkeypatch.setattr(viewer, "_spawn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    assert viewer.ensure() is False
    assert "disabled" in viewer.status()


def test_ensure_never_raises(monkeypatch):
    """A traceback from a hook lands in the operator's session."""
    monkeypatch.setattr(viewer, "is_up", lambda port=viewer.DEFAULT_PORT: (_ for _ in ()).throw(RuntimeError("boom")))
    assert viewer.ensure() is False


def test_is_up_is_false_for_a_closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert viewer.is_up(free) is False


def test_is_up_is_true_for_an_open_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert viewer.is_up(s.getsockname()[1]) is True


def test_status_explains_itself_when_down(monkeypatch):
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    monkeypatch.setattr(viewer, "is_up", lambda port=viewer.DEFAULT_PORT: False)
    monkeypatch.setattr(viewer, "available", lambda: True)
    assert "harness viewer --start" in viewer.status()
