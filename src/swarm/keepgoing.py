"""Stop asking a session to keep going.

A session finishes a turn, lists what it has not done, and waits. You type
"keep going". It does the next piece, and waits again. The harness knows
everything needed to make that decision -- what was just said, whether
anything is outstanding, whether the next piece is safe -- so it should be
making it.

## How a hook can refuse to let a session stop

Claude Code fires `Stop` when the assistant is about to yield. A hook that
exits **2** blocks the stop and feeds whatever it wrote to stderr back as an
instruction. That is not an invention here: the official `security-guidance`
plugin uses exactly this to force a session to fix what it flagged.

`stop_hook_active` arrives true when a Stop hook is already in flight. Ignoring
it is how you write an infinite loop, so it is the first thing checked.

## Why the whole final message, and no model call

`Stop` runs in the operator's session while they wait. `nightshift` can afford
to ask a model what the next step is; this cannot -- that is a 30-90 second
pause on every single turn. So the decision is made from the text already in
hand, with rules that are plain enough to argue with.

The instruction sent back is deliberately not a specific task. The session has
its own context and knows what it was doing; it needs permission to carry on,
which is exactly what the person typing "keep going" is providing. Extracting a
task would mean the regex guessing at a next step it is measurably bad at --
across 56 real session endings, an explicit "Next steps" heading appeared zero
times.

## What makes it stop

The gate, and only the gate: a message that names no outstanding work, or that
names work touching production, hands control back. Both come from
`nightshift`, so a step refused overnight is refused at noon for the same
reason and with the same wording.

There is also a runaway backstop -- a count of consecutive continues per
session, high enough never to be reached in normal work. It is not a budget,
it is the thing that stops a pathological loop when the gate keeps saying yes
forever. Set it to 0 to remove it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import nightshift, paths

# Consecutive continues before a session is handed back regardless. Not a
# budget: normal work stops long before this because the gate stops it. This
# exists because a hook that never declines has no other exit.
RUNAWAY_LIMIT = 25

# How many times the SAME check failure may repeat before the session is handed
# back. This is the backstop `RUNAWAY_LIMIT` is bad at: a mistyped `--until`
# command can never pass, and waiting 25 turns to notice wastes an evening. A
# check whose output is changing is work in progress; one whose output is
# byte-identical five times running is a loop.
#
# Deliberately compared on the output rather than on the exit code alone: a
# test suite going 7 failures -> 3 -> 1 is progressing, and all three of those
# are exit 1.
SAME_FAILURE_LIMIT = 5

# How long `--until` may take. Long enough for a real test suite, short enough
# that a command which hangs does not hang the operator's Stop hook with it.
CHECK_TIMEOUT_SECONDS = 120

# How much of a failing check's output is fed back. The tail, not the head:
# the assertion is at the bottom of a pytest run, not the top.
CHECK_OUTPUT_WINDOW = 2000

# How much of the message after the first "still outstanding" marker counts as
# a description of the remaining work. Long enough to cover a paragraph, short
# enough that an unrelated closing note does not veto the whole turn.
REMAINING_WORK_WINDOW = 700

# The instruction handed back on a block. Short on purpose -- it is prepended
# to a session that already holds the full context of its own work.
CONTINUE_INSTRUCTION = (
    "Keep going with the work you just described. Do not stop to ask whether "
    "to continue; if something is genuinely ambiguous, take the smallest "
    "defensible option and say what you assumed. Stop and hand back only if "
    "the next step would deploy, release, migrate, push, or touch credentials."
)

# Phrases that mean work is outstanding. Checked against the whole final
# message, since real endings are narrative rather than a bullet list:
# "Two things I'd carry forward", "#383 is open and unmerged", "Still
# outstanding". Missing one costs a "keep going"; a false positive costs a
# turn, so this leans toward missing.
_OUTSTANDING = (
    # A stated intention to do the next thing. This is the dominant real
    # shape and the original patterns missed it: on the mobile-app sessions
    # the endings were "Let me verify that's actually enforced." and "I'll
    # check the other three" -- a plan announced, and then a stop. That is
    # precisely the moment someone types "keep going".
    r"\blet me \w+", r"\bi'?ll \w+", r"\bi am going to\b", r"\bi'?m going to\b",
    r"\bnext i\b", r"\bthen i\b", r"\bnext up\b", r"\bafter that\b",
    # Explicitly named leftovers.
    r"\bnext steps?\b", r"\bstill (to|need|outstanding|open|left)\b",
    r"\bremaining\b", r"\bnot (yet|done|finished|implemented|covered)\b",
    r"\bcarry forward\b", r"\bfollow[- ]ups?\b", r"\boutstanding\b",
    r"\bleft to do\b", r"\bto ?do\b", r"\bunmerged\b", r"\bunfinished\b",
)

# A question aimed at the operator. Continuing past one makes the session
# guess at a decision that was handed to a human on purpose.
_ASKING = (
    r"\bwhich (would|do) you\b", r"\bdo you want\b", r"\bwould you (like|rather|prefer)\b",
    r"\bshould i\b", r"\blet me know\b", r"\byour call\b", r"\bup to you\b",
    r"\bconfirm before\b", r"\bwant me to\b",
    # Checked BEFORE the intention patterns above, which is what stops
    # "Let me check with you on what's next" reading as a plan to carry out.
    r"\bcheck with you\b", r"\btell me (which|whether|if)\b",
    r"\bwhat would you\b", r"\bhappy to (do|take|go)\b",
)


@dataclass
class Decision:
    """Whether to block the stop, and the reason either way."""
    keep_going: bool
    reason: str
    instruction: str = ""


def _matches(patterns, text: str) -> str | None:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            return found.group(0)
    return None


def decide(final_message: str, *, continues_so_far: int = 0) -> Decision:
    """Should this session be allowed to stop?

    Pure, so the rules can be argued with in a test rather than mid-turn.
    Every path that lets the session stop says why, because "it stopped" and
    "it decided to stop" look identical from the outside.
    """
    text = (final_message or "").strip()
    if not text:
        return Decision(False, "nothing was said to act on")

    if RUNAWAY_LIMIT and continues_so_far >= RUNAWAY_LIMIT:
        return Decision(False, f"{continues_so_far} consecutive continues -- handing back")

    lowered = text.lower()

    asked = _matches(_ASKING, lowered)
    if asked:
        # A question is a decision handed over deliberately. Continuing past
        # it does not answer it, it just guesses.
        return Decision(False, f"it asked you something ({asked!r})")

    outstanding = re.search("|".join(_OUTSTANDING), lowered)
    if not outstanding and text.rstrip().endswith(":"):
        # A message ending on a colon is mid-sentence. One real ending was
        # "Two real app-wide violations, both in the web shell:" -- the list
        # that follows a colon never arrived.
        return Decision(True, "the message ends mid-thought, on a colon",
                        instruction=CONTINUE_INSTRUCTION)
    if not outstanding:
        return Decision(False, "nothing outstanding named")

    # The production check runs on what comes NEXT, not on the whole message.
    #
    # Measured on this machine first: screening the entire final message
    # handed back four of ten real sessions on words like "deploy" and
    # "migration" appearing in a summary of what had just been done, or of
    # what had deliberately NOT been done. A summary is not a plan, and a
    # gate that fires on the past declines nearly everything.
    #
    # nightshift screens a short extracted recommendation, which is the right
    # granularity; the equivalent here is the tail from the point the message
    # starts describing what is left.
    ahead = lowered[outstanding.start():][:REMAINING_WORK_WINDOW]
    hit = nightshift._production_hit(ahead)
    if hit:
        return Decision(False, f"the remaining work {hit[1]}")

    return Decision(True, f"outstanding work ({outstanding.group(0)!r})",
                    instruction=CONTINUE_INSTRUCTION)


# ---------------------------------------------------------------------------
# Goals, and the check that answers them
# ---------------------------------------------------------------------------
#
# Why the completion test is a COMMAND and not a model call.
#
# This hook runs inside the operator's session while they wait for it. The
# module docstring already refuses a model call here for that reason -- 30-90
# seconds on every single turn. That constraint turns out to be a gift rather
# than a limitation: "has the goal been reached" answered by a shell exit code
# is instant, deterministic, and arguable, and it hands the next turn the
# actual failure output instead of a paraphrase of it.
#
# What it cannot do is judge a goal that has no test. `--until` is therefore
# optional, and without it a goal only sharpens the instruction -- the stop
# decision falls back to reading the session's own prose, exactly as before.
# A goal you cannot test is a goal this cannot tell you has been reached, and
# saying so is better than implying otherwise.

GOAL_INSTRUCTION = (
    "Keep working toward this goal. Do not stop to ask whether to continue; "
    "if something is genuinely ambiguous, take the smallest defensible option "
    "and say what you assumed. Stop and hand back only if the next step would "
    "deploy, release, migrate, push, or touch credentials.\n\nGOAL: {goal}\n"
)

CHECK_FAILED_SUFFIX = (
    "\nThe completion check `{until}` still fails. Its output ends:\n\n"
    "```\n{output}\n```\n"
)

# A check can fail silently -- `test -f done.txt` prints nothing. An empty
# fenced block reads as though the output were lost, so say what happened.
CHECK_FAILED_QUIET = (
    "\nThe completion check `{until}` still fails, and printed nothing.\n"
)


@dataclass
class GoalCheck:
    """The result of running a goal's `--until` command."""
    ran: bool
    met: bool
    output: str = ""
    error: str = ""
    until: str = ""

    @property
    def digest(self) -> str:
        """What "the same failure again" means. Whitespace-normalised so a
        timing line that differs by milliseconds does not read as progress."""
        return " ".join(self.output.split())


def run_check(until: str, cwd: Path, *,
              timeout: int = CHECK_TIMEOUT_SECONDS) -> GoalCheck:
    """Run the completion check. Never raises.

    A check that cannot run is NOT a failed check -- it is an unanswerable
    question, and `decide_goal` hands the session back rather than looping on
    it. Blocking a stop forever on a mistyped command would be the worst
    behaviour available here: the operator sits watching a session that cannot
    finish and cannot say why.
    """
    import subprocess  # noqa: PLC0415 - keep hook startup cheap

    try:
        done = subprocess.run(until, shell=True, cwd=str(cwd), text=True,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return GoalCheck(ran=False, met=False, until=until,
                         error=f"the check did not finish within {timeout}s")
    except OSError as exc:
        return GoalCheck(ran=False, met=False, until=until,
                         error=f"the check could not run: {exc}")

    combined = ((done.stdout or "") + (done.stderr or "")).strip()
    return GoalCheck(ran=True, met=done.returncode == 0, until=until,
                     output=combined[-CHECK_OUTPUT_WINDOW:])


def decide_goal(goal: str, check: GoalCheck | None, *,
                continues_so_far: int = 0,
                same_failure_streak: int = 0) -> Decision:
    """Should a session working toward `goal` be allowed to stop?

    Pure, like `decide`. `check` is None when the goal has no `--until`; the
    caller then falls back to `decide` and uses only this function's
    instruction.
    """
    if RUNAWAY_LIMIT and continues_so_far >= RUNAWAY_LIMIT:
        return Decision(False, f"{continues_so_far} consecutive continues -- handing back")

    if check is None:
        return Decision(True, "goal set, no completion check",
                        instruction=GOAL_INSTRUCTION.format(goal=goal))

    if not check.ran:
        return Decision(False, f"the completion check could not answer: {check.error}")

    if check.met:
        return Decision(False, "the goal's completion check passed")

    if SAME_FAILURE_LIMIT and same_failure_streak >= SAME_FAILURE_LIMIT:
        return Decision(
            False,
            f"the completion check failed identically {same_failure_streak} "
            "times -- nothing is changing")

    # The failing output goes back with the instruction. This is the whole
    # advantage of a command over a model call: the next turn starts from the
    # actual assertion rather than from a summary of it.
    tail = (CHECK_FAILED_SUFFIX.format(until=check.until, output=check.output)
            if check.output.strip()
            else CHECK_FAILED_QUIET.format(until=check.until))
    return Decision(True, "the goal's completion check still fails",
                    instruction=GOAL_INSTRUCTION.format(goal=goal) + tail)


# ---------------------------------------------------------------------------
# Which repositories are armed
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return paths.state_dir() / "keepgoing.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1), encoding="utf-8")


def arm(repo: Path, *, goal: str = "", until: str = "") -> None:
    """Arm a repository, optionally toward a goal.

    With no goal this is what it always was: the session is kept going while
    its own final message names outstanding work.

    With a goal, the question changes from "did it say it was finished?" to
    "is it finished?" -- and `until` is what answers that. See `run_check`.
    """
    state = _read_state()
    state.setdefault("repos", {})[str(repo)] = {
        "armed_at": time.time(),
        "goal": (goal or "").strip(),
        "until": (until or "").strip(),
    }
    _write_state(state)


def disarm(repo: Path) -> None:
    state = _read_state()
    state.get("repos", {}).pop(str(repo), None)
    _write_state(state)


def armed_repos() -> list[str]:
    return sorted(_read_state().get("repos", {}))


def is_armed(cwd: Path) -> bool:
    """Armed for this directory, or any parent of it.

    A session started in a subdirectory of an armed repository is still that
    repository's session; matching the exact path only would silently do
    nothing for anyone working one level down.
    """
    return _armed_repo_for(cwd) is not None


def _armed_repo_for(cwd: Path) -> str | None:
    """The armed repo governing `cwd`: the longest match, not the first.

    Longest wins so that arming a subdirectory toward its own goal is not
    silently overridden by an older, broader arming of its parent.
    """
    resolved = str(cwd)
    matches = [repo for repo in armed_repos()
               if resolved == repo or resolved.startswith(repo.rstrip("/") + "/")]
    return max(matches, key=len) if matches else None


def goal_for(cwd: Path) -> dict | None:
    """The goal record governing `cwd`, or None if it is armed without one.

    Returns `{"goal": str, "until": str}`. A repo armed before goals existed
    has neither key, and reads as no goal -- the old behaviour, unchanged.
    """
    repo = _armed_repo_for(cwd)
    if repo is None:
        return None
    record = _read_state().get("repos", {}).get(repo, {})
    goal = (record.get("goal") or "").strip()
    if not goal:
        return None
    return {"goal": goal, "until": (record.get("until") or "").strip()}


# ---------------------------------------------------------------------------
# Consecutive-continue counter, per session
# ---------------------------------------------------------------------------

def note_block(session_id: str, transcript: str | None) -> None:
    """Remember the transcript's size at the moment a stop was blocked.

    The next fire compares against it. Without this the ledger records what the
    hook DECIDED and never what happened — and it reported twenty successful
    continuations on a session where only eight actually resumed. That is the
    same failure as `nightshift` counting exit 0 as work done, one project
    over, and it hid a 40% success rate for four days.
    """
    size = None
    if transcript:
        try:
            size = Path(transcript).stat().st_size
        except OSError:
            size = None
    state = _read_state()
    state.setdefault("pending", {})[session_id] = {"at": time.time(), "size": size}
    _write_state(state)


def resolve_block(session_id: str, transcript: str | None) -> tuple[bool, int] | None:
    """Did the previous block actually restart the session?

    Returns (worked, bytes_written), or None when there was no pending block.
    A blocked stop that produced no new transcript bytes did not continue
    anything, whatever its exit code said.
    """
    state = _read_state()
    pending = state.get("pending", {}).pop(session_id, None)
    if not pending:
        return None
    _write_state(state)
    before = pending.get("size")
    if before is None or not transcript:
        return None
    try:
        after = Path(transcript).stat().st_size
    except OSError:
        return None
    return after > before, after - before


def continues_for(session_id: str) -> int:
    return int(_read_state().get("sessions", {}).get(session_id, 0))


def note_continue(session_id: str) -> int:
    state = _read_state()
    sessions = state.setdefault("sessions", {})
    sessions[session_id] = int(sessions.get(session_id, 0)) + 1
    _write_state(state)
    return sessions[session_id]


def reset_continues(session_id: str) -> None:
    """Called when a session is allowed to stop.

    The count is CONSECUTIVE. A session that continues twice, stops, and later
    continues again has not been looping, and carrying the old total forward
    would eventually hand back a perfectly healthy session for no reason.
    """
    state = _read_state()
    changed = state.get("sessions", {}).pop(session_id, None) is not None
    changed |= state.get("checks", {}).pop(session_id, None) is not None
    if changed:
        _write_state(state)


# ---------------------------------------------------------------------------
# How many times the same check failure has repeated, per session
# ---------------------------------------------------------------------------

def failure_streak(session_id: str) -> int:
    return int(_read_state().get("checks", {}).get(session_id, {}).get("streak", 0))


def note_failure(session_id: str, digest: str) -> int:
    """Record a failing check and return how many times it has now repeated.

    A digest that differs from last time resets the streak to 1: the check is
    saying something new, which is what progress looks like from out here.
    """
    state = _read_state()
    checks = state.setdefault("checks", {})
    previous = checks.get(session_id) or {}
    streak = int(previous.get("streak", 0)) + 1 if previous.get("digest") == digest else 1
    checks[session_id] = {"digest": digest, "streak": streak}
    _write_state(state)
    return streak


def effectiveness() -> dict:
    """How often a blocked stop actually restarted the session.

    Reads outcomes, not decisions. If this sits far below 100%, the hook is
    deciding correctly and Claude Code is not acting on it — a different
    problem from the gate refusing too much, and one that has to be
    distinguishable at a glance rather than inferred four days later.
    """
    from . import nightshift  # noqa: PLC0415 - shared ledger

    rows = [r for r in nightshift.read_ledger() if r.get("event") == "keepgoing-outcome"]
    worked = sum(1 for r in rows if r.get("worked"))
    return {"resolved": len(rows), "worked": worked,
            "rate": round(100 * worked / len(rows)) if rows else None}
