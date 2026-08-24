"""Automatic continuation.

The decision rules were written against real session endings from this
machine, and the ones quoted below are verbatim. That matters more than usual
here: the failure mode is not a crash, it is a session that carries on when it
should have asked, or asks when it should have carried on.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from swarm import keepgoing as kg


# --- what makes a session carry on -----------------------------------------

@pytest.mark.parametrize("ending", [
    # Real: the dominant shape, a plan announced and then a stop. This is
    # exactly the moment someone types "keep going".
    'It claims a view-only constraint. Let me verify that\'s actually enforced.',
    "I'll check the other three call sites next.",
    "Next I look at the remaining screens.",
    "That leaves the parser tests still to write.",
])
def test_an_announced_plan_carries_on(ending):
    assert kg.decide(ending).keep_going, ending


def test_a_message_ending_mid_thought_carries_on():
    """Real ending: the list after the colon never arrived."""
    ending = "Two real app-wide violations, both in the web shell:"
    decision = kg.decide(ending)
    assert decision.keep_going
    assert "colon" in decision.reason


# --- what hands back --------------------------------------------------------

@pytest.mark.parametrize("ending", [
    # Real: a genuine either/or handed to a person on purpose.
    "Want me to look at what a platform would take, or dig into the design work?",
    # Real: reads like a plan, is actually a request for direction.
    "Verification is done and I've fixed a real bug. Let me check with you on what's next.",
    "Which would you rather I do first?",
    "Should I keep the old behaviour behind a flag?",
])
def test_a_question_hands_back(ending):
    decision = kg.decide(ending)
    assert not decision.keep_going, ending
    assert "asked you" in decision.reason


def test_asking_is_checked_before_intent():
    """"Let me check with you" is a request, not a plan.

    Both patterns match it; the order is what makes the answer right.
    """
    decision = kg.decide("Let me check with you on which approach you prefer.")
    assert not decision.keep_going


def test_a_finished_session_hands_back():
    assert not kg.decide("All 201 tests pass and the branch is pushed.").keep_going


def test_an_empty_message_hands_back():
    for text in ("", "   ", None):
        assert not kg.decide(text).keep_going


# --- the production gate ----------------------------------------------------

def test_remaining_work_touching_production_hands_back():
    ending = "Tests pass. Next steps: deploy the new build to production."
    decision = kg.decide(ending)
    assert not decision.keep_going
    assert "remaining work" in decision.reason


def test_the_gate_errs_toward_refusing():
    """"Migration path for the remaining screens" is a UI refactor.

    The gate reads "migration" and hands back anyway, because it cannot tell a
    screen migration from a database one. That is the trade taken deliberately
    everywhere this gate is used: a needless hand-back costs one "keep going",
    a wrong pass costs whatever it touched.
    """
    decision = kg.decide("Next I look at the migration path for the remaining screens.")
    assert not decision.keep_going
    assert "runs a migration" in decision.reason


def test_a_summary_mentioning_a_deploy_still_carries_on():
    """The gate reads what is NEXT, not what already happened.

    Screening the whole message handed back four of ten real sessions on
    words like "deploy" and "migration" appearing in a description of work
    already done, or deliberately not done. A summary is not a plan.
    """
    ending = (
        "I deployed nothing and ran no migration -- both were out of scope, "
        "and I checked the production config only to confirm it was untouched. "
        "Let me finish the parser tests."
    )
    assert kg.decide(ending).keep_going


# --- the runaway backstop ---------------------------------------------------

def test_the_backstop_hands_back_eventually():
    """Not a budget -- the gate is the stop rule.

    This exists because a hook that never declines has no other exit.
    """
    ending = "Let me finish the remaining tests."
    assert kg.decide(ending, continues_so_far=kg.RUNAWAY_LIMIT - 1).keep_going
    stopped = kg.decide(ending, continues_so_far=kg.RUNAWAY_LIMIT)
    assert not stopped.keep_going
    assert "consecutive continues" in stopped.reason


# --- arming -----------------------------------------------------------------

def test_arming_is_per_repository(swarm_home, tmp_path):
    repo = tmp_path / "mobile-app"
    other = tmp_path / "elsewhere"
    repo.mkdir(); other.mkdir()

    assert not kg.is_armed(repo)
    kg.arm(repo)
    assert kg.is_armed(repo)
    # Nothing else changes behaviour, which is the point of opting in.
    assert not kg.is_armed(other)

    kg.disarm(repo)
    assert not kg.is_armed(repo)


def test_a_subdirectory_of_an_armed_repo_is_armed(swarm_home, tmp_path):
    repo = tmp_path / "mobile-app"
    (repo / "packages" / "web").mkdir(parents=True)
    kg.arm(repo)
    # A session started one level down is still that repository's session.
    assert kg.is_armed(repo / "packages" / "web")


def test_a_sibling_with_a_shared_prefix_is_not_armed(swarm_home, tmp_path):
    (tmp_path / "team").mkdir()
    (tmp_path / "mobile-app").mkdir()
    kg.arm(tmp_path / "team")
    # Naive prefix matching would arm `mobile-app` too.
    assert not kg.is_armed(tmp_path / "mobile-app")


# --- the consecutive counter ------------------------------------------------

def test_the_counter_is_consecutive_not_cumulative(swarm_home):
    kg.note_continue("s1")
    kg.note_continue("s1")
    assert kg.continues_for("s1") == 2
    # Allowed to stop: the streak is over. Carrying the total forward would
    # eventually hand back a healthy session for no reason.
    kg.reset_continues("s1")
    assert kg.continues_for("s1") == 0


def test_counters_are_per_session(swarm_home):
    kg.note_continue("s1")
    assert kg.continues_for("s2") == 0


# --- the hook's exit-code contract ------------------------------------------

def _run_hook(payload: dict, home: Path):
    return subprocess.run(
        [sys.executable, "-c",
         "from swarm.keepgoing_hook import main; main()"],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"SWARM_HOME": str(home), "PYTHONPATH": str(Path.cwd() / "src"),
             "PATH": "/usr/bin:/bin"},
    )


def _transcript(home: Path, session: str, text: str) -> Path:
    path = home / "projects" / "-proj" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-08-21T01:00:00Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }) + "\n")
    return path


def test_the_hook_exits_zero_when_the_repo_is_not_armed(swarm_home, tmp_path):
    path = _transcript(swarm_home, "s1", "Let me finish the remaining tests.")
    out = _run_hook({"session_id": "s1", "cwd": str(tmp_path),
                     "transcript_path": str(path)}, swarm_home)
    assert out.returncode == 0
    # Silence matters: on exit 2 stderr becomes an instruction.
    assert out.stderr.strip() == ""


def test_the_hook_blocks_the_stop_when_armed_and_work_remains(swarm_home, tmp_path):
    kg.arm(tmp_path)
    path = _transcript(swarm_home, "s1", "Let me finish the remaining parser tests.")
    out = _run_hook({"session_id": "s1", "cwd": str(tmp_path),
                     "transcript_path": str(path)}, swarm_home)
    assert out.returncode == 2
    assert "Keep going" in out.stderr


def test_the_hook_honours_the_recursion_guard(swarm_home, tmp_path):
    """Ignoring stop_hook_active is how this becomes an infinite loop."""
    kg.arm(tmp_path)
    path = _transcript(swarm_home, "s1", "Let me finish the remaining tests.")
    out = _run_hook({"session_id": "s1", "cwd": str(tmp_path),
                     "transcript_path": str(path), "stop_hook_active": True},
                    swarm_home)
    assert out.returncode == 0
    assert out.stderr.strip() == ""


def test_the_hook_survives_a_malformed_payload(swarm_home, tmp_path):
    """A traceback on stderr with exit 2 would be injected as an instruction.

    Every failure path exits 0; the only route to 2 is a decision.
    """
    out = subprocess.run(
        [sys.executable, "-c", "from swarm.keepgoing_hook import main; main()"],
        input="not json at all", capture_output=True, text=True,
        env={"SWARM_HOME": str(swarm_home), "PYTHONPATH": str(Path.cwd() / "src"),
             "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0
    assert "Traceback" not in out.stderr


def test_the_hook_records_every_decision(swarm_home, tmp_path):
    from swarm import nightshift as ns

    kg.arm(tmp_path)
    path = _transcript(swarm_home, "s1", "Let me finish the remaining tests.")
    _run_hook({"session_id": "s1", "cwd": str(tmp_path),
               "transcript_path": str(path)}, swarm_home)
    entries = [e for e in ns.read_ledger() if e.get("event") == "keepgoing"]
    # A hook that silently changes when a session stops cannot be debugged.
    assert entries and entries[-1]["kept_going"] is True
    assert entries[-1]["reason"]
