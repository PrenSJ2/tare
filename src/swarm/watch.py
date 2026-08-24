"""Live terminal view of a session's agent fleet.

`render` is a pure function of a run list so the layout is testable without a
terminal, a clock, or a live session. `watch` is the loop around it.

The view answers the question that prompted this: *what is happening right
now, and is anything stuck?* So running agents sort to the top with a live
elapsed clock, and everything finished collapses into a tail ordered by cost.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime

from .reader import AgentRun, current_session, read_session

# A run that has been going noticeably longer than its peers is the signal an
# operator actually acts on -- the previous project's worst task ran 25x the
# median before anyone noticed.
SLOW_MULTIPLE = 3.0


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "  --  "
    seconds = int(seconds)
    return f"{seconds // 60:3d}:{seconds % 60:02d}"


def _elapsed(run: AgentRun, now: datetime) -> float | None:
    if run.status == "running" and run.started:
        return (now - run.started).total_seconds()
    return run.seconds


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def render(runs: list[AgentRun], *, now: datetime, width: int = 80, tail: int = 8) -> str:
    """The whole view as one string."""
    running = [r for r in runs if r.status == "running"]
    finished = [r for r in runs if r.status != "running"]
    done_secs = [r.seconds for r in finished if r.seconds is not None]
    total = sum(done_secs) + sum(_elapsed(r, now) or 0 for r in running)
    typical = _median(done_secs)

    label_width = max(20, width - 34)
    lines: list[str] = []
    session_note = f"{len(runs)} agent(s)"
    lines.append(f"swarm  ·  {session_note}  ·  {total / 60:.0f} min of agent time")
    lines.append("")

    if running:
        lines.append(f"RUNNING ({len(running)})")
        for run in sorted(running, key=lambda r: -(_elapsed(r, now) or 0)):
            secs = _elapsed(run, now) or 0
            # Flag rather than hide: a slow agent is the one thing here worth
            # interrupting, and it is invisible in a list sorted by start time.
            flag = "  ← slow" if typical and secs > typical * SLOW_MULTIPLE else ""
            lines.append(
                f"  ▸ {_clock(secs)}  {(run.model or '?'):7.7s} {run.label[:label_width]}{flag}"
            )
        lines.append("")

    if finished:
        lines.append(f"FINISHED ({len(finished)}) — longest")
        for run in sorted(finished, key=lambda r: -(r.seconds or 0))[:tail]:
            lines.append(
                f"  ✔ {_clock(run.seconds)}  {(run.model or '?'):7.7s} {run.label[:label_width]}"
            )
        if len(finished) > tail:
            # Never imply the tail is everything.
            lines.append(f"  … {len(finished) - tail} more not shown")

    return "\n".join(lines)


def watch(session: str | None = None, *, interval: float = 2.0, redact: bool = False) -> int:
    """Re-render until interrupted."""
    session = session or current_session()
    if session is None:
        print("no session transcript found", file=sys.stderr)
        return 1

    try:
        while True:
            width = shutil.get_terminal_size((80, 24)).columns
            runs = read_session(session, redact=redact)
            now = datetime.now().astimezone()
            os.system("clear" if os.name != "nt" else "cls")
            print(render(runs, now=now, width=width))
            print(f"\n  {session[:8]}  ·  refreshing every {interval:g}s  ·  ctrl-c to stop")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
