"""Viewer supervision tests.

`ensure()` runs from a SessionStart hook on every session of every project, so
the tests that matter are the ones proving it stays out of the way: never
spawns a second server, never blocks, never speaks, never raises.
"""

from __future__ import annotations

import socket

from tare import viewer


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


def test_port_in_use_detects_a_bound_port():
    """Still worth knowing -- but as "someone holds this port", NOT as "the
    viewer is up". Conflating the two is what hid a broken viewer for an hour."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert viewer.port_in_use(free) is False

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert viewer.port_in_use(s.getsockname()[1]) is True


def test_status_explains_itself_when_down(monkeypatch, tmp_path):
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)
    monkeypatch.setattr(viewer, "port_in_use", lambda port: False)
    monkeypatch.setattr(viewer, "available", lambda: True)
    assert "tare viewer --start" in viewer.status()


def test_a_stranger_on_the_port_is_not_the_viewer(monkeypatch, tmp_path):
    """The failure that looks like success.

    A Next.js dev server had held agent-flow's default port for three days, so
    a port probe reported the viewer as running while every agent event was
    being dropped -- with nothing anywhere saying so.
    """
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)   # no instances
    monkeypatch.setattr(viewer, "port_in_use", lambda port: True)
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    assert viewer.is_up() is False
    assert "already holds port" in viewer.status()


def test_a_stale_discovery_file_is_not_a_live_instance(monkeypatch, tmp_path):
    """A pid file survives a reboot and a kill -9; it must not be believed."""
    import json
    (tmp_path / "dead.json").write_text(json.dumps({"pid": 999999, "port": 1234, "workspace": "/x"}))
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)
    assert viewer.instances() == []


def test_a_live_discovery_file_counts(monkeypatch, tmp_path):
    import json, os
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": os.getpid(), "port": 1234, "workspace": str(tmp_path)}))
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    assert len(viewer.instances()) == 1 and viewer.is_up() is True


def test_the_workspace_is_an_ancestor_of_every_project(monkeypatch):
    """agent-flow's hook drops any event whose cwd is outside the registered
    workspace, so a per-project workspace loses every other project silently."""
    monkeypatch.delenv("TARE_VIEWER_ROOT", raising=False)
    from pathlib import Path
    assert viewer.workspace_root() == Path.home()


def test_the_console_is_a_local_build_not_a_fetched_package(tmp_path, monkeypatch):
    """The UI is tare's own fork, built locally. Looked up rather than vendored
    so it stays a git checkout that can still merge from upstream."""
    monkeypatch.setenv(viewer.FORK_DIR_ENV, str(tmp_path))
    assert viewer.fork_build() is None, "no build present yet"

    built = tmp_path / "app" / "dist"
    built.mkdir(parents=True)
    (built / "app.js").write_text("//")
    assert viewer.fork_build() == built / "app.js"


def test_status_names_where_the_build_came_from(monkeypatch, tmp_path):
    import json, os
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": os.getpid(), "port": 1234, "workspace": str(tmp_path)}))
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)
    monkeypatch.setattr(viewer, "fork_build", lambda: tmp_path / "app" / "dist" / "app.js")
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    assert "tare-console from" in viewer.status()


def test_status_says_how_to_build_when_missing(monkeypatch, tmp_path):
    """Not built is an ordinary state with an ordinary fix -- say the fix."""
    monkeypatch.setattr(viewer, "_discovery_dir", lambda: tmp_path)
    monkeypatch.setattr(viewer, "port_in_use", lambda port: False)
    monkeypatch.setattr(viewer, "fork_build", lambda: None)
    monkeypatch.delenv(viewer.DISABLE_ENV, raising=False)
    assert "pnpm run build:app" in viewer.status()
