"""Validate a stream and report what is missing.

The hooks are silent by design, so this is the only place an operator learns a
record was lost. It reports plainly rather than reassuringly.

Duration is derived here, not recorded: no hook payload carries one, so the gap
between a subagent's start and stop timestamps is the only measure available.
Token spend is not available to Phase A at all -- the render says so.
"""

import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Report:
    path: str = ""
    records: int = 0
    malformed: int = 0
    sessions: int = 0
    agents: int = 0
    paired_agents: int = 0
    unmatched_starts: int = 0
    orphan_stops: int = 0
    reused_agent_ids: int = 0
    negative_durations: int = 0
    total_duration_ms: int = 0
    agent_types: Counter = field(default_factory=Counter)
    events: Counter = field(default_factory=Counter)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def inspect(path: Path) -> Report:
    rep = Report(path=str(path))
    # Lists, not single values: an agent_id can appear more than once in a
    # stream, and a dict would let the second dispatch overwrite the first --
    # losing a duration with nothing reporting the loss, in the one module
    # whose job is to report losses.
    starts: dict[str, list] = defaultdict(list)
    stops: dict[str, list] = defaultdict(list)
    sessions = set()

    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return rep

    with handle as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                rep.malformed += 1
                continue
            if not isinstance(rec, dict):
                rep.malformed += 1
                continue

            rep.records += 1
            rep.events[rec.get("event", "?")] += 1
            if rec.get("session"):
                sessions.add(rec["session"])

            agent_id = rec.get("agent_id")
            event = rec.get("event")
            # Tallied from both kinds: SubagentStart does not fire for most
            # dispatches, so a tally limited to starts would describe the
            # minority of agents that happen to produce one.
            if event in ("subagent_start", "subagent_stop") and rec.get("agent_type"):
                rep.agent_types[rec["agent_type"]] += 1
            if event == "subagent_start" and agent_id:
                starts[agent_id].append(_parse_ts(rec.get("ts")))
            elif event == "subagent_stop" and agent_id:
                stops[agent_id].append(_parse_ts(rec.get("ts")))

    for agent_id in set(starts) | set(stops):
        started_list = starts.get(agent_id, [])
        stopped_list = stops.get(agent_id, [])
        if len(started_list) > 1 or len(stopped_list) > 1:
            rep.reused_agent_ids += 1
        if started_list and stopped_list:
            rep.paired_agents += 1
        # Pair in arrival order; surplus on either side is unmatched.
        for started, stopped in zip(started_list, stopped_list):
            if started is None or stopped is None:
                continue
            try:
                delta = (stopped - started).total_seconds() * 1000
            except TypeError:
                # One naive, one timezone-aware -- they can't be subtracted.
                # Treated the same as an unparseable timestamp: this is a
                # robustness module, so a malformed pair must not raise.
                continue
            if delta >= 0:
                rep.total_duration_ms += int(delta)
            else:
                # A clock adjustment, most likely. Counted rather than
                # discarded: a third silent-discard path would undercut the
                # point of this module.
                rep.negative_durations += 1
        rep.unmatched_starts += max(0, len(started_list) - len(stopped_list))
        # A stop with no matching start is the normal case, not a fault --
        # SubagentStart does not fire for most dispatches (measured: 4 starts
        # against 15 stops on a live stream). Still counted, but reported as
        # an ordinary statistic rather than a Problem; see render().
        rep.orphan_stops += max(0, len(stopped_list) - len(started_list))

    rep.sessions = len(sessions)
    rep.agents = len(set(starts) | set(stops))
    return rep


def check_hook_command() -> list[str]:
    """Does the command install() registered in settings.json still exist?

    install() freezes an absolute path into settings.json. Reinstall swarm
    into a different venv without re-running `swarm install` and the old path
    stops resolving; the hooks then fail (fail open and silent, by design),
    with nothing else telling the operator they stopped running. This is that
    reporting path. Degrades to an empty list on any surprise -- a missing or
    malformed settings.json, an unreadable one -- because a check inside
    `doctor` must never itself crash the reader.
    """
    from swarm import install

    try:
        command = install.registered_command()
    except Exception:
        return []
    if not command:
        return []
    try:
        p = Path(command)
        if not p.exists():
            return [f"registered hook command not found: {command}"]
        if not os.access(p, os.X_OK):
            return [f"registered hook command is not executable: {command}"]
    except Exception:
        return []
    return []


def render(rep: Report, extra_problems: list[str] | None = None) -> str:
    lines = [f"swarm doctor — {rep.path}", "=" * 44, ""]
    lines.append(
        f"{rep.records} records across {rep.sessions} session(s), {rep.agents} agent(s)"
    )
    if rep.events:
        lines.append("  " + ", ".join(f"{k}={v}" for k, v in sorted(rep.events.items())))
    if rep.agent_types:
        lines.append(
            "  agents: "
            + ", ".join(f"{k}×{v}" for k, v in rep.agent_types.most_common())
        )
    if rep.orphan_stops:
        # Normal, not a problem: SubagentStart does not fire for most
        # dispatches. Stated here as an ordinary statistic so the operator
        # sees it without doctor crying wolf over it below.
        lines.append(
            f"  {rep.orphan_stops} stop(s) had no matching start "
            "(normal — SubagentStart doesn't fire for most dispatches)"
        )
    lines.append("")
    if rep.paired_agents == 0:
        lines.append(
            "Wall clock: unavailable — no agent produced both a start and a stop."
        )
        lines.append(
            "  SubagentStart fires for only a minority of dispatches, so this is"
        )
        lines.append("  common and does not indicate a fault.")
    else:
        lines.append(
            f"Wall clock: {rep.total_duration_ms / 1000:.0f}s across "
            f"{rep.paired_agents} of {rep.agents} agent(s) that produced both "
            "records — the true total for this session is unknown."
        )
    lines.append("Tokens: not captured — no hook payload carries usage.")
    lines.append("  Spend is resolved from agent_id by the reader, not by this stream.")

    problems = []
    if rep.malformed:
        problems.append(f"{rep.malformed} malformed line(s) skipped")
    if rep.unmatched_starts:
        problems.append(
            f"{rep.unmatched_starts} unmatched start(s) — still running, or the stop was lost"
        )
    if rep.reused_agent_ids:
        problems.append(
            f"{rep.reused_agent_ids} agent id(s) seen more than once — "
            "durations are paired in arrival order and may be misattributed"
        )
    if rep.negative_durations:
        problems.append(
            f"{rep.negative_durations} pair(s) stopped before they started — "
            "excluded from the total; likely a clock adjustment"
        )
    if extra_problems:
        problems.extend(extra_problems)

    lines.append("")
    if problems:
        lines.append("Problems:")
        lines.extend(f"  {p}" for p in problems)
    else:
        lines.append("No problems found.")
    return "\n".join(lines)
