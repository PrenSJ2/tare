"""Reader and view tests.

Everything here builds transcripts in a temporary HOME. Nothing reads the
operator's real ~/.claude.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swarm import reader, watch


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    (home / "projects" / "proj").mkdir(parents=True)
    monkeypatch.setenv("SWARM_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    monkeypatch.setattr(reader.paths, "claude_home", lambda: home)
    return home


def write_session(home, session, dispatches):
    """dispatches: list of (use_id, agent_id, label, agent_type, model)."""
    path = home / "projects" / "proj" / f"{session}.jsonl"
    rows = []
    for i, (use_id, agent_id, label, kind, model) in enumerate(dispatches):
        ts = f"2026-08-20T10:{i:02d}:00.000Z"
        rows.append({
            "timestamp": ts,
            "message": {"content": [{
                "type": "tool_use", "id": use_id, "name": "Agent",
                "input": {"description": label, "subagent_type": kind, "model": model},
            }]},
        })
        rows.append({
            "timestamp": ts,
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": use_id,
                "content": f"Async agent launched successfully.\nagentId: {agent_id}",
            }]},
        })
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def write_agent(home, agent_id, start, end, lines=3):
    d = home / "projects" / "proj" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"timestamp": start}] + [{"timestamp": start}] * (lines - 2) + [{"timestamp": end}]
    (d / f"agent-{agent_id}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_reads_label_type_model_and_real_duration(fake_home):
    write_session(fake_home, "sess", [("toolu_1", "a1111111111111111", "Review Task 8", "general-purpose", "sonnet")])
    write_agent(fake_home, "a1111111111111111", "2026-08-20T10:00:05.000Z", "2026-08-20T10:01:05.000Z")

    runs = reader.read_session("sess", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    assert len(runs) == 1
    run = runs[0]
    assert run.label == "Review Task 8"
    assert run.model == "sonnet"
    assert run.agent_type == "general-purpose"
    assert run.seconds == 60.0
    assert run.status == "done"


def test_duration_comes_from_the_agents_own_transcript_not_the_dispatch(fake_home):
    """An async dispatch is acknowledged in milliseconds; the tool_result
    timestamp is NOT the end of the run."""
    write_session(fake_home, "sess", [("toolu_1", "a1111111111111111", "Long one", "general-purpose", "sonnet")])
    write_agent(fake_home, "a1111111111111111", "2026-08-20T10:00:00.000Z", "2026-08-20T10:30:00.000Z")

    run = reader.read_session("sess", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))[0]
    assert run.seconds == 1800.0


def test_a_recently_written_agent_counts_as_running(fake_home):
    write_session(fake_home, "sess", [("toolu_1", "a1111111111111111", "Still going", "general-purpose", "haiku")])
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    write_agent(fake_home, "a1111111111111111", "2026-08-20T11:50:00.000Z", recent)

    run = reader.read_session("sess", now=now)[0]
    assert run.status == "running"


def test_a_dispatch_with_no_agent_transcript_is_unknown_not_dropped(fake_home):
    """Degrade and report: a run we cannot time still has a label worth showing."""
    write_session(fake_home, "sess", [("toolu_1", "a9999999999999999", "Vanished", "general-purpose", "haiku")])

    runs = reader.read_session("sess", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    assert len(runs) == 1
    assert runs[0].status == "unknown"
    assert runs[0].seconds is None
    assert runs[0].label == "Vanished"


def test_redact_hides_labels_but_keeps_timing(fake_home):
    write_session(fake_home, "sess", [("toolu_1", "a1111111111111111", "Client X migration", "general-purpose", "sonnet")])
    write_agent(fake_home, "a1111111111111111", "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")

    run = reader.read_session("sess", redact=True, now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))[0]
    assert "Client X" not in run.label
    assert run.seconds == 60.0


def test_a_truncated_final_line_does_not_break_reading(fake_home):
    """Transcripts are appended to by live sessions while this reads them."""
    path = write_session(fake_home, "sess", [("toolu_1", "a1111111111111111", "Fine", "general-purpose", "haiku")])
    write_agent(fake_home, "a1111111111111111", "2026-08-20T10:00:00.000Z", "2026-08-20T10:00:30.000Z")
    with path.open("a") as fh:
        fh.write('{"timestamp": "2026-08-20T10:0')

    runs = reader.read_session("sess", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    assert len(runs) == 1


def test_missing_session_returns_empty_rather_than_raising(fake_home):
    assert reader.read_session("nope") == []


# --- view -------------------------------------------------------------------


def _run(label, secs, status="done", model="sonnet"):
    start = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    return reader.AgentRun(
        agent_id="a", label=label, agent_type="general-purpose", model=model,
        dispatched=start, started=start, ended=start + timedelta(seconds=secs),
        lines=5, status=status,
    )


def test_render_puts_running_agents_first_and_counts_them():
    now = datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc)
    out = watch.render([_run("done one", 30), _run("live one", 60, status="running")], now=now)
    assert out.index("RUNNING") < out.index("FINISHED")
    assert "live one" in out


def test_render_never_implies_the_tail_is_everything():
    """A view that shows 8 of 106 without saying so is a lie by omission."""
    runs = [_run(f"task {i}", i) for i in range(20)]
    out = watch.render(runs, now=datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc), tail=5)
    assert "15 more not shown" in out


def test_render_flags_an_agent_running_far_longer_than_its_peers():
    now = datetime(2026, 8, 20, 10, 30, tzinfo=timezone.utc)
    runs = [_run(f"quick {i}", 60) for i in range(5)]
    runs.append(_run("stuck", 0, status="running"))
    runs[-1].started = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    out = watch.render(runs, now=now)
    assert "← slow" in out


def test_render_survives_a_run_with_no_timing():
    out = watch.render([_run("mystery", 0, status="unknown")], now=datetime(2026, 8, 20, 10, 5, tzinfo=timezone.utc))
    assert "mystery" in out
