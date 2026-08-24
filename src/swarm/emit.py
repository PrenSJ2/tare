"""Append one record to the session's stream. Never raises.

This runs inside a live Claude Code session on every dispatch. A traceback here
would surface in the operator's work, and an exception could delay the session.
So every failure mode returns False quietly: the reader (swarm doctor) is where
the operator learns something was lost.
"""

import json
from datetime import datetime, timezone

from swarm import paths
from swarm.project import project


def _day_from_ts(ts) -> str:
    """The record's own ts, not a fresh wall-clock read.

    A hook fires, the record is projected with a ts, and only afterwards did
    this used to ask the clock again for "today" -- independently of that ts.
    A session whose stream crosses UTC midnight between those two reads would
    split across two files. Deriving the day from the record's own ts instead
    ties one session to one file. Falls back to the current date only if the
    ts itself is missing or unparseable, so a record is still written.
    """
    try:
        return datetime.fromisoformat(str(ts)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def emit(event: str, payload) -> bool:
    try:
        record = project(event, payload)
        if record is None:
            return False

        day = _day_from_ts(record.get("ts"))
        target = paths.stream_path(str(record.get("session") or "unknown"), day)
        target.parent.mkdir(parents=True, exist_ok=True)

        # default=str so an unserialisable value degrades to its repr rather
        # than losing the whole record. One write of one line: atomic enough at
        # this size for concurrent subagents appending to the same file.
        line = json.dumps(record, default=str, ensure_ascii=False)
        line = line.replace("\n", " ").replace("\r", " ")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:
        return False
