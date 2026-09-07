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
    # SWARM_HOME as well as TARE_HOME, and this is not belt-and-braces.
    #
    # The two halves read their root from different variables, so a fixture
    # setting only TARE_HOME isolated tare and left swarm pointing at the
    # operator's real ~/.claude. A test exercising both halves then wrote
    # eight hook entries into the real settings.json -- pointing at pytest
    # temp paths that no longer existed by the time it finished, so every
    # later session would have run a hook that could not be found.
    #
    # Fixed here rather than in that one test because the hole belongs to the
    # fixture: any future test touching both halves falls into it, and the
    # damage is silent until somebody reads their own settings file.
    monkeypatch.setenv("TARE_HOME", str(home))
    monkeypatch.setenv("SWARM_HOME", str(home))
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
