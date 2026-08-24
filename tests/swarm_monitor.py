"""Monitor tests.

The hardest requirement here is NOT firing. A supervisor that flags a healthy
run gets muted, and a muted supervisor is the same as no supervisor -- so the
quiet cases below matter as much as the loud ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm import monitor
from swarm.reader import AgentRun

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def run(label, secs, *, status="done", started=None):
    start = started or NOW - timedelta(seconds=secs)
    return AgentRun(
        agent_id="a" * 17, label=label, agent_type="general-purpose", model="sonnet",
        dispatched=start, started=start,
        ended=None if status == "running" else start + timedelta(seconds=secs),
        lines=10, status=status,
    )


def healthy_fleet(n=10, secs=140):
    return [run(f"task {i}", secs) for i in range(n)]


def test_a_healthy_run_produces_nothing():
    assert monitor.assess(healthy_fleet(), now=NOW) == []


def test_the_normal_implement_review_fix_loop_is_not_churn():
    """Four dispatches on one subject is the standard loop, not a problem."""
    runs = healthy_fleet()
    runs += [run(f"{verb} Task 42", 140) for verb in ("Implement", "Review", "Fix", "Re-review")]
    assert [f for f in monitor.assess(runs, now=NOW) if f.kind == "churn"] == []


def test_a_subject_past_the_loop_and_costing_real_time_is_churn():
    runs = healthy_fleet()
    runs += [run("Review Task 42", 600) for _ in range(6)]
    churn = [f for f in monitor.assess(runs, now=NOW) if f.kind == "churn"]
    assert len(churn) == 1
    assert "task 42" in churn[0].subject
    assert churn[0].evidence, "a finding without evidence cannot be acted on"


def test_many_quick_retries_are_not_churn():
    """Dispatch count alone is not the signal -- it has to have cost something."""
    runs = healthy_fleet(secs=600)
    runs += [run("Review Task 42", 5) for _ in range(6)]
    assert [f for f in monitor.assess(runs, now=NOW) if f.kind == "churn"] == []


def test_an_agent_running_far_past_the_median_is_flagged():
    runs = healthy_fleet()
    runs.append(run("Task 99", 0, status="running", started=NOW - timedelta(seconds=3000)))
    stuck = [f for f in monitor.assess(runs, now=NOW) if f.kind == "stuck"]
    assert len(stuck) == 1
    assert "median" in " ".join(stuck[0].evidence)


def test_a_running_agent_within_normal_range_is_not_flagged():
    runs = healthy_fleet()
    runs.append(run("Task 99", 0, status="running", started=NOW - timedelta(seconds=200)))
    assert [f for f in monitor.assess(runs, now=NOW) if f.kind == "stuck"] == []


def test_no_baseline_means_no_slow_flag():
    """With too few finished agents the median is meaningless; say nothing
    rather than invent a threshold."""
    runs = [run("Task 1", 0, status="running", started=NOW - timedelta(seconds=9000))]
    assert [f for f in monitor.assess(runs, now=NOW) if f.kind == "stuck"] == []


def test_a_dispatch_that_produced_no_transcript_is_reported():
    runs = healthy_fleet()
    runs.append(AgentRun("b" * 17, "Ghost", "general-purpose", "haiku", NOW, None, None, 0, "unknown"))
    lost = [f for f in monitor.assess(runs, now=NOW) if f.kind == "lost"]
    assert len(lost) == 1 and "Ghost" in " ".join(lost[0].evidence)


def test_a_goal_with_nothing_running_is_reported_as_idle():
    findings = monitor.assess(healthy_fleet(), now=NOW, goal="ship it")
    assert [f for f in findings if f.kind == "idle"]


def test_a_goal_with_work_in_flight_is_not_idle():
    runs = healthy_fleet()
    runs.append(run("Task 99", 0, status="running", started=NOW - timedelta(seconds=60)))
    findings = monitor.assess(runs, now=NOW, goal="ship it")
    assert [f for f in findings if f.kind == "idle"] == []


def test_findings_that_need_action_sort_first():
    runs = healthy_fleet()
    runs.append(AgentRun("b" * 17, "Ghost", "general-purpose", "haiku", NOW, None, None, 0, "unknown"))
    runs += [run("Review Task 42", 600) for _ in range(6)]
    findings = monitor.assess(runs, now=NOW)
    assert findings[0].severity == "act"


def test_every_finding_carries_an_action_it_would_take():
    """The acting interface is defined even though nothing is dispatched yet."""
    runs = healthy_fleet()
    runs += [run("Review Task 42", 600) for _ in range(6)]
    for finding in monitor.assess(runs, now=NOW):
        assert finding.action, f"{finding.kind} has no stated action"


def test_assess_dispatches_nothing():
    """Guard the boundary: this module reports, it does not act."""
    source = (monitor.__file__)
    text = open(source, encoding="utf-8").read()
    for forbidden in ("subprocess", "Popen", "os.system"):
        assert forbidden not in text, f"monitor must not be able to execute anything ({forbidden})"


def test_render_says_nothing_needs_attention_when_clean():
    assert "nothing needs attention" in monitor.render([])


def test_render_never_hides_evidence_silently():
    runs = healthy_fleet()
    runs += [run("Review Task 42", 600) for _ in range(8)]
    out = monitor.render(monitor.assess(runs, now=NOW))
    assert "more" in out  # the omitted evidence is counted, not dropped


def test_subject_collapses_the_loops_verbs():
    for label in ("Implement Task 5", "Review Task 5", "Re-review Task 5 fix", "Task 5"):
        assert monitor.subject_of(label) == "task 5"
