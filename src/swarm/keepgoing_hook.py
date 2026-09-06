"""`swarm-keepgoing`: the Stop hook that decides whether a session may stop.

Contract, and it is stricter than the other hook's. Claude Code reads this
hook's exit code and stderr:

- **exit 0, silent** — the session stops, as it would have anyway.
- **exit 2, reason on stderr** — the stop is blocked and stderr becomes the
  instruction the session continues from.

So an accidental exit 2, or a traceback landing on stderr, does not merely
fail: it injects noise into the operator's session as an instruction. Every
failure path here therefore exits 0. The only route to exit 2 is a decision
that was reached deliberately.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            sys.exit(0)

        # First, always. Claude Code sets this while a Stop hook is in flight,
        # and ignoring it is how this becomes an infinite loop.
        if payload.get("stop_hook_active"):
            sys.exit(0)

        from pathlib import Path  # noqa: PLC0415 - keep hook startup cheap

        from swarm import keepgoing

        cwd = payload.get("cwd")
        session_id = payload.get("session_id") or ""
        if not cwd or not keepgoing.is_armed(Path(cwd)):
            sys.exit(0)

        # The transcript path is in the payload; falling back to a lookup by
        # id covers builds that do not send it.
        transcript = payload.get("transcript_path")
        text = ""
        if transcript and Path(transcript).is_file():
            text = _last_assistant_text(Path(transcript))
        elif session_id:
            text = nightshift_last(session_id)

        # Resolve the PREVIOUS block before deciding anything new: did the
        # session that was blocked last time actually write anything?
        outcome = keepgoing.resolve_block(session_id, transcript)
        if outcome is not None:
            worked, grew = outcome
            _record({"event": "keepgoing-outcome", "session": session_id,
                     "worked": worked, "bytes": grew})

        continues = keepgoing.continues_for(session_id)
        goal = keepgoing.goal_for(Path(cwd))

        if goal is None:
            decision = keepgoing.decide(text, continues_so_far=continues)
        elif goal["until"]:
            # A goal with a completion check does not read the session's prose
            # at all. Whether it SAID it was finished is not the question.
            check = keepgoing.run_check(goal["until"], Path(cwd))
            streak = 0
            if check.ran and not check.met:
                streak = keepgoing.note_failure(session_id, check.digest)
            decision = keepgoing.decide_goal(
                goal["goal"], check, continues_so_far=continues,
                same_failure_streak=streak)
            _record({"event": "keepgoing-check", "session": session_id,
                     "until": goal["until"], "ran": check.ran, "met": check.met,
                     "streak": streak, "reason": decision.reason})
        else:
            # A goal with no check: the stop decision is still the prose one,
            # because nothing here can tell whether the goal is met. Only the
            # instruction improves -- it names the goal instead of saying
            # "carry on with what you just described".
            prose = keepgoing.decide(text, continues_so_far=continues)
            goal_only = keepgoing.decide_goal(goal["goal"], None,
                                              continues_so_far=continues)
            decision = keepgoing.Decision(
                prose.keep_going, prose.reason,
                instruction=goal_only.instruction if prose.keep_going else "")

        if not decision.keep_going:
            keepgoing.reset_continues(session_id)
            _log(session_id, cwd, False, decision.reason)
            sys.exit(0)

        keepgoing.note_continue(session_id)
        keepgoing.note_block(session_id, transcript)
        _log(session_id, cwd, True, decision.reason)
        # stderr IS the instruction on exit 2.
        sys.stderr.write(decision.instruction)
        sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        # Never let this project's failure become the operator's problem.
        sys.exit(0)


def _last_assistant_text(path) -> str:
    from swarm import reader  # noqa: PLC0415

    latest = ""
    for obj in reader._iter_json(path):
        if obj.get("type") != "assistant":
            continue
        chunks = [b.get("text", "") for b in reader._blocks(obj)
                  if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(c for c in chunks if c).strip()
        if joined:
            latest = joined
    return latest


def nightshift_last(session_id: str) -> str:
    from swarm import nightshift  # noqa: PLC0415

    return nightshift.last_assistant_text(session_id)


def _record(entry: dict) -> None:
    try:
        from swarm import nightshift  # noqa: PLC0415

        nightshift.record(entry)
    except Exception:
        pass


def _log(session_id: str, cwd: str, kept_going: bool, reason: str) -> None:
    """Every decision, in the same ledger nightshift writes to.

    A hook that silently changes when your session stops is impossible to
    trust or debug. `swarm keepgoing status` reads this back.
    """
    try:
        from swarm import nightshift  # noqa: PLC0415

        nightshift.record({
            "event": "keepgoing",
            "session": session_id,
            "repo": cwd,
            "kept_going": kept_going,
            "reason": reason,
        })
    except Exception:
        pass


if __name__ == "__main__":
    main()
