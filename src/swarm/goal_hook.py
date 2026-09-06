"""`swarm-goal`: tell a session, and every subagent it spawns, what the goal is.

`keepgoing --goal` decides when a session may stop. That decision is invisible
from inside the session: the model discovers the goal only when it tries to
stop and is told to carry on. So it works toward something it was never told,
and a subagent it spawns knows even less -- a `SessionStart` hook does not fire
for one.

This closes that. Both `SessionStart` and `SubagentStart` accept
`hookSpecificOutput.additionalContext`, and for `SubagentStart` that context
lands in the subagent. Registering the same entrypoint on both means the goal
is stated up front, once, everywhere it matters.

## Why this is not the recording hook

`swarm-hook`'s contract is that it writes nothing, ever, so that this project's
failure can never become the operator's. This hook must speak, so it gets its
own entrypoint -- the same reason `swarm-keepgoing` has one. A hook that can
talk has no business handling the events that must not.

It is still silent by default. No armed goal, an unreadable state file, a
payload with no `cwd`, any exception at all: exit 0 and say nothing, which
leaves the session exactly as it would have been.
"""

from __future__ import annotations

import json
import sys

# Kept short deliberately. This is prepended to every session and every
# subagent in an armed repository, so it competes for the same context the
# work needs. It states the goal, how completion is decided, and nothing else.
CONTEXT = (
    "Goal for this repository, set with `swarm keepgoing on --goal`:\n\n"
    "  {goal}\n"
)

CONTEXT_UNTIL = (
    "\nIt is considered reached when this command exits 0:\n\n"
    "  {until}\n\n"
    "Sessions here are kept going until it passes, so work toward it rather "
    "than stopping to ask whether to continue.\n"
)

CONTEXT_NO_CHECK = (
    "\nThere is no completion check, so nothing can confirm the goal is "
    "reached -- say plainly what is and is not done rather than implying it "
    "is finished.\n"
)


def build_context(goal: str, until: str) -> str:
    body = CONTEXT.format(goal=goal)
    return body + (CONTEXT_UNTIL.format(until=until) if until else CONTEXT_NO_CHECK)


def main() -> None:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else "SessionStart"
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            sys.exit(0)

        cwd = payload.get("cwd")
        if not cwd:
            sys.exit(0)

        from pathlib import Path  # noqa: PLC0415 - keep hook startup cheap

        from swarm import keepgoing

        record = keepgoing.goal_for(Path(cwd))
        if not record:
            sys.exit(0)

        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": build_context(record["goal"], record["until"]),
        }}))
    except SystemExit:
        raise
    except Exception:
        # Silent on every failure. A session that never hears about its goal
        # is worse off than one that does; a session handed a traceback as
        # context is worse off than either.
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
