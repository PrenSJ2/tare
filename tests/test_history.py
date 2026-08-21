"""Provenance: what appeared when, and what a session changed.

The load-bearing property here is honesty. Two of these tests exist only to
pin down what the module must NOT claim.
"""

import json
import os
import time

from tare import db, history, mine


def _node(conn, node_id, name, path, plugin=None, state="live"):
    conn.execute(
        "INSERT INTO nodes (id, kind, name, path, origin, provider_plugin, state) "
        "VALUES (?, 'skill', ?, ?, 'user-authored', ?, ?)",
        (node_id, name, str(path), plugin, state),
    )


def _skill(root, name, body="x"):
    d = root / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body)
    return f


def test_fs_name_covers_live_vault_plugin_and_agents():
    # One rule has to hold across all four shapes, or an edit recorded against
    # a capability's old location stops being attributable after a move.
    assert mine._fs_name("/Users/x/.claude/skills/hyperframes-core/SKILL.md") == "hyperframes-core"
    assert mine._fs_name("/Users/x/.claude/skills/hyperframes-core/reference.md") == "hyperframes-core"
    assert mine._fs_name("/Users/x/.claude/vault/skills/learn-codebase/SKILL.md") == "learn-codebase"
    assert mine._fs_name("/Users/x/.claude/plugins/cache/p/1.0/skills/brainstorm/SKILL.md") == "brainstorm"
    assert mine._fs_name("/Users/x/.claude/agents/rust-pro.md") == "rust-pro"
    assert mine._fs_name("/Users/x/Documents/notes.md") is None


def test_ambiguous_names_are_dropped_not_guessed(fake_home):
    conn = db.connect()
    _node(conn, "a", "dup", fake_home / "skills" / "dup" / "SKILL.md")
    _node(conn, "b", "dup", fake_home / "plugins" / "cache" / "p" / "skills" / "dup" / "SKILL.md")
    _node(conn, "c", "solo", fake_home / "skills" / "solo" / "SKILL.md")
    targets = mine._edit_targets(conn)
    # A misattributed edit history is worse than a short one: it reads as fact.
    assert "dup" not in targets
    assert targets["solo"] == "c"


def test_changed_is_none_when_the_file_was_only_written_once(fake_home):
    f = _skill(fake_home, "fresh")
    born, changed = history._times(str(f))
    assert born is not None
    # Writing a file sets birth and mtime microseconds apart. Reporting that
    # as an edit would mark every capability as "evolved" the day it arrived.
    assert changed is None


def test_changed_is_set_after_a_later_write(fake_home):
    f = _skill(fake_home, "edited")
    st = os.stat(f)
    later = time.time() + 60
    os.utime(f, (later, later))
    born, changed = history._times(str(f))
    assert born is not None and changed is not None
    assert changed > born
    assert st.st_mtime < later


def test_missing_file_yields_no_dates_rather_than_raising(fake_home):
    assert history._times(str(fake_home / "skills" / "gone" / "SKILL.md")) == (None, None)
    assert history._times(None) == (None, None)


def test_summary_separates_plugin_capabilities_from_the_operators(fake_home):
    conn = db.connect()
    _node(conn, "mine", "mine", _skill(fake_home, "mine"))
    _node(conn, "theirs", "theirs", _skill(fake_home, "theirs"), plugin="somepack")
    s = history.summary(conn)
    assert s["counts"] == {
        "own": 1, "from_plugins": 1,
        "session_edited": 0, "authored_outside_sessions": 1,
    }
    # A plugin's birth time is when it was cached here, not when it was
    # written, so it must never be interleaved into the operator's timeline.
    assert [e["n"] for e in s["added"]] == ["mine"]
    assert [e["n"] for e in s["installed"]] == ["theirs"]


def test_authored_outside_sessions_counts_the_record_not_the_author(fake_home):
    """The count must not be read as "hand-written", and the module says so.

    Nothing in the data can distinguish "written in an editor" from "written
    in a session whose transcript has since been deleted", so the name and the
    docstring both have to stay about the RECORD.
    """
    conn = db.connect()
    _node(conn, "n1", "untouched", _skill(fake_home, "untouched"))
    assert history.summary(conn)["counts"]["authored_outside_sessions"] == 1
    assert "not necessarily" in history.__doc__


def test_session_edits_attach_to_their_capability(fake_home):
    conn = db.connect()
    _node(conn, "n1", "touched", _skill(fake_home, "touched"))
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'edit', ?, ?)",
        ("2026-08-01T10:00:00Z", "n1",
         json.dumps({"tool": "Edit", "project": "proj", "file": "f", "session": "s"})),
    )
    s = history.summary(conn)
    assert [e["n"] for e in s["session_edited"]] == ["touched"]
    entry = s["session_edited"][0]
    assert entry["edit_count"] == 1
    assert entry["last_edit"] == "2026-08-01T10:00:00Z"
    assert s["counts"]["authored_outside_sessions"] == 0


def test_mining_records_edits_to_capability_files(fake_home):
    conn = db.connect()
    _node(conn, "n1", "target", _skill(fake_home, "target"))
    transcript = fake_home / "projects" / "-proj" / "s.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    edit = {
        "type": "assistant", "timestamp": "2026-08-02T09:00:00Z",
        "message": {"content": [{
            "type": "tool_use", "name": "Edit",
            "input": {"file_path": str(fake_home / "skills" / "target" / "SKILL.md")},
        }]},
    }
    unrelated = {
        "type": "assistant", "timestamp": "2026-08-02T09:01:00Z",
        "message": {"content": [{
            "type": "tool_use", "name": "Edit",
            "input": {"file_path": "/somewhere/else/main.py"},
        }]},
    }
    transcript.write_text(json.dumps(edit) + "\n" + json.dumps(unrelated) + "\n")

    mine.mine(conn)
    rows = conn.execute("SELECT node_id, payload FROM events WHERE kind = 'edit'").fetchall()
    assert len(rows) == 1
    assert rows[0]["node_id"] == "n1"
    assert json.loads(rows[0]["payload"])["tool"] == "Edit"


def test_mining_rebuilds_edits_rather_than_appending(fake_home):
    """`events` of kind 'edit' is a cache, like every other mined table.

    Without the DELETE, every run doubles the history and the counts drift
    upward forever.
    """
    conn = db.connect()
    _node(conn, "n1", "target", _skill(fake_home, "target"))
    transcript = fake_home / "projects" / "-proj" / "s.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-08-02T09:00:00Z",
        "message": {"content": [{
            "type": "tool_use", "name": "Write",
            "input": {"file_path": str(fake_home / "skills" / "target" / "SKILL.md")},
        }]},
    }) + "\n")

    mine.mine(conn)
    mine.mine(conn)
    count = conn.execute("SELECT COUNT(*) c FROM events WHERE kind = 'edit'").fetchone()["c"]
    assert count == 1
