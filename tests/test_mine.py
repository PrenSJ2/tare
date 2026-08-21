"""Tests for mine.py: transcripts -> usage + invocation events.

Fixtures write raw JSONL by hand rather than through any real Claude Code
client, since mine.py's job is to survive exactly the malformed, unreadable
and self-authored inputs a real corpus contains.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from tare import db, mine, paths


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            if isinstance(line, str):
                fh.write(line + "\n")
            else:
                fh.write(json.dumps(line) + "\n")


def _skill_use(name, ts="2026-08-18T10:00:00Z"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": name}}
            ]
        },
    }


def _agent_use(name, ts="2026-08-18T10:00:00Z", tool_name="Agent"):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "input": {"subagent_type": name, "prompt": "do a thing"},
                }
            ]
        },
    }


def _user_text(text):
    return {"type": "user", "message": {"content": text}}


def _insert_node(conn, node_id, kind, name):
    conn.execute(
        "INSERT INTO nodes (id, kind, name) VALUES (?, ?, ?)", (node_id, kind, name)
    )
    conn.commit()


def test_mine_matches_skill_and_agent_invocations(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _insert_node(conn, "agent:beta", "agent", "beta")

    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha"), _agent_use("beta")],
    )

    result = mine.mine(conn)

    assert result.transcripts == 1
    assert result.invocations == 2
    assert result.unmatched == 0
    assert result.malformed == 0
    assert result.unreadable == 0
    assert result.excluded == 0

    usage = {r["node_id"]: r for r in conn.execute("SELECT * FROM usage")}
    assert usage["skill:alpha"]["invocations"] == 1
    assert usage["skill:alpha"]["sessions"] == 1
    assert usage["agent:beta"]["invocations"] == 1

    events = list(conn.execute("SELECT * FROM events WHERE kind = 'invocation'"))
    assert len(events) == 2
    assert {e["node_id"] for e in events} == {"skill:alpha", "agent:beta"}


def test_mine_supports_task_tool_name_alias(fake_home):
    conn = db.connect()
    _insert_node(conn, "agent:beta", "agent", "beta")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_agent_use("beta", tool_name="Task")],
    )
    result = mine.mine(conn)
    assert result.invocations == 1
    assert result.unmatched == 0


def test_mine_does_not_insert_usage_row_for_never_invoked_capability(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _insert_node(conn, "skill:unused", "skill", "unused")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha")],
    )

    mine.mine(conn)

    rows = {r["node_id"] for r in conn.execute("SELECT node_id FROM usage")}
    assert rows == {"skill:alpha"}
    # Never-invoked capability: no row at all, not a row with invocations=0.
    assert conn.execute(
        "SELECT * FROM usage WHERE node_id = 'skill:unused'"
    ).fetchone() is None


def test_mine_counts_unmatched_names_without_crashing(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha"), _skill_use("ghost-skill")],
    )

    result = mine.mine(conn)

    assert result.invocations == 2
    assert result.unmatched == 1
    usage_ids = {r["node_id"] for r in conn.execute("SELECT node_id FROM usage")}
    assert usage_ids == {"skill:alpha"}


def test_mine_counts_malformed_json_lines(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [
            json.dumps(_skill_use("alpha")),
            "{this is not valid json",
            "",  # blank line: not malformed, just skipped
        ],
    )

    result = mine.mine(conn)

    assert result.transcripts == 1
    assert result.malformed == 1
    assert result.invocations == 1


def test_mine_ignores_valid_json_that_is_not_an_object(fake_home):
    conn = db.connect()
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        ["42", "[1, 2, 3]", '"just a string"'],
    )
    result = mine.mine(conn)
    # All three lines are valid JSON -- none are malformed -- but none are
    # transcript-entry shaped, so nothing crashes and nothing is invoked.
    assert result.malformed == 0
    assert result.invocations == 0


def test_mine_counts_unreadable_files(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    good = paths.projects_dir() / "proj1" / "good.jsonl"
    bad = paths.projects_dir() / "proj1" / "bad.jsonl"
    _write_jsonl(good, [_skill_use("alpha")])
    _write_jsonl(bad, [_skill_use("alpha")])
    os.chmod(bad, 0o000)

    if os.access(bad, os.R_OK):
        pytest.skip("running with privileges that bypass file permissions")

    try:
        result = mine.mine(conn)
    finally:
        os.chmod(bad, stat.S_IRUSR | stat.S_IWUSR)

    assert result.transcripts == 2
    assert result.unreadable == 1
    # The readable file's invocation still counts -- one bad file doesn't
    # take down the whole run.
    assert result.invocations == 1


def test_mine_excludes_own_tagging_exhaust(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")

    # A harness-own `claude -p` tagging call: opens with the tag prompt
    # signature and never calls a tool.
    _write_jsonl(
        paths.projects_dir() / "proj1" / "tag-call.jsonl",
        [
            _user_text(mine.TAG_PROMPT_SIGNATURE + "\n\nDescribe: some-skill"),
            {
                "type": "assistant",
                "timestamp": "2026-08-18T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "purpose: ..."}]},
            },
        ],
    )
    # A genuine session, counted normally.
    _write_jsonl(
        paths.projects_dir() / "proj1" / "real-session.jsonl",
        [_skill_use("alpha")],
    )

    result = mine.mine(conn)

    assert result.transcripts == 2
    assert result.excluded == 1
    assert result.invocations == 1
    usage_ids = {r["node_id"] for r in conn.execute("SELECT node_id FROM usage")}
    assert usage_ids == {"skill:alpha"}


def test_mine_does_not_exclude_session_with_tool_use_even_if_signature_present(fake_home):
    """Structural check, not a bare substring scan: a real session that
    happens to quote the tag signature but also calls a tool is not excluded."""
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [
            _user_text(mine.TAG_PROMPT_SIGNATURE + " (quoted while debugging mine.py)"),
            _skill_use("alpha"),
        ],
    )

    result = mine.mine(conn)

    assert result.excluded == 0
    assert result.invocations == 1


def test_mine_counts_sessions_and_tracks_last_used(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha", ts="2026-08-01T00:00:00Z")],
    )
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session2.jsonl",
        [_skill_use("alpha", ts="2026-08-18T00:00:00Z")],
    )

    mine.mine(conn)

    row = conn.execute(
        "SELECT * FROM usage WHERE node_id = 'skill:alpha'"
    ).fetchone()
    assert row["invocations"] == 2
    assert row["sessions"] == 2
    assert row["last_used"] == "2026-08-18T00:00:00Z"


def test_mine_walks_subagent_transcripts(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "subagents" / "agent-xyz.jsonl",
        [_skill_use("alpha")],
    )

    result = mine.mine(conn)

    assert result.transcripts == 1
    assert result.invocations == 1


def test_mine_is_a_rebuilt_cache_deleted_and_repopulated(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _insert_node(conn, "skill:gamma", "skill", "gamma")

    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha")],
    )
    mine.mine(conn)
    assert conn.execute(
        "SELECT invocations FROM usage WHERE node_id = 'skill:alpha'"
    ).fetchone()["invocations"] == 1

    # Durable data of a different events kind must survive a re-mine.
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'audit', ?, ?)",
        ("2026-08-18T00:00:00Z", "skill:alpha", "kept"),
    )
    conn.commit()

    # Rewrite the corpus entirely: alpha is gone, gamma now appears twice.
    (paths.projects_dir() / "proj1" / "session1.jsonl").unlink()
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session2.jsonl",
        [_skill_use("gamma"), _skill_use("gamma")],
    )

    result = mine.mine(conn)

    assert result.invocations == 2
    usage_rows = {r["node_id"]: r["invocations"] for r in conn.execute("SELECT * FROM usage")}
    assert usage_rows == {"skill:gamma": 2}  # alpha's stale usage row is gone

    invocation_events = list(
        conn.execute("SELECT * FROM events WHERE kind = 'invocation'")
    )
    assert len(invocation_events) == 2
    assert all(e["node_id"] == "skill:gamma" for e in invocation_events)

    # The durable, non-invocation event is untouched by the rebuild.
    audit_events = list(conn.execute("SELECT * FROM events WHERE kind = 'audit'"))
    assert len(audit_events) == 1


def test_mine_commits_so_data_survives_reconnect(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")
    _write_jsonl(
        paths.projects_dir() / "proj1" / "session1.jsonl",
        [_skill_use("alpha")],
    )
    mine.mine(conn)
    conn.close()

    conn2 = db.connect()
    assert conn2.execute("SELECT COUNT(*) c FROM usage").fetchone()["c"] == 1


def test_mine_result_is_reported_not_only_counted_internally(fake_home):
    """Every dropped-input counter is reachable from a single run, not dead
    code -- this exercises all of them together against one corpus."""
    conn = db.connect()
    _insert_node(conn, "skill:alpha", "skill", "alpha")

    _write_jsonl(
        paths.projects_dir() / "proj1" / "good.jsonl",
        [_skill_use("alpha"), _skill_use("ghost")],
    )
    _write_jsonl(
        paths.projects_dir() / "proj1" / "broken.jsonl",
        ["not json at all"],
    )
    _write_jsonl(
        paths.projects_dir() / "proj1" / "tagcall.jsonl",
        [_user_text(mine.TAG_PROMPT_SIGNATURE)],
    )
    unreadable = paths.projects_dir() / "proj1" / "locked.jsonl"
    _write_jsonl(unreadable, [_skill_use("alpha")])
    os.chmod(unreadable, 0o000)
    if not os.access(unreadable, os.R_OK):
        try:
            result = mine.mine(conn)
        finally:
            os.chmod(unreadable, stat.S_IRUSR | stat.S_IWUSR)
        assert result.transcripts == 4
        assert result.invocations == 2
        assert result.unmatched == 1
        assert result.malformed == 1
        assert result.unreadable == 1
        assert result.excluded == 1
    else:
        os.chmod(unreadable, stat.S_IRUSR | stat.S_IWUSR)
        pytest.skip("running with privileges that bypass file permissions")
