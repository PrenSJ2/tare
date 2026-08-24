"""Turn a hook payload into a stream record.

Pure: payload in, record out, no I/O. This is the only place that decides what
is recorded, which makes the metadata-only guarantee testable in one file.

An allowlist, never a denylist. Fields are copied out by name, so anything not
named here cannot leak -- including fields Claude Code adds in future versions.
That matters concretely: SubagentStop carries last_assistant_message (the
subagent's output text), agent_transcript_path, and cwd, none of which may ever
reach the stream.

Several fields this project wanted do not exist in any payload -- tokens,
duration, parent agent id, model, tool-use count. See docs/hook-payloads.md.
"""

from datetime import datetime, timezone

_EVENTS = {
    "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop",
    "TaskCreated": "task_created",
    "TaskCompleted": "task_completed",
    "SessionEnd": "session_end",
}

# The five fields this module ever copies out (session, agent_id, agent_type,
# task_id, reason) are expected to be scalar. agent_type has already changed
# shape once in practice; nothing stops Claude Code from doing it again. A
# dict or list is rejected outright rather than stringified -- str(value) on
# a dict would still print its nested keys and values verbatim, which is
# exactly the leak the allowlist exists to prevent. 256 chars is ample for an
# id or a type name and bounds how much of a surprising value can land.
_SCALAR_TYPES = (str, int, float, bool)
_MAX_LEN = 256


def _get(payload, name):
    """Value for a key, or None. Tolerates a payload that is not a dict.

    Coerces to a length-capped string. Anything that is not a plain scalar
    (a dict or a list, most plausibly) is dropped as None rather than
    serialised -- see _SCALAR_TYPES above.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get(name)
    if value is None or not isinstance(value, _SCALAR_TYPES):
        return None
    text = str(value)[:_MAX_LEN]
    return text if text else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def project(event: str, payload) -> dict | None:
    """Project a hook payload onto a stream record, or None if not recorded."""
    kind = _EVENTS.get(event)
    if kind is None:
        return None

    rec = {
        "ts": _now(),
        "session": _get(payload, "session_id"),
        "event": kind,
    }

    if kind in ("subagent_start", "subagent_stop"):
        rec["agent_id"] = _get(payload, "agent_id")
        rec["agent_type"] = _get(payload, "agent_type")

    if kind in ("task_created", "task_completed"):
        # task_subject and task_description are authored text that may echo
        # user content, so only the id is recorded.
        rec["task_id"] = _get(payload, "task_id")

    if kind == "session_end":
        # Open enum: bypass_permissions_disabled was removed in 2.1.234 but may
        # appear on earlier builds.
        rec["reason"] = _get(payload, "reason")

    return rec
