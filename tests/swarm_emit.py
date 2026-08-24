import json
import os
import stat

from swarm import emit


def read_stream(home):
    files = list((home / "runs").glob("*.jsonl"))
    assert len(files) == 1, f"expected one stream file, got {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def test_emit_writes_one_line(swarm_home):
    assert emit.emit("SubagentStart", {"session_id": "s1", "agent_id": "a1"}) is True
    records = read_stream(swarm_home)
    assert len(records) == 1
    assert records[0]["event"] == "subagent_start"


def test_appends_rather_than_overwrites(swarm_home):
    emit.emit("SubagentStart", {"session_id": "s1", "agent_id": "a1"})
    emit.emit("SubagentStop", {"session_id": "s1", "agent_id": "a1"})
    assert len(read_stream(swarm_home)) == 2


def test_unknown_event_writes_nothing(swarm_home):
    assert emit.emit("NotAnEvent", {"session_id": "s1"}) is False
    assert list((swarm_home / "runs").glob("*.jsonl")) == []


def test_missing_runs_dir_is_created(swarm_home):
    (swarm_home / "runs").rmdir()
    assert emit.emit("SubagentStart", {"session_id": "s1"}) is True


def test_unwritable_runs_dir_returns_false_and_does_not_raise(swarm_home):
    runs = swarm_home / "runs"
    runs.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read+execute, no write
    try:
        assert emit.emit("SubagentStart", {"session_id": "s1"}) is False
    finally:
        runs.chmod(stat.S_IRWXU)


def test_malformed_payload_never_raises(swarm_home):
    """A recognised event name alone is enough to produce a record, even from
    a malformed payload -- project() fills every field with None rather than
    raising. Assert that real property: no raise, and a record lands each
    time. `in (True, False)` would pass no matter what emit() actually did."""
    for bad in (None, [], "string", 42, {"session_id": object()}):
        assert emit.emit("SubagentStart", bad) is True
    assert len(read_stream(swarm_home)) == 5


def test_each_record_is_one_line(swarm_home):
    emit.emit("SubagentStart", {"session_id": "s1", "agent_type": "has\nnewline"})
    text = (list((swarm_home / "runs").glob("*.jsonl"))[0]).read_text()
    assert text.count("\n") == 1


def test_missing_session_id_lands_in_the_unknown_file(swarm_home):
    """Without this, every session-less record from every session would
    collide silently in one 'unknown' file."""
    assert emit.emit("SubagentStart", {"agent_id": "a1"}) is True
    files = list((swarm_home / "runs").glob("*.jsonl"))
    assert len(files) == 1
    assert "unknown" in files[0].name


def test_day_derived_from_the_record_ts_not_a_fresh_wall_clock_read(swarm_home, monkeypatch):
    """A session that crosses UTC midnight between the ts being stamped and
    emit() asking the clock again for 'today' must not split into two files."""
    import swarm.project as project_module

    monkeypatch.setattr(project_module, "_now", lambda: "2026-01-01T23:59:59.999+00:00")
    assert emit.emit("SubagentStart", {"session_id": "s1"}) is True
    files = list((swarm_home / "runs").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name.startswith("2026-01-01-s1")
