import json
from pathlib import Path

from swarm.project import project

FIXTURES = Path(__file__).parent / "fixtures" / "payloads"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_subagent_start_projects_the_documented_fields():
    rec = project("SubagentStart", load("subagent_start"))
    assert rec["event"] == "subagent_start"
    assert rec["session"] == "abc123"
    assert rec["agent_id"] == "agent-abc123"
    assert rec["agent_type"] == "Explore"
    assert rec["ts"]


def test_subagent_stop_pairs_with_start_on_agent_id():
    rec = project("SubagentStop", load("subagent_stop"))
    assert rec["event"] == "subagent_stop"
    assert rec["agent_id"] == "agent-abc123"
    assert rec["agent_type"] == "Explore"


def test_task_events_carry_only_the_id():
    """task_subject and task_description are authored text; they never land."""
    rec = project("TaskCreated", load("task_created"))
    assert rec["task_id"] == "task-77"
    assert "task_subject" not in rec
    assert "task_description" not in rec


def test_session_end_carries_the_reason():
    assert project("SessionEnd", load("session_end"))["reason"] == "clear"


def test_unknown_event_returns_none():
    assert project("SomethingElse", {"session_id": "s"}) is None


def test_missing_fields_do_not_raise():
    rec = project("SubagentStart", {})
    assert rec is not None
    assert rec["event"] == "subagent_start"
    assert rec["agent_id"] is None
    assert rec["agent_type"] is None


def test_malformed_payload_does_not_raise():
    """A non-dict payload must still yield a record, not an exception."""
    for bad in (None, [], "string", 42):
        rec = project("SubagentStart", bad)
        assert isinstance(rec, dict)
        assert rec["event"] == "subagent_start"
        assert rec["session"] is None


def test_no_content_leaks_from_the_real_stop_payload():
    """The whole safety property, against the actual documented payload.

    SubagentStop really does carry the subagent's output text and two file
    paths; none may reach the stream.
    """
    rec = project("SubagentStop", load("subagent_stop"))
    blob = json.dumps(rec)
    for forbidden in ("last_assistant_message", "Analysis complete",
                      "transcript", "private-client", "/Users/"):
        assert forbidden not in blob, f"{forbidden!r} leaked into a record"


def test_task_authored_text_does_not_leak():
    rec = project("TaskCreated", load("task_created"))
    blob = json.dumps(rec)
    assert "billing" not in blob
    assert "Acme" not in blob


def test_unknown_future_fields_cannot_leak():
    """Allowlist, not denylist: a field Claude Code adds later stays out."""
    rec = project("SubagentStart", {
        "session_id": "s", "agent_id": "a",
        "some_future_field": "SECRET", "user_message": "SECRET",
    })
    assert "SECRET" not in json.dumps(rec)


def test_unicode_in_an_agent_type_survives():
    rec = project("SubagentStart", {"session_id": "s", "agent_type": "réview ✓"})
    assert rec["agent_type"] == "réview ✓"


def test_dict_valued_agent_type_does_not_leak_nested_content():
    """agent_type has already changed shape once. If Claude Code ever sends a
    dict instead of a string, none of its contents may reach the record --
    the metadata-only guarantee rests on these fields staying scalar."""
    rec = project("SubagentStart", {
        "session_id": "s", "agent_id": "a",
        "agent_type": {"name": "Explore", "description": "/Users/you/client-project/secret"},
    })
    assert rec["agent_type"] is None
    blob = json.dumps(rec)
    assert "client-project" not in blob
    assert "secret" not in blob
    assert "description" not in blob


def test_list_valued_field_does_not_leak_nested_content():
    rec = project("SubagentStart", {"session_id": "s", "agent_type": ["Explore", "/Users/you/x"]})
    assert rec["agent_type"] is None


def test_overlong_value_is_capped_at_256_chars():
    rec = project("SubagentStart", {"session_id": "s", "agent_type": "x" * 500})
    assert len(rec["agent_type"]) == 256
