"""Single source of truth for every filesystem location swarm touches.

No other module may construct a path into ~/.claude. Tests set SWARM_HOME to a
temporary directory so no test can write to the real configuration.
"""

import os
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def claude_home() -> Path:
    override = os.environ.get("SWARM_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def runs_dir() -> Path:
    return claude_home() / "runs"


def settings_path() -> Path:
    return claude_home() / "settings.json"


def stream_path(session_id: str, day: str) -> Path:
    """One append-only stream per session.

    The session id arrives from a hook payload -- external input -- so every
    character outside [A-Za-z0-9_-] is replaced. A id of "../../etc/passwd"
    must not write outside runs_dir.
    """
    safe_session = _UNSAFE.sub("_", session_id or "unknown")
    safe_day = _UNSAFE.sub("_", day or "unknown")
    return runs_dir() / f"{safe_day}-{safe_session}.jsonl"


def state_dir() -> Path:
    """Where swarm keeps its own durable records, as opposed to captured runs.

    Separate from `runs_dir` on purpose: a stream is a rebuildable capture of
    one session, while the nightshift ledger is the only account of what ran
    unattended. Nothing regenerates it, so it must not sit among files that
    look disposable.
    """
    return claude_home() / "swarm"


def working_tree(value: str) -> Path:
    """A user-supplied working directory, resolved.

    Here rather than at the call site because `~` expansion is home
    resolution, and the rule this module exists to enforce is that home
    resolution happens in exactly one place -- even when the target is a
    repository rather than ~/.claude.
    """
    return Path(value).expanduser().resolve()


def projects_dir() -> Path:
    """Where Claude Code keeps session transcripts.

    `reader` uses these rather than the hook stream: on a real 106-dispatch
    session the stream resolved 7 agents, the transcripts resolved all 106.
    """
    return claude_home() / "projects"
