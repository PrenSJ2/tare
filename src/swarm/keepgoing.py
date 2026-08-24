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
    for pattern, why in nightshift._PRODUCTION_PATTERNS:
        found = re.search(pattern, ahead)
        if found:
            return Decision(False, f"the remaining work {why}")

    return Decision(True, f"outstanding work ({outstanding.group(0)!r})",
                    instruction=CONTINUE_INSTRUCTION)


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


def arm(repo: Path) -> None:
    state = _read_state()
    state.setdefault("repos", {})[str(repo)] = {"armed_at": time.time()}
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
    resolved = str(cwd)
    for repo in armed_repos():
        if resolved == repo or resolved.startswith(repo.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Consecutive-continue counter, per session
# ---------------------------------------------------------------------------

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
    if state.get("sessions", {}).pop(session_id, None) is not None:
        _write_state(state)
