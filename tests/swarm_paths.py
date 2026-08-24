from pathlib import Path

from swarm import paths


def test_claude_home_defaults_to_real_location(monkeypatch):
    monkeypatch.delenv("SWARM_HOME", raising=False)
    assert paths.claude_home() == Path.home() / ".claude"


def test_swarm_home_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_HOME", str(tmp_path))
    assert paths.claude_home() == tmp_path
    assert paths.runs_dir() == tmp_path / "runs"
    assert paths.settings_path() == tmp_path / "settings.json"


def test_stream_path_is_dated_and_session_keyed(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_HOME", str(tmp_path))
    p = paths.stream_path("abc123", "2026-08-19")
    assert p == tmp_path / "runs" / "2026-08-19-abc123.jsonl"


def test_stream_path_sanitises_a_hostile_session_id(monkeypatch, tmp_path):
    """A session id is external input; it must not escape runs_dir."""
    monkeypatch.setenv("SWARM_HOME", str(tmp_path))
    p = paths.stream_path("../../etc/passwd", "2026-08-19")
    assert p.parent == paths.runs_dir()
    assert "/" not in p.name
    assert ".." not in p.name


def test_no_module_resolves_claude_home_directly():
    src = Path(__file__).parent.parent / "src" / "swarm"
    offenders = []
    for py in src.rglob("*.py"):
        if py.name == "paths.py":
            continue
        text = py.read_text()
        for needle in ['".claude"', "'.claude'", "Path.home()",
                       'environ["HOME"]', "environ['HOME']",
                       'environ.get("HOME"', "environ.get('HOME'",
                       "expanduser"]:
            if needle in text:
                offenders.append(f"{py.name}: {needle}")
    assert offenders == [], f"must use swarm.paths instead: {offenders}"
