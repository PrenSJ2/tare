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


def write_stream(home, session, day, events):
    """events: list of dicts, each an already-projected stream record."""
    d = home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{day}-{session}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def age_transcript(home, session, seconds):
    """Backdate a session transcript's mtime by `seconds`."""
    import os
    import time as _time
    path = home / "projects" / "proj" / f"{session}.jsonl"
    when = _time.time() - seconds
    os.utime(path, (when, when))


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


# --- session state -----------------------------------------------------------


def test_a_session_end_record_is_certain_and_beats_a_warm_mtime(fake_home):
    """The cctv case: written twelve minutes ago, but definitively over.

    mtime alone would call this live. The stream knows better, and the state
    must say which signal decided so a reader can tell the two apart.
    """
    write_session(fake_home, "s1", [])
    write_stream(fake_home, "s1", "2026-09-11", [
        {"ts": "2026-09-11T10:00:00+00:00", "session": "s1", "event": "session_end",
         "reason": "prompt_input_exit"},
    ])
    st = reader.session_state("proj", "s1")
    assert st.state == "ended"
    assert st.by == "session_end"
    assert st.reason == "prompt_input_exit"


def test_no_stream_and_a_warm_transcript_reads_live(fake_home):
    write_session(fake_home, "s2", [])
    age_transcript(fake_home, "s2", 60)
    st = reader.session_state("proj", "s2")
    assert st.state == "live"
    assert st.by == "mtime"
    assert st.reason is None


def test_no_stream_and_a_cold_transcript_reads_cold(fake_home):
    write_session(fake_home, "s3", [])
    age_transcript(fake_home, "s3", 33 * 3600)
    st = reader.session_state("proj", "s3")
    assert st.state == "cold"
    assert st.by == "mtime"


def test_the_window_boundary(fake_home):
    write_session(fake_home, "s4", [])
    age_transcript(fake_home, "s4", reader.SESSION_COLD_AFTER_SECONDS - 30)
    assert reader.session_state("proj", "s4").state == "live"
    age_transcript(fake_home, "s4", reader.SESSION_COLD_AFTER_SECONDS + 30)
    assert reader.session_state("proj", "s4").state == "cold"


def test_a_stream_spanning_utc_midnight_still_resolves_its_end(fake_home):
    """One session, two stream files. The end is in the second."""
    write_session(fake_home, "s5", [])
    write_stream(fake_home, "s5", "2026-09-10", [
        {"ts": "2026-09-10T23:59:00+00:00", "session": "s5", "event": "subagent_start"},
    ])
    write_stream(fake_home, "s5", "2026-09-11", [
        {"ts": "2026-09-11T00:01:00+00:00", "session": "s5", "event": "session_end",
         "reason": "other"},
    ])
    st = reader.session_state("proj", "s5")
    assert st.state == "ended"
    assert st.reason == "other"


def test_clear_retires_the_session_id(fake_home):
    """/clear ends that id even though the operator keeps working."""
    write_session(fake_home, "s6", [])
    age_transcript(fake_home, "s6", 60)
    write_stream(fake_home, "s6", "2026-09-11", [
        {"ts": "2026-09-11T10:00:00+00:00", "session": "s6", "event": "session_end",
         "reason": "clear"},
    ])
    st = reader.session_state("proj", "s6")
    assert st.state == "ended"
    assert st.reason == "clear"


def test_without_a_runs_directory_everything_falls_to_mtime(fake_home):
    """The tier the live corpus does not currently exercise.

    On a machine where swarm's recording hooks were never installed there is
    no stream at all, and mtime is the only signal there is.
    """
    assert not (fake_home / "runs").exists()
    write_session(fake_home, "s7", [])
    age_transcript(fake_home, "s7", 60)
    st = reader.session_state("proj", "s7")
    assert st.by == "mtime"
    assert st.state == "live"


def test_a_future_mtime_clamps_to_live(fake_home):
    """Clock skew is wrong in the safe direction: never hide a live session."""
    write_session(fake_home, "s8", [])
    age_transcript(fake_home, "s8", -600)
    st = reader.session_state("proj", "s8")
    assert st.age == 0
    assert st.state == "live"


def test_a_corrupt_stream_line_does_not_hide_the_end_record(fake_home):
    write_session(fake_home, "s9", [])
    d = fake_home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"2026-09-11-s9.jsonl").write_text(
        "{not json at all\n"
        + json.dumps({"ts": "2026-09-11T10:00:00+00:00", "session": "s9",
                      "event": "session_end", "reason": "other"}) + "\n")
    assert reader.session_state("proj", "s9").state == "ended"


def test_fleet_keeps_session_identity(fake_home):
    """Two sessions in ONE project must not arrive merged.

    The old return type was dict[project, list[AgentRun]], which made a live
    session and a dead one in the same repository indistinguishable.
    """
    write_session(fake_home, "live1", [
        ("toolu_a", "aaaaaaaaaaaaaaaa", "live work", "general-purpose", "sonnet")])
    write_agent(fake_home, "aaaaaaaaaaaaaaaa",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    write_session(fake_home, "dead1", [
        ("toolu_b", "bbbbbbbbbbbbbbbb", "old work", "general-purpose", "sonnet")])
    write_agent(fake_home, "bbbbbbbbbbbbbbbb",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    age_transcript(fake_home, "live1", 60)
    age_transcript(fake_home, "dead1", 40 * 3600)

    got = reader.fleet()
    assert [s.session for s in got] == ["live1", "dead1"]      # live first
    assert got[0].project == got[1].project == "proj"
    assert got[0].state.state == "live"
    assert got[1].state.state == "cold"


def test_fleet_does_not_parse_a_session_that_is_over(fake_home, monkeypatch):
    """The saving must not silently regress into a full walk.

    Parsing the 25 main transcripts is 86% of the console payload's cost, and
    on the machine this was built for 21 of them yield nothing.
    """
    write_session(fake_home, "dead2", [
        ("toolu_c", "cccccccccccccccc", "old work", "general-purpose", "sonnet")])
    age_transcript(fake_home, "dead2", 40 * 3600)

    parsed = []
    real = reader.read_session
    monkeypatch.setattr(reader, "read_session",
                        lambda s, **kw: parsed.append(s) or real(s, **kw))

    got = reader.fleet()
    assert parsed == []                    # nobody looked
    assert got[0].read is False
    assert got[0].runs == []


def test_read_false_is_not_the_same_fact_as_no_agents(fake_home):
    """A live session that dispatched nothing is read and empty.
    A cold one is unread. The UI must be able to tell them apart.
    """
    write_session(fake_home, "empty", [])
    write_session(fake_home, "over", [])
    age_transcript(fake_home, "empty", 60)
    age_transcript(fake_home, "over", 40 * 3600)

    by_id = {s.session: s for s in reader.fleet()}
    assert by_id["empty"].read is True and by_id["empty"].runs == []
    assert by_id["over"].read is False and by_id["over"].runs == []


def test_nothing_is_lost(fake_home):
    """The capability half's bargain, applied to the agent half.

    A view that hides a session must still be able to produce it. include_cold
    returns every agent, whatever each session's state.
    """
    write_session(fake_home, "live3", [
        ("toolu_d", "dddddddddddddddd", "live work", "general-purpose", "sonnet")])
    write_agent(fake_home, "dddddddddddddddd",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    write_session(fake_home, "dead3", [
        ("toolu_e", "eeeeeeeeeeeeeeee", "old work", "general-purpose", "sonnet")])
    write_agent(fake_home, "eeeeeeeeeeeeeeee",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    age_transcript(fake_home, "live3", 60)
    age_transcript(fake_home, "dead3", 40 * 3600)

    default = {r.agent_id for s in reader.fleet() for r in s.runs}
    everything = {r.agent_id for s in reader.fleet(include_cold=True) for r in s.runs}
    assert default == {"dddddddddddddddd"}
    assert everything == {"dddddddddddddddd", "eeeeeeeeeeeeeeee"}
    assert all(s.read for s in reader.fleet(include_cold=True))
