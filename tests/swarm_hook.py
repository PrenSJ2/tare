import json
import os
import subprocess
import sys


def run_hook(args, stdin_text, home):
    """Invoke the entrypoint exactly as Claude Code will: argv + stdin."""
    env = dict(os.environ, SWARM_HOME=str(home), PYTHONPATH="src")
    return subprocess.run(
        [sys.executable, "-m", "swarm.hook", *args],
        input=stdin_text, capture_output=True, text=True, env=env,
    )


def test_hook_writes_a_record(swarm_home):
    payload = json.dumps({"session_id": "s1", "agent_id": "a1"})
    proc = run_hook(["SubagentStart"], payload, swarm_home)
    assert proc.returncode == 0
    assert proc.stderr == ""
    files = list((swarm_home / "runs").glob("*.jsonl"))
    assert len(files) == 1


def test_exit_zero_and_silent_on_invalid_json(swarm_home):
    proc = run_hook(["SubagentStart"], "{not json", swarm_home)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == ""


def test_exit_zero_and_silent_with_no_argument(swarm_home):
    proc = run_hook([], "{}", swarm_home)
    assert proc.returncode == 0
    assert proc.stderr == ""
    assert proc.stdout == ""


def test_empty_stdin_still_records_that_the_hook_fired(swarm_home):
    """Empty stdin is treated as an empty payload, not as nothing to do.

    A hook firing is an observable fact worth recording even with no data --
    the timestamp proves it ran. Pinned explicitly so the behaviour cannot
    flip silently.
    """
    proc = run_hook(["SubagentStop"], "", swarm_home)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert len(list((swarm_home / "runs").glob("*.jsonl"))) == 1


def test_exit_zero_when_runs_dir_is_unwritable(swarm_home):
    runs = swarm_home / "runs"
    runs.chmod(0o500)
    try:
        proc = run_hook(["SubagentStart"], json.dumps({"session_id": "s"}), swarm_home)
        assert proc.returncode == 0
        assert proc.stderr == ""
        assert proc.stdout == ""
    finally:
        runs.chmod(0o700)


def test_hook_writes_nothing_to_stdout(swarm_home):
    """Claude Code may interpret hook stdout. Say nothing."""
    proc = run_hook(["SubagentStart"], json.dumps({"session_id": "s"}), swarm_home)
    assert proc.stdout == ""
