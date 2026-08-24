"""Watch a run against a goal and say what needs attention, with evidence.

## Why this only reports, for now

The design for this piece was "holds a goal, watches the stream, acts within a
budget, escalates with evidence" -- and acting means re-dispatching and
re-scoping other agents. The same design argued for building the *view* first
so a monitor's rules could be calibrated against real runs before being let
loose, and that argument got a lot stronger the day a subagent deleted a
repository.

So `assess()` is a pure function from runs to findings, every finding carries
the evidence it was derived from and the action it would take, and nothing is
dispatched. Run it over real sessions, check the findings against what actually
needed intervention, and only then wire the actions up. `Finding.action` is the
interface that will carry them.

## Thresholds are relative, not absolute

Calibrated on a real 106-agent session: median run 140s, p90 749s, longest
3178s -- 22x the median. An absolute "flag anything over 10 minutes" would fire
23 times on a healthy run of that shape and never fire at all on a fleet of
quick agents. Everything below is a multiple of the run's own median.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from .reader import AgentRun

# A run this many times the median is worth interrupting for. 3x fires on ~20%
# of a healthy run's agents, 5x on ~11%; the flag is only raised for agents
# STILL RUNNING, which is what makes it rare in practice.
SLOW_MULTIPLE = 5.0

# implement -> review -> fix -> re-review is the normal loop, so four
# dispatches on one subject is healthy. Calibrated on a real 106-agent
# session: 50 of 64 subjects took a single dispatch, and every subject that
# went past four was one that genuinely struggled. Flagging at three fired
# eight times on a run that converged fine -- a monitor that cries wolf on a
# healthy run gets muted, which is the same as not having one.
HEALTHY_TOUCHES = 4

# ...and it has to have cost something. Dispatch count alone flags a subject
# that was retried four times in ninety seconds, which is not a problem.
CHURN_TIME_MULTIPLE = 2.0

# Below this many finished agents the median is not a meaningful baseline.
MIN_SAMPLE = 5

_STRIP = re.compile(
    r"^(implement|review|re-review|fix|rebuild|retry)\s+|\s+(round\s*\d+|fix)\s*$",
    re.I,
)


@dataclass
class Finding:
    kind: str          # "stuck" | "churn" | "lost" | "idle"
    severity: str      # "warn" | "act"
    subject: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    action: str | None = None   # what an acting monitor would do; not performed


def subject_of(label: str) -> str:
    """Collapse 'Implement Task 5' / 'Review Task 5' / 'Task 5 fix round 2'
    onto one subject, so repeated work on the same thing is countable."""
    text = _STRIP.sub("", label or "").strip().lower()
    return re.sub(r"\s+", " ", text) or "(unlabelled)"


def _elapsed(run: AgentRun, now: datetime) -> float | None:
    if run.status == "running" and run.started:
        return (now - run.started).total_seconds()
    return run.seconds


def assess(runs: list[AgentRun], *, now: datetime, goal: str | None = None) -> list[Finding]:
    """Everything about this run that a supervisor should look at."""
    findings: list[Finding] = []
    finished = [r for r in runs if r.seconds is not None]
    baseline = statistics.median([r.seconds for r in finished]) if len(finished) >= MIN_SAMPLE else None

    # 1. Still running, far past what this run's agents normally take.
    if baseline:
        for run in runs:
            if run.status != "running":
                continue
            secs = _elapsed(run, now) or 0
            if secs > baseline * SLOW_MULTIPLE:
                findings.append(Finding(
                    kind="stuck",
                    severity="act",
                    subject=run.label,
                    detail=f"running {secs / 60:.0f} min, {secs / baseline:.0f}x this run's median",
                    evidence=[
                        f"agent {run.agent_id} started {run.started:%H:%M:%S}",
                        f"median of {len(finished)} finished agents: {baseline:.0f}s",
                        f"{run.lines} transcript lines so far",
                    ],
                    action="ask the agent for a status line; if it cannot answer, stop and re-dispatch "
                           "with a narrower task",
                ))

    # 2. The same subject worked repeatedly -- the loop is not converging.
    touches: dict[str, list[AgentRun]] = {}
    for run in runs:
        touches.setdefault(subject_of(run.label), []).append(run)
    per_subject = [sum(r.seconds or 0 for r in g) for g in touches.values()]
    typical_subject = statistics.median(per_subject) if per_subject else 0.0
    for subject, group in sorted(touches.items()):
        spent = sum(r.seconds or 0 for r in group)
        if len(group) <= HEALTHY_TOUCHES:
            continue
        if typical_subject and spent < typical_subject * CHURN_TIME_MULTIPLE:
            continue
        findings.append(Finding(
            kind="churn",
            severity="act",
            subject=subject,
            detail=f"{len(group)} dispatches, {spent / 60:.0f} min total -- past the "
                   f"{HEALTHY_TOUCHES}-dispatch implement/review/fix loop",
            evidence=[f"{r.label} ({(r.seconds or 0):.0f}s)" for r in group],
            action="stop re-dispatching; the task or its brief is wrong, escalate with the "
                   "findings that keep recurring",
        ))

    # 3. Dispatched but nothing to show for it.
    lost = [r for r in runs if r.status == "unknown"]
    if lost:
        findings.append(Finding(
            kind="lost",
            severity="warn",
            subject=f"{len(lost)} dispatch(es) with no transcript",
            detail="dispatched but never wrote anything -- died at launch, or the transcript is gone",
            evidence=[f"{r.label} ({r.agent_id})" for r in lost[:5]],
            action="re-dispatch; nothing was produced, so nothing is lost by retrying",
        ))

    # 4. A goal was set and nothing is moving toward it.
    if goal and not [r for r in runs if r.status == "running"]:
        findings.append(Finding(
            kind="idle",
            severity="warn",
            subject=goal,
            detail="no agent is running -- the goal is either met or stalled",
            evidence=[f"{len(runs)} agents dispatched, none active"],
            action="confirm the goal is met; if not, dispatch the next step",
        ))

    order = {"act": 0, "warn": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), f.kind))
    return findings


def render(findings: list[Finding], *, goal: str | None = None) -> str:
    lines: list[str] = []
    if goal:
        lines.append(f"goal: {goal}")
        lines.append("")
    if not findings:
        lines.append("nothing needs attention")
        return "\n".join(lines)

    acts = sum(1 for f in findings if f.severity == "act")
    lines.append(f"{len(findings)} finding(s), {acts} worth acting on")
    for finding in findings:
        lines.append("")
        mark = "!" if finding.severity == "act" else "·"
        lines.append(f"{mark} [{finding.kind}] {finding.subject}")
        lines.append(f"    {finding.detail}")
        for item in finding.evidence[:4]:
            lines.append(f"      - {item}")
        if len(finding.evidence) > 4:
            lines.append(f"      - … {len(finding.evidence) - 4} more")
        if finding.action:
            lines.append(f"    would: {finding.action}")
    return "\n".join(lines)
