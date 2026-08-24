"""Shared fixtures.

`fake_home` points every `paths.*` helper at a temporary directory. Use it in
every test that touches the filesystem or the database -- nothing in the suite
may read or write the operator's real ~/.claude.

Note for anyone writing ad hoc probe scripts alongside the suite:
`os.environ.setdefault("TARE_HOME", ...)` is NOT sufficient, and
`setdefault` on HOME is a no-op when HOME is already set. Use monkeypatch, or
this fixture.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    (home / "skills").mkdir(parents=True)
    (home / "agents").mkdir(parents=True)
    (home / "plugins" / "cache").mkdir(parents=True)
    (home / "plugins" / "marketplaces").mkdir(parents=True)
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("TARE_HOME", str(home))
    return home


@pytest.fixture
def swarm_home(tmp_path, monkeypatch):
    """An isolated ~/.claude for the agent-observation half.

    Separate from `fake_home` only because that half reads its root from
    SWARM_HOME. Same rule applies: no test may touch the real configuration.
    """
    home = tmp_path / "claude"
    (home / "runs").mkdir(parents=True)
    monkeypatch.setenv("SWARM_HOME", str(home))
    monkeypatch.setenv("TARE_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _clear_payload_cache():
    """The console payload is cached globally for a few seconds.

    Without this, one test's payload answers another test's assertion — and
    because the cache is populated from the REAL ~/.claude when a test forgets
    its fixture, a test could pass on data it never created. That is exactly
    how `test_it_degrades_without_swarm` passed while asserting nothing.
    """
    from tare import console

    console._PAYLOAD_CACHE = None
    yield
    console._PAYLOAD_CACHE = None
