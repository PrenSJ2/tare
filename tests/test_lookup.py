"""Tests for lookup.py: FTS5 + BM25 + usage prior, relationship chains.

`fake_home` is used throughout -- nothing here touches the operator's real
~/.claude. A separate section at the bottom validates against the read-only
golden database when it's present on this machine; those tests skip cleanly
if it isn't.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from harness import db, lookup

GOLDEN_DB = os.path.expanduser("~/Documents/Code/harness-recovery/harness-original.db")


def _insert_node(
    conn,
    node_id,
    name,
    *,
    kind="skill",
    state="live",
    purpose_line="",
    when_to_use="",
    tags="",
    desc_raw="",
    est_tokens=10,
):
    conn.execute(
        """
        INSERT INTO nodes
            (id, kind, name, state, purpose_line, when_to_use, tags, desc_raw, est_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (node_id, kind, name, state, purpose_line, when_to_use, tags, desc_raw, est_tokens),
    )
    conn.commit()


def _insert_usage(conn, node_id, invocations):
    conn.execute(
        "INSERT INTO usage (node_id, invocations, sessions, last_used) VALUES (?, ?, 1, NULL)",
        (node_id, invocations),
    )
    conn.commit()


def _names(results):
    return [r.name for r in results]


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


def test_reindex_returns_row_count(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="does a thing")
    _insert_node(conn, "skill:b", "b", purpose_line="does another thing")
    assert lookup.reindex(conn) == 2


def test_reindex_is_idempotent(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="rust performance tuning")
    lookup.reindex(conn)
    first = conn.execute("SELECT COUNT(*) AS c FROM nodes_fts").fetchone()["c"]
    lookup.reindex(conn)
    second = conn.execute("SELECT COUNT(*) AS c FROM nodes_fts").fetchone()["c"]
    assert first == second == 1
    # And results are stable across repeated reindexing, not accumulating.
    results = lookup.lookup(conn, "rust performance")
    assert _names(results) == ["a"]


def test_reindex_reflects_updated_node_text(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="original text about widgets")
    lookup.reindex(conn)
    conn.execute("UPDATE nodes SET purpose_line = ? WHERE id = ?", ("now about rockets", "skill:a"))
    conn.commit()
    lookup.reindex(conn)
    assert _names(lookup.lookup(conn, "rockets")) == ["a"]
    assert _names(lookup.lookup(conn, "widgets")) == []


# ---------------------------------------------------------------------------
# lookup: basic ranking
# ---------------------------------------------------------------------------


def test_lookup_ranks_stronger_text_match_first(fake_home):
    conn = db.connect()
    _insert_node(
        conn,
        "skill:rust-pro",
        "rust-pro",
        purpose_line="A Rust specialist for ownership, lifetimes and performance tuning",
        when_to_use="Fire on Rust work: borrow-checker errors, async concurrency, performance tuning",
        tags="rust,performance,systems-programming",
    )
    _insert_node(
        conn,
        "skill:name-wizard",
        "name-wizard",
        purpose_line="Generates candidate names for a new product",
        tags="naming,brand",
    )
    lookup.reindex(conn)

    results = lookup.lookup(conn, "optimize rust performance")
    assert results
    assert results[0].name == "rust-pro"


def test_lookup_returns_no_state_filter_vaulted_node_findable(fake_home):
    """The entire bargain of the vault: state drops the context cost, not
    the findability. A vaulted node must be returned, labelled as such."""
    conn = db.connect()
    _insert_node(
        conn,
        "skill:unity-developer",
        "unity-developer",
        state="vaulted",
        purpose_line="Unity game development specialist for gameplay scripts and builds",
        when_to_use="Fire when work involves a Unity project or game mechanics",
        tags="unity,game-dev,csharp",
    )
    lookup.reindex(conn)

    results = lookup.lookup(conn, "build a unity game")
    assert results
    assert results[0].name == "unity-developer"
    assert results[0].state == "vaulted"


def test_lookup_result_carries_expected_fields(fake_home):
    conn = db.connect()
    _insert_node(
        conn,
        "skill:code-reviewer",
        "code-reviewer",
        kind="agent",
        purpose_line="Reviews code for quality and security issues",
        when_to_use="Fire after writing or modifying code",
        tags="review,security",
    )
    _insert_usage(conn, "skill:code-reviewer", 12)
    lookup.reindex(conn)

    results = lookup.lookup(conn, "review code for security")
    assert results
    r = results[0]
    assert r.name == "code-reviewer"
    assert r.state == "live"
    assert r.kind == "agent"
    assert r.purpose_line == "Reviews code for quality and security issues"
    assert r.when_to_use == "Fire after writing or modifying code"
    assert r.invocations == 12


def test_lookup_respects_limit(fake_home):
    conn = db.connect()
    for i in range(10):
        _insert_node(conn, f"skill:widget-{i}", f"widget-{i}", purpose_line="a widget tool for widgets")
    lookup.reindex(conn)

    results = lookup.lookup(conn, "widget", limit=3)
    assert len(results) == 3


def test_lookup_empty_query_returns_no_results(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="something")
    lookup.reindex(conn)
    assert lookup.lookup(conn, "") == []
    assert lookup.lookup(conn, "   ") == []


def test_lookup_no_match_returns_empty_list(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="rust performance tuning")
    lookup.reindex(conn)
    assert lookup.lookup(conn, "zzznonexistentqueryzzz") == []


def test_lookup_query_with_boolean_keyword_is_literal_not_operator(fake_home):
    """A query containing "and"/"or" must be searched as literal text, not
    reinterpreted as an FTS5 boolean operator -- see _fts_query's docstring."""
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", purpose_line="rust and go performance")
    lookup.reindex(conn)
    # Must not raise a syntax error, and must find the match.
    results = lookup.lookup(conn, "and")
    assert _names(results) == ["a"]


# ---------------------------------------------------------------------------
# usage prior: multiplicative, not additive
# ---------------------------------------------------------------------------


def test_usage_prior_boosts_between_comparable_text_matches(fake_home):
    """Two nodes with essentially the same text relevance to the query --
    the heavily-invoked one should rank first."""
    conn = db.connect()
    _insert_node(
        conn,
        "skill:popular-linter",
        "popular-linter",
        purpose_line="Lints code for style and quality issues",
        tags="lint,style,quality",
    )
    _insert_node(
        conn,
        "skill:obscure-linter",
        "obscure-linter",
        purpose_line="Lints code for style and quality issues",
        tags="lint,style,quality",
    )
    _insert_usage(conn, "skill:popular-linter", 40)
    lookup.reindex(conn)

    results = lookup.lookup(conn, "lint code style quality")
    assert _names(results)[0] == "popular-linter"


def test_usage_prior_does_not_override_a_much_stronger_text_match(fake_home):
    """This is the regression test for the additive bug described in the
    module docstring: a heavily-used but textually-irrelevant node must NOT
    outrank a weakly-used node whose text is a strong match. Under the old
    additive scheme (score = bm25 + weight * log1p(invocations)), the usage
    term (~3.58 for 35 invocations) swamped bm25 (~5e-6) and the most-used
    capability won every query regardless of relevance."""
    conn = db.connect()
    _insert_node(
        conn,
        "skill:rust-pro",
        "rust-pro",
        purpose_line="A Rust specialist for ownership, lifetimes and performance tuning",
        when_to_use="Fire on Rust work: borrow-checker errors, async concurrency, performance",
        tags="rust,performance,systems-programming,concurrency",
    )
    _insert_node(
        conn,
        "skill:heavily-used-but-irrelevant",
        "heavily-used-but-irrelevant",
        purpose_line="Formats commit messages according to team conventions",
        tags="git,commits,formatting",
    )
    _insert_usage(conn, "skill:heavily-used-but-irrelevant", 35)
    lookup.reindex(conn)

    results = lookup.lookup(conn, "optimize rust performance")
    assert results
    assert results[0].name == "rust-pro"


def test_usage_prior_weight_is_half(fake_home):
    assert lookup.USAGE_PRIOR_WEIGHT == 0.5


# ---------------------------------------------------------------------------
# relationship chains
# ---------------------------------------------------------------------------


def test_lookup_includes_routes_to_chain(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:hyperframes", "hyperframes", purpose_line="Orchestrates video composition")
    _insert_node(conn, "skill:hyperframes-core", "hyperframes-core", purpose_line="Composition contract details")
    conn.execute(
        "INSERT INTO edges (src, dst, type, weight, evidence) VALUES "
        "('skill:hyperframes', 'skill:hyperframes-core', 'routes-to', 1.0, 'body references')"
    )
    conn.commit()
    lookup.reindex(conn)

    results = lookup.lookup(conn, "hyperframes")
    by_name = {r.name: r for r in results}
    assert any("routes-to" in c and "hyperframes-core" in c for c in by_name["hyperframes"].chains)


def test_lookup_chain_direction_is_preserved_for_incoming_edge(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:orchestrator", "orchestrator", purpose_line="Runs a pipeline")
    _insert_node(conn, "skill:leaf-tool", "leaf-tool", purpose_line="A narrow pipeline helper tool")
    conn.execute(
        "INSERT INTO edges (src, dst, type, weight, evidence) VALUES "
        "('skill:orchestrator', 'skill:leaf-tool', 'routes-to', 1.0, 'body references')"
    )
    conn.commit()
    lookup.reindex(conn)

    results = lookup.lookup(conn, "leaf-tool pipeline")
    leaf = next(r for r in results if r.name == "leaf-tool")
    assert any(c.startswith("orchestrator --(routes-to)-->") for c in leaf.chains)


def test_lookup_node_with_no_edges_has_empty_chains(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:lonely", "lonely", purpose_line="Stands entirely alone")
    lookup.reindex(conn)
    results = lookup.lookup(conn, "lonely")
    assert results[0].chains == []


# ---------------------------------------------------------------------------
# Golden database validation
# ---------------------------------------------------------------------------

pytestmark_golden = pytest.mark.skipif(
    not os.path.exists(GOLDEN_DB), reason="golden database not present on this machine"
)


def _golden_conn():
    """Opened strictly read-only: this fixture is a safety copy of a real,
    once-lost database, and it must never be written to -- so these tests
    query the index it already ships with rather than calling reindex()."""
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@pytestmark_golden
def test_golden_db_optimize_rust_performance_surfaces_rust_pro():
    results = lookup.lookup(_golden_conn(), "optimize rust performance")
    assert _names(results)
    assert "rust-pro" in _names(results)


@pytestmark_golden
def test_golden_db_build_a_unity_game_surfaces_unity_developer():
    results = lookup.lookup(_golden_conn(), "build a unity game")
    assert "unity-developer" in _names(results)


@pytestmark_golden
def test_golden_db_review_code_for_security_surfaces_code_reviewer():
    results = lookup.lookup(_golden_conn(), "review this code for security")
    matches = [r for r in results if r.name == "code-reviewer"]
    assert matches
    assert matches[0].invocations == 165


@pytestmark_golden
def test_golden_db_vaulted_capability_is_findable_and_labelled():
    results = lookup.lookup(_golden_conn(), "optimize rust performance")
    rust_pro = next(r for r in results if r.name == "rust-pro")
    assert rust_pro.state == "vaulted"
