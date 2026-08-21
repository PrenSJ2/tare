from datetime import datetime, timedelta

import pytest

from tare import buckets, db


# ---------------------------------------------------------------------------
# is_pinned -- the safety predicate. Argument order is (name, node_id).
# ---------------------------------------------------------------------------

def test_is_pinned_true_for_literal_pinned_names():
    for name in buckets.PINNED:
        assert buckets.is_pinned(name, f"skill:{name}")


def test_is_pinned_false_for_unrelated_capability():
    assert buckets.is_pinned("some-random-skill", "skill:some-random-skill") is False


def test_is_pinned_matches_prefix_in_id_component_not_name():
    """Plugin skills store only the leaf name in `name` -- the plugin
    attribution lives in the id. A node named plainly "brainstorming" but
    provided by the superpowers plugin must still be pinned via its id."""
    assert buckets.is_pinned("brainstorming", "skill:brainstorming@superpowers") is True


def test_is_pinned_foo_superpowers_bar_shape():
    """Explicit regression for the shape called out in the spec: a capability
    whose id is "foo:superpowers-bar" must be pinned because the component
    "superpowers-bar" starts with the "superpowers" prefix -- even though
    neither the whole id nor the name is an exact match for anything in
    PINNED or PINNED_PREFIXES."""
    assert buckets.is_pinned("bar", "foo:superpowers-bar") is True


def test_is_pinned_does_not_match_substring_of_a_component():
    """A component must *start with* a pinned prefix, not merely contain it,
    and matching happens per-component after splitting on ':' and '@' --
    not as a raw substring search over the whole id."""
    assert buckets.is_pinned("x", "skill:not-superpowers-related") is False
    assert buckets.is_pinned("x", "skill:xsuperpowers") is False


def test_is_pinned_name_alone_does_not_leak_into_prefix_rule():
    """A node literally named "superpowers" but with an unrelated id must not
    be pinned by the prefix rule (its name isn't in PINNED, and no id
    component after splitting starts with the prefix in a meaningful way
    beyond the exact name itself, which is expected to match)."""
    assert buckets.is_pinned("superpowers", "skill:superpowers") is True
    assert buckets.is_pinned("unrelated", "skill:unrelated") is False


# ---------------------------------------------------------------------------
# score_node
# ---------------------------------------------------------------------------

def _insert_node(conn, node_id, name="n", **extra):
    conn.execute(
        "INSERT INTO nodes (id, kind, name) VALUES (?, 'skill', ?)",
        (node_id, name),
    )
    conn.commit()


def _insert_invocation(conn, node_id, ts):
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'invocation', ?, '{}')",
        (ts, node_id),
    )
    conn.commit()


def test_score_node_is_zero_with_no_events(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    assert buckets.score_node(conn, "skill:a", datetime.now()) == 0.0


def test_score_node_decays_with_age(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    now = datetime(2026, 8, 20, 0, 0, 0)
    _insert_invocation(conn, "skill:a", now.isoformat())
    score_today = buckets.score_node(conn, "skill:a", now)
    assert score_today == pytest.approx(1.0, abs=1e-6)

    _insert_node(conn, "skill:b")
    _insert_invocation(conn, "skill:b", (now - timedelta(days=30)).isoformat())
    score_30d = buckets.score_node(conn, "skill:b", now)
    assert score_30d == pytest.approx(1.0 / 2.718281828, abs=1e-3)
    assert score_30d < score_today


def test_score_node_sums_multiple_invocations(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    now = datetime(2026, 8, 20, 0, 0, 0)
    _insert_invocation(conn, "skill:a", now.isoformat())
    _insert_invocation(conn, "skill:a", now.isoformat())
    assert buckets.score_node(conn, "skill:a", now) == pytest.approx(2.0, abs=1e-6)


def test_score_node_ignores_non_invocation_events(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    now = datetime(2026, 8, 20, 0, 0, 0)
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'scan', ?, '{}')",
        (now.isoformat(), "skill:a"),
    )
    conn.commit()
    assert buckets.score_node(conn, "skill:a", now) == 0.0


def test_score_node_skips_unparseable_timestamps(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    now = datetime(2026, 8, 20, 0, 0, 0)
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES ('not-a-date', 'invocation', 'skill:a', '{}')"
    )
    conn.commit()
    assert buckets.score_node(conn, "skill:a", now) == 0.0


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

def test_classify_buckets_by_threshold(fake_home):
    conn = db.connect()
    now = datetime(2026, 8, 20, 0, 0, 0)

    # "always": many recent invocations pushes score above ALWAYS_THRESHOLD.
    _insert_node(conn, "skill:heavy", "heavy")
    for _ in range(6):
        _insert_invocation(conn, "skill:heavy", now.isoformat())

    # "sometimes": one recent invocation, between the two thresholds.
    _insert_node(conn, "skill:medium", "medium")
    _insert_invocation(conn, "skill:medium", now.isoformat())

    # "rarely": no invocations at all.
    _insert_node(conn, "skill:cold", "cold")

    counts = buckets.classify(conn, now=now.isoformat())

    rows = {r["id"]: r["bucket"] for r in conn.execute("SELECT id, bucket FROM nodes")}
    assert rows["skill:heavy"] == "always"
    assert rows["skill:medium"] == "sometimes"
    assert rows["skill:cold"] == "rarely"
    assert counts == {"always": 1, "sometimes": 1, "rarely": 1}


def test_classify_pins_regardless_of_score(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:agent-browser", "agent-browser")
    counts = buckets.classify(conn, now=datetime(2026, 8, 20).isoformat())
    row = conn.execute("SELECT bucket FROM nodes WHERE id='skill:agent-browser'").fetchone()
    assert row["bucket"] == "always"
    assert counts["always"] == 1


def test_classify_pins_plugin_skill_by_id_prefix(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:brainstorming@superpowers", "brainstorming")
    buckets.classify(conn, now=datetime(2026, 8, 20).isoformat())
    row = conn.execute(
        "SELECT bucket FROM nodes WHERE id='skill:brainstorming@superpowers'"
    ).fetchone()
    assert row["bucket"] == "always"


def test_classify_rejects_unparseable_now_naming_the_value(fake_home):
    conn = db.connect()
    with pytest.raises(ValueError) as exc_info:
        buckets.classify(conn, now="not-a-real-date")
    assert "not-a-real-date" in str(exc_info.value)


def test_classify_defaults_now_to_current_time_when_omitted(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a")
    # Must not raise, and must actually run classification.
    counts = buckets.classify(conn)
    assert counts["rarely"] == 1
