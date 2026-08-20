"""Usage memory tests.

The property that matters most: these rows must survive `mine`, which rebuilds
invocation history from transcripts on every run. If a learning row can be
wiped by a routine command, the feature is a lie.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harness import db, memory, mine

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeResult:
    id: str = "skill:alpha"
    name: str = "alpha"
    state: str = "live"
    score: float = 1.0


def test_a_search_that_found_something_is_recorded_as_a_lookup(fake_home):
    conn = db.connect()
    memory.record_lookup(conn, "make a video", [FakeResult()])
    row = conn.execute("SELECT kind, node_id, payload FROM events").fetchone()
    assert row["kind"] == "lookup"
    assert row["node_id"] == "skill:alpha"
    assert json.loads(row["payload"])["query"] == "make a video"


def test_a_search_that_found_nothing_is_recorded_as_a_miss(fake_home):
    conn = db.connect()
    memory.record_lookup(conn, "quantum compiler", [])
    row = conn.execute("SELECT kind, payload FROM events").fetchone()
    assert row["kind"] == "miss"
    assert json.loads(row["payload"])["query"] == "quantum compiler"


def test_a_low_score_is_NOT_treated_as_a_miss(fake_home):
    """Relevance scores cannot be thresholded here, and an earlier version of
    this module assumed they could.

    Measured against the real index, a bad match (systematic-debugging for
    "quantum error correction", 8.36) scores between two good ones
    (ai-engineer 8.25, content-marketer 8.53). Any cutoff either throws away
    real hits or accepts nonsense, so only an empty result set is a miss and
    the behavioural signal below does the rest.
    """
    conn = db.connect()
    memory.record_lookup(conn, "quantum compiler", [FakeResult(score=0.001)])
    assert conn.execute("SELECT kind FROM events").fetchone()["kind"] == "lookup"


def test_a_query_repeated_without_ever_using_a_result_is_a_gap(fake_home):
    """The honest miss signal: the search looked fine and helped nobody."""
    conn = db.connect()
    for _ in range(3):
        memory.record_lookup(conn, "terraform module review", [FakeResult()])
    gaps = [s for s in memory.suggestions(conn, now=NOW) if s.kind == "gap"]
    assert len(gaps) == 1
    assert "terraform" in gaps[0].subject


def test_a_query_followed_by_an_activation_is_not_a_gap(fake_home):
    """If the search led to using something, it worked."""
    conn = db.connect()
    for _ in range(3):
        memory.record_lookup(conn, "rust performance", [FakeResult()])
    memory.record_activation(conn, "agent:rust-pro", "rust-pro", was="vaulted")
    gaps = [s for s in memory.suggestions(conn, now=NOW) if s.kind == "gap"]
    assert [g for g in gaps if "rust" in g.subject] == []


def test_an_empty_query_records_nothing(fake_home):
    conn = db.connect()
    memory.record_lookup(conn, "   ", [])
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0


def test_learning_rows_survive_mine(fake_home):
    """mine rebuilds kind='invocation' from transcripts on every run. These
    rows are written once and must never be caught by that delete."""
    conn = db.connect()
    memory.record_lookup(conn, "make a video", [FakeResult()])
    memory.record_activation(conn, "skill:alpha", "alpha", was="vaulted")
    conn.execute("INSERT INTO events (ts, kind, node_id) VALUES ('t', 'invocation', 'skill:alpha')")
    conn.commit()

    mine.mine(conn)

    kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM events")}
    assert "lookup" in kinds and "activation" in kinds
    assert "invocation" not in kinds  # rebuilt from an empty transcript corpus


def test_a_capability_pulled_back_after_shelving_is_surfaced(fake_home):
    conn = db.connect()
    for _ in range(2):
        memory.record_activation(conn, "agent:rust-pro", "rust-pro", was="vaulted")
    items = [s for s in memory.suggestions(conn, now=NOW) if s.kind == "unshelve"]
    assert len(items) == 1
    assert items[0].subject == "rust-pro"
    assert items[0].evidence


def test_activating_something_that_was_already_live_is_not_a_shelving_mistake(fake_home):
    conn = db.connect()
    memory.record_activation(conn, "agent:rust-pro", "rust-pro", was="live")
    assert [s for s in memory.suggestions(conn, now=NOW) if s.kind == "unshelve"] == []


def test_a_repeated_fruitless_search_becomes_a_gap(fake_home):
    conn = db.connect()
    for _ in range(3):
        memory.record_lookup(conn, "Terraform module review", [])
    gaps = [s for s in memory.suggestions(conn, now=NOW) if s.kind == "gap"]
    assert len(gaps) == 1
    assert "terraform" in gaps[0].subject


def test_a_single_fruitless_search_is_not_a_gap(fake_home):
    """One search is a question; a repeated one is a pattern."""
    conn = db.connect()
    memory.record_lookup(conn, "one off", [])
    assert [s for s in memory.suggestions(conn, now=NOW) if s.kind == "gap"] == []


def test_recent_activations_weigh_more_than_old_ones(fake_home):
    conn = db.connect()
    old = (NOW - timedelta(days=365)).isoformat()
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'activation', 'a', ?)",
        (old, json.dumps({"name": "ancient", "was": "vaulted"})),
    )
    conn.commit()
    memory.record_activation(conn, "b", "recent", was="vaulted")

    weights = {}
    for item in memory.suggestions(conn, now=NOW):
        if item.kind == "unshelve":
            weights[item.subject] = float(item.evidence[-1].split()[-1])
    assert weights["recent"] > weights["ancient"]


def test_a_holding_vault_is_reported_as_working(fake_home):
    """Evidence that shelving was RIGHT is worth surfacing too, not just
    evidence that it was wrong."""
    conn = db.connect()
    conn.execute("INSERT INTO nodes (id, kind, name, state) VALUES ('a','skill','a','vaulted')")
    conn.commit()
    assert [s for s in memory.suggestions(conn, now=NOW) if s.kind == "wasted"]


def test_no_usage_yet_says_so_plainly(fake_home):
    conn = db.connect()
    assert "nothing learned yet" in memory.render(memory.suggestions(conn, now=NOW))


def test_suggestions_change_nothing(fake_home):
    """It reports. Re-ranking or un-shelving silently would make the tool's
    behaviour unexplainable the first time it surprised someone."""
    conn = db.connect()
    memory.record_activation(conn, "agent:rust-pro", "rust-pro", was="vaulted")
    before = conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
    memory.suggestions(conn, now=NOW)
    assert conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"] == before


def test_a_corrupt_payload_does_not_break_reporting(fake_home):
    conn = db.connect()
    conn.execute("INSERT INTO events (ts, kind, node_id, payload) VALUES ('t','activation','a','{not json')")
    conn.commit()
    memory.suggestions(conn, now=NOW)  # must not raise


def test_resolve_project_returns_none_rather_than_a_nearest_ancestor(fake_home):
    """An earlier version walked up to the closest existing directory, so an
    unknown key resolved to the home directory and would have pointed the
    operator at unrelated notes."""
    assert memory.resolve_project("-definitely-not-a-real-path-xyz") is None


def test_resolve_project_prefers_the_longest_matching_component(tmp_path, monkeypatch):
    """Real directory names contain hyphens, so the key is lossy."""
    target = tmp_path / "Some-Project"
    target.mkdir()
    key = "-" + str(target).replace("/", "-").lstrip("-")
    assert memory.resolve_project(key) == target


def test_project_notes_lists_only_files_that_exist(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("use profile X here\n")
    found = dict(memory.project_notes(tmp_path))
    assert "CLAUDE.md" in found and found["CLAUDE.md"] == 1
    assert "AGENTS.md" not in found


def test_project_notes_is_empty_for_a_project_with_none(tmp_path):
    assert memory.project_notes(tmp_path) == []
