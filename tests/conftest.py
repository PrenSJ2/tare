"""Shared fixtures.

`fake_home` points every `paths.*` helper at a temporary directory. Use it in
every test that touches the filesystem or the database -- nothing in the suite
may read or write the operator's real ~/.claude.

Note for anyone writing ad hoc probe scripts alongside the suite:
`os.environ.setdefault("HARNESS_HOME", ...)` is NOT sufficient, and
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
    monkeypatch.setenv("HARNESS_HOME", str(home))
    return home
