import json

from swarm import doctor


def write_stream(home, records, name="2026-08-19-s1.jsonl"):
    path = home / "runs" / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def test_counts_records_and_pairs(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:30.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.records == 2
    assert rep.agents == 1
    assert rep.unmatched_starts == 0


def test_duration_is_derived_from_paired_timestamps(swarm_home):
    """No payload carries a duration; it is the gap between start and stop."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:30.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    assert doctor.inspect(path).total_duration_ms == 30_000


def test_unpaired_agent_contributes_no_duration(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.unmatched_starts == 1
    assert rep.total_duration_ms == 0


def test_unparseable_timestamp_does_not_break_the_total(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "not-a-time", "session": "s1", "event": "subagent_start",
         "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:30.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.total_duration_ms == 0
    assert rep.agents == 1


def test_orphan_stop_is_counted(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "ghost", "agent_type": "Explore"},
    ])
    assert doctor.inspect(path).orphan_stops == 1


def test_stop_without_start_is_not_a_problem(swarm_home):
    """SubagentStart does not fire for most dispatches; a bare stop is the
    normal case on a real stream, not data loss. It must not appear as a
    Problem, or the one channel that reports real loss cries wolf on every
    stream."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "ghost", "agent_type": None},
    ])
    rep = doctor.inspect(path)
    assert rep.orphan_stops == 1
    text = doctor.render(rep)
    assert "Problems" not in text
    assert "no problems" in text.lower()


def test_unmatched_start_remains_a_problem(swarm_home):
    """A start with no stop is a genuine anomaly -- still running, or lost."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
    ])
    text = doctor.render(doctor.inspect(path))
    assert "Problems:" in text
    assert "unmatched start" in text


def test_malformed_lines_are_counted_not_fatal(swarm_home):
    path = swarm_home / "runs" / "2026-08-19-s1.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
                    "event": "subagent_start", "agent_id": "a1"}) + "\n"
        + "{ this is not json\n"
    )
    rep = doctor.inspect(path)
    assert rep.malformed == 1
    assert rep.records == 1


def test_agent_types_are_tallied(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:01.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a2", "agent_type": "Explore"},
    ])
    assert doctor.inspect(path).agent_types["Explore"] == 2


def test_agent_type_is_tallied_from_stops_too(swarm_home):
    """Most dispatches only ever produce a stop; a tally limited to starts
    would describe the minority, not the stream."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "code-reviewer"},
    ])
    assert doctor.inspect(path).agent_types["code-reviewer"] == 1


def test_paired_agents_counted_and_stated_in_render(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:05.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:10.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a2", "agent_type": None},
    ])
    rep = doctor.inspect(path)
    assert rep.paired_agents == 1
    assert rep.agents == 2
    text = doctor.render(rep)
    assert "1 of 2 agent(s)" in text


def test_render_names_what_was_lost(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
    ])
    text = doctor.render(doctor.inspect(path))
    assert "unmatched" in text.lower()


def test_render_states_that_spend_is_not_captured(swarm_home):
    """Phase A cannot record tokens. The report must not imply otherwise."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    text = doctor.render(doctor.inspect(path)).lower()
    assert "token" in text
    assert "not captured" in text or "not available" in text


def test_reused_agent_id_counts_both_durations_and_says_so(swarm_home):
    """A dict keyed on agent_id would drop the first dispatch silently.

    That is the exact failure this module exists to surface, so both durations
    must be counted and the reuse must appear as a problem.
    """
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:10.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:20.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:35.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.total_duration_ms == 25_000  # 10s + 15s, neither lost
    assert rep.reused_agent_ids == 1
    assert rep.unmatched_starts == 0
    assert "more than once" in doctor.render(rep)


def test_negative_duration_is_counted_not_silently_dropped(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:30.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.total_duration_ms == 0
    assert rep.negative_durations == 1
    assert "stopped before they started" in doctor.render(rep)


def test_durations_sum_across_distinct_agents(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:05.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a2", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:07.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a2", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)
    assert rep.total_duration_ms == 12_000
    assert rep.agents == 2
    assert rep.reused_agent_ids == 0


def test_missing_file_is_not_a_crash(swarm_home):
    rep = doctor.inspect(swarm_home / "runs" / "does-not-exist.jsonl")
    assert rep.records == 0


def test_naive_ts_paired_with_aware_ts_does_not_raise(swarm_home):
    """A naive and a timezone-aware datetime cannot be subtracted; the
    robustness module must not raise TypeError over it."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000", "session": "s1",  # naive
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:30.000+00:00", "session": "s1",  # aware
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
    ])
    rep = doctor.inspect(path)  # must not raise
    assert rep.total_duration_ms == 0


def test_check_hook_command_when_nothing_installed(swarm_home):
    assert doctor.check_hook_command() == []


def test_check_hook_command_degrades_on_missing_settings(swarm_home):
    assert not (swarm_home / "settings.json").exists()
    assert doctor.check_hook_command() == []


def test_check_hook_command_degrades_on_malformed_settings(swarm_home):
    (swarm_home / "settings.json").write_text("{ not json")
    assert doctor.check_hook_command() == []


def test_check_hook_command_reports_a_missing_executable(swarm_home):
    from swarm import install

    missing = swarm_home / "nonexistent-venv" / "swarm-hook"
    install.install(str(missing))
    problems = doctor.check_hook_command()
    assert len(problems) == 1
    assert "not found" in problems[0]
    assert str(missing) in problems[0]


def test_check_hook_command_passes_when_executable_exists(swarm_home, tmp_path):
    from swarm import install

    hook = tmp_path / "swarm-hook"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    install.install(str(hook))
    assert doctor.check_hook_command() == []


def test_check_hook_command_reports_a_non_executable_file(swarm_home, tmp_path):
    from swarm import install

    hook = tmp_path / "swarm-hook"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o644)  # not executable
    install.install(str(hook))
    problems = doctor.check_hook_command()
    assert len(problems) == 1
    assert "not executable" in problems[0]


def test_render_includes_extra_problems(swarm_home):
    path = write_stream(swarm_home, [])
    text = doctor.render(doctor.inspect(path), ["something bad happened"])
    assert "Problems:" in text
    assert "something bad happened" in text


def test_zero_paired_agents_says_unavailable_not_zero(swarm_home):
    """Stop-only is the common stream shape. "0s" would read as "took no time"."""
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": f"a{i}", "agent_type": None}
        for i in range(3)
    ])
    text = doctor.render(doctor.inspect(path))
    assert "unavailable" in text
    assert "0s" not in text


def test_partial_pairing_states_the_total_is_unknown(swarm_home):
    path = write_stream(swarm_home, [
        {"ts": "2026-08-19T10:00:00.000+00:00", "session": "s1",
         "event": "subagent_start", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:05.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a1", "agent_type": "Explore"},
        {"ts": "2026-08-19T10:00:06.000+00:00", "session": "s1",
         "event": "subagent_stop", "agent_id": "a2", "agent_type": None},
    ])
    text = doctor.render(doctor.inspect(path))
    assert "1 of 2" in text
    assert "unknown" in text
