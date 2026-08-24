"""Live shells, read from the process table.

The parsing targets are verbatim from this machine: Claude Code's shell
wrapper, and the artefacts `ps` introduces on the way out.
"""

from swarm import shells

# A real wrapped command, exactly as `ps` renders it — including the literal
# \012 it substitutes for an embedded newline and the '"'"' that survives
# nested shell quoting.
REAL_WRAPPED = (
    "/bin/zsh -c source /Users/you/.claude/shell-snapshots/snapshot-zsh-178708-rmkyph.sh "
    "2>/dev/null || true && setopt NO_EXTENDED_GLOB 2>/dev/null || true "
    "&& eval 'POD=api-worker-7b5945df7c-662ww; "
    "until kubectl logs -n prod-ns $POD --since=6h 2>/dev/null | grep -qE \"LOOP ENDED\"; "
    "do sleep 90; done' < /dev/null && pwd -P >| /tmp/claude-3712-cwd"
)


def test_the_real_command_is_recovered_from_the_wrapper():
    got = shells._real_command(REAL_WRAPPED)
    assert got.startswith("POD=api-worker")
    assert "kubectl logs" in got
    # None of the wrapper survives.
    assert "shell-snapshots" not in got
    assert "pwd -P" not in got


def test_transport_artefacts_are_undone():
    raw = "x && eval 'echo one\\012echo two' < /dev/null"
    assert shells._real_command(raw) == "echo one echo two"
    quoted = """x && eval 'ssh host '"'"'do thing'"'"'' < /dev/null"""
    assert "'\"'\"'" not in shells._real_command(quoted)


def test_an_unwrapped_command_is_left_alone():
    assert shells._real_command("node /usr/bin/some-mcp") == "node /usr/bin/some-mcp"


def test_a_truncated_command_degrades_rather_than_vanishing():
    """`ps` truncates long command lines, so the eval may have no closing quote.

    Returning the raw line is worse-looking and more honest than returning
    nothing at all.
    """
    truncated = "/bin/zsh -c source /x/snapshot.sh && eval 'python3 -c \"lots of"
    assert shells._real_command(truncated).startswith("/bin/zsh -c")


def test_elapsed_time_covers_every_ps_shape():
    assert shells._etime_seconds("00:31") == 31
    assert shells._etime_seconds("21:26:45") == 21 * 3600 + 26 * 60 + 45
    assert shells._etime_seconds("02-18:03:05") == 2 * 86400 + 18 * 3600 + 3 * 60 + 5


def test_shells_and_services_are_counted_separately(monkeypatch):
    """An MCP server is plumbing, not work.

    Counting the two together reads as "this project is busy" when the only
    thing running is the playwright server it started at launch.
    """
    rows = [
        (100, 1, "01:00:00", "claude --dangerously-skip-permissions"),
        (101, 100, "21:26:45", "/bin/zsh -c source /x/snapshot.sh && eval 'kubectl logs' < /dev/null"),
        (102, 100, "00:10:00", "node /Users/you/.npm-global/bin/ios-simulator-mcp"),
        (103, 999, "00:05:00", "/bin/zsh -c something unrelated"),  # not ours
    ]
    monkeypatch.setattr(shells, "_table", lambda: rows)
    monkeypatch.setattr(shells, "_cwds", lambda pids: {100: "/Users/you/Documents/Code/example-app"})

    found = shells.live()
    kinds = {s.pid: s.kind for s in found}
    assert kinds == {101: "shell", 102: "service"}     # 103 has another parent
    assert shells.by_project(found) == {"example-app": [s for s in found if s.pid == 101]}
    assert found[0].project == "example-app"


def test_a_process_merely_mentioning_claude_is_not_a_session(monkeypatch):
    """Every command in this repository mentions the word.

    Matching anywhere in the command line would make `swarm shells` itself
    look like a session and adopt its own children.
    """
    rows = [
        (200, 1, "00:01:00", "/bin/zsh -c echo claude --resume"),
        (201, 200, "00:00:30", "/bin/zsh -c source /x/s.sh && eval 'ls' < /dev/null"),
    ]
    monkeypatch.setattr(shells, "_table", lambda: rows)
    monkeypatch.setattr(shells, "_cwds", lambda pids: {})
    assert shells.live() == []


def test_no_processes_is_not_an_error(monkeypatch):
    monkeypatch.setattr(shells, "_table", lambda: [])
    assert shells.live() == []
    assert "no shells running" in shells.render([])


def test_an_unreadable_process_table_degrades_quietly(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("ps is missing")

    monkeypatch.setattr(shells.subprocess, "run", boom)
    # A viewer must not take itself down because `ps` was unavailable.
    assert shells._table() == []
    assert shells._cwds([1]) == {}
