"""What each session is running in a shell, right now.

Transcripts record that a command was *launched*; they cannot say whether it
is still going. On this machine 141 background shells were launched and not
one `BashOutput` or `KillShell` call was ever recorded, so the transcript's
answer to "what is running now" is a list of things that were true once. The
only honest source is the process table.

## How a shell is attributed to a session

Claude Code runs each session as a `claude` process and spawns tool commands
as its children, wrapped in `/bin/zsh -c source <snapshot> ... && eval '<the
real command>'`. So the children of a `claude` process are that session's
shells, and the command is recoverable from inside the `eval`.

**The attribution is to a PROJECT, not to a session id.** A `claude` process
exposes its working directory and nothing else useful -- it does not hold its
transcript open -- so two sessions in one repository cannot be told apart from
out here. Everything below says "project" for that reason, and the UI must not
promise more than that.

## Shells and services are different things

A session's children also include the MCP servers it started: playwright,
ios-simulator, and so on. Those are plumbing that lives for the whole session
and says nothing about what is being worked on, whereas a shell polling
`kubectl logs` for six hours is exactly what someone wants to see. They are
separated rather than counted together.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Claude Code wraps every tool command the same way; the real one is inside.
_EVAL = re.compile(r"&& eval '(.*)' < /dev/null", re.S)
_SNAPSHOT = re.compile(r"shell-snapshots/snapshot-")

# How a `claude` session process announces itself in the process table.
_CLAUDE = re.compile(r"(^|/)claude( |$)")


@dataclass
class Shell:
    pid: int
    session_pid: int
    project: str
    cwd: str
    command: str
    seconds: float
    kind: str  # "shell" — a tool command; "service" — an MCP server


def _etime_seconds(etime: str) -> float:
    """`ps` elapsed time: [[dd-]hh:]mm:ss."""
    days = 0
    if "-" in etime:
        day_part, etime = etime.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _table() -> list[tuple[int, int, str, str]]:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
        except ValueError:
            continue
    return rows


def _cwds(pids: list[int]) -> dict[int, str]:
    """Working directory per pid, in ONE lsof call.

    One call per process is ~100ms each and this runs behind a polling API;
    lsof takes a comma-separated list, so it costs the same for one or twenty.
    """
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", ",".join(str(p) for p in pids),
             "-Fpn"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    found: dict[int, str] = {}
    current = None
    for line in out.stdout.splitlines():
        if line.startswith("p"):
            try:
                current = int(line[1:])
            except ValueError:
                current = None
        elif line.startswith("n") and current is not None:
            found.setdefault(current, line[1:])
    return found


def _real_command(raw: str) -> str:
    found = _EVAL.search(raw)
    text = found.group(1) if found else raw
    # `ps` renders embedded newlines as a literal \012, and a command that
    # went through nested shell quoting arrives carrying '"'"' where it
    # meant a single quote. Both are artefacts of the transport, not of the
    # command anyone wrote.
    text = text.replace("\\012", " ").replace("'\"'\"'", "'")
    return " ".join(text.split())


def live() -> list[Shell]:
    """Every command and service running under a live session, now."""
    rows = _table()
    if not rows:
        return []

    # Matched on the EXECUTABLE only. `claude --resume`, `claude -p` and a
    # bare `claude` all count; a shell whose arguments merely mention the word
    # -- every command in this repository, for instance -- does not.
    sessions = {pid: cmd for pid, _ppid, _etime, cmd in rows
                if _CLAUDE.search((cmd.split() or [""])[0])}
    if not sessions:
        return []

    cwds = _cwds(list(sessions))
    out: list[Shell] = []
    for pid, ppid, etime, cmd in rows:
        if ppid not in sessions:
            continue
        cwd = cwds.get(ppid, "")
        is_shell = bool(_SNAPSHOT.search(cmd)) or cmd.startswith("/bin/zsh -c")
        out.append(Shell(
            pid=pid,
            session_pid=ppid,
            project=Path(cwd).name if cwd else "",
            cwd=cwd,
            command=_real_command(cmd),
            seconds=_etime_seconds(etime),
            kind="shell" if is_shell else "service",
        ))
    out.sort(key=lambda s: (s.project, -s.seconds))
    return out


def by_project(shells: list[Shell] | None = None) -> dict[str, list[Shell]]:
    """Shells only, grouped. Services are deliberately dropped here.

    A count that includes every MCP server reads as "this project is busy"
    when nothing is happening at all.
    """
    grouped: dict[str, list[Shell]] = {}
    for shell in (shells if shells is not None else live()):
        if shell.kind != "shell":
            continue
        grouped.setdefault(shell.project or "(unknown)", []).append(shell)
    return grouped


def render(shells: list[Shell] | None = None) -> str:
    found = shells if shells is not None else live()
    grouped = by_project(found)
    services = sum(1 for s in found if s.kind == "service")
    if not grouped:
        return f"no shells running ({services} MCP service(s) up)"

    lines = [f"{sum(len(v) for v in grouped.values())} shell(s) running "
             f"across {len(grouped)} project(s); {services} MCP service(s) up", ""]
    for project, items in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"{project}  ({len(items)})")
        for shell in items:
            lines.append(f"  {_clock(shell.seconds):>9}  {shell.command[:96]}")
    return "\n".join(lines)


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
