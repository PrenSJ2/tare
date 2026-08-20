from harness import db, edges


def _insert_node(
    conn,
    node_id,
    name,
    *,
    kind="skill",
    path=None,
    provider_plugin=None,
    marketplace=None,
    purpose_line="",
    when_to_use="",
    tags="",
):
    conn.execute(
        """
        INSERT INTO nodes
            (id, kind, name, path, provider_plugin, marketplace,
             purpose_line, when_to_use, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (node_id, kind, name, path, provider_plugin, marketplace, purpose_line, when_to_use, tags),
    )
    conn.commit()


def _insert_invocation(conn, node_id, ts, session):
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'invocation', ?, ?)",
        (ts, node_id, f'{{"session": "{session}"}}'),
    )
    conn.commit()


def _edges_of_type(conn, edge_type):
    return {
        (r["src"], r["dst"]): (r["weight"], r["evidence"])
        for r in conn.execute("SELECT src, dst, weight, evidence FROM edges WHERE type = ?", (edge_type,))
    }


# ---------------------------------------------------------------------------
# provided-by
# ---------------------------------------------------------------------------

def test_provided_by_edge_from_provider_columns(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:brainstorming", "brainstorming", provider_plugin="superpowers", marketplace="anthropics")
    edges.build(conn)
    got = _edges_of_type(conn, "provided-by")
    assert ("skill:brainstorming", "plugin:superpowers@anthropics") in got


def test_provided_by_edge_without_marketplace_omits_at_sign(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:foo", "foo", provider_plugin="acme")
    edges.build(conn)
    got = _edges_of_type(conn, "provided-by")
    assert ("skill:foo", "plugin:acme") in got


def test_no_provided_by_edge_for_user_authored_skill(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:mine", "mine")
    edges.build(conn)
    assert _edges_of_type(conn, "provided-by") == {}


# ---------------------------------------------------------------------------
# routes-to
# ---------------------------------------------------------------------------

def _write_skill_file(home, dirname, content):
    d = home / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(content)
    return str(f)


def test_routes_to_edge_from_explicit_name_reference(fake_home):
    conn = db.connect()
    dst_path = _write_skill_file(fake_home, "sub-dispatch-target", "---\nname: sub-dispatch-target\n---\nDoes a narrow thing.\n")
    src_path = _write_skill_file(
        fake_home,
        "orchestrator-alpha",
        "---\nname: orchestrator-alpha\n---\nThis skill delegates to sub-dispatch-target for the narrow part.\n",
    )
    _insert_node(conn, "skill:orchestrator-alpha", "orchestrator-alpha", path=src_path)
    _insert_node(conn, "skill:sub-dispatch-target", "sub-dispatch-target", path=dst_path)

    edges.build(conn)

    got = _edges_of_type(conn, "routes-to")
    assert ("skill:orchestrator-alpha", "skill:sub-dispatch-target") in got
    # Not reciprocal: the sub-skill's own file never names the orchestrator.
    assert ("skill:sub-dispatch-target", "skill:orchestrator-alpha") not in got


def test_no_routes_to_edge_without_a_name_reference(fake_home):
    conn = db.connect()
    a_path = _write_skill_file(fake_home, "alpha-standalone", "---\nname: alpha-standalone\n---\nDoes its own thing entirely.\n")
    b_path = _write_skill_file(fake_home, "beta-standalone", "---\nname: beta-standalone\n---\nAlso does its own thing.\n")
    _insert_node(conn, "skill:alpha-standalone", "alpha-standalone", path=a_path)
    _insert_node(conn, "skill:beta-standalone", "beta-standalone", path=b_path)

    edges.build(conn)

    assert _edges_of_type(conn, "routes-to") == {}


def test_routes_to_tolerates_missing_file(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:ghost", "ghost", path=str(fake_home / "skills" / "gone" / "SKILL.md"))
    _insert_node(conn, "skill:other", "other")
    # Must not raise.
    written = edges.build(conn)
    assert isinstance(written, int)


# ---------------------------------------------------------------------------
# overlaps
# ---------------------------------------------------------------------------

def test_overlaps_edge_from_similar_description_text(fake_home):
    conn = db.connect()
    _insert_node(
        conn,
        "skill:code-reviewer",
        "code-reviewer",
        purpose_line="Reviews pull requests for code quality and correctness issues",
        when_to_use="Use after writing or modifying code",
        tags="review quality correctness code",
    )
    _insert_node(
        conn,
        "skill:architect-reviewer",
        "architect-reviewer",
        purpose_line="Reviews pull requests for architectural quality and correctness issues",
        when_to_use="Use after structural or architectural changes",
        tags="review quality correctness architecture",
    )

    edges.build(conn)

    got = _edges_of_type(conn, "overlaps")
    assert ("skill:code-reviewer", "skill:architect-reviewer") in got
    assert ("skill:architect-reviewer", "skill:code-reviewer") in got


def test_no_overlaps_edge_for_unrelated_descriptions(fake_home):
    conn = db.connect()
    _insert_node(
        conn,
        "skill:code-reviewer",
        "code-reviewer",
        purpose_line="Reviews pull requests for code quality",
        tags="review code quality",
    )
    _insert_node(
        conn,
        "skill:name-wizard",
        "name-wizard",
        purpose_line="Generates candidate names for a new product",
        tags="naming brand product",
    )

    edges.build(conn)

    got = _edges_of_type(conn, "overlaps")
    assert ("skill:code-reviewer", "skill:name-wizard") not in got


# ---------------------------------------------------------------------------
# routes-to vs overlaps must not be conflated
# ---------------------------------------------------------------------------

def test_routes_to_and_overlaps_are_independent_signals(fake_home):
    conn = db.connect()

    # Pair 1: overlapping descriptions, no dispatch. overlaps only.
    _insert_node(
        conn,
        "skill:code-reviewer",
        "code-reviewer",
        path=_write_skill_file(fake_home, "code-reviewer", "---\nname: code-reviewer\n---\nLooks at diffs for bugs.\n"),
        purpose_line="Reviews pull requests for code quality and correctness issues",
        tags="review quality correctness code",
    )
    _insert_node(
        conn,
        "skill:architect-reviewer",
        "architect-reviewer",
        path=_write_skill_file(
            fake_home, "architect-reviewer", "---\nname: architect-reviewer\n---\nLooks at structure for drift.\n"
        ),
        purpose_line="Reviews pull requests for architectural quality and correctness issues",
        tags="review quality correctness architecture",
    )

    # Pair 2: explicit dispatch, dissimilar descriptions. routes-to only.
    _insert_node(
        conn,
        "skill:orchestrator-beta",
        "orchestrator-beta",
        path=_write_skill_file(
            fake_home,
            "orchestrator-beta",
            "---\nname: orchestrator-beta\n---\nCoordinates a video render, then hands off to render-finalizer.\n",
        ),
        purpose_line="Coordinates a multi-stage video render pipeline",
        tags="video render pipeline",
    )
    _insert_node(
        conn,
        "skill:render-finalizer",
        "render-finalizer",
        path=_write_skill_file(fake_home, "render-finalizer", "---\nname: render-finalizer\n---\nEncodes the naming report.\n"),
        purpose_line="Generates a naming report for a new brand",
        tags="naming brand report",
    )

    edges.build(conn)

    routes = _edges_of_type(conn, "routes-to")
    overlaps = _edges_of_type(conn, "overlaps")

    assert ("skill:code-reviewer", "skill:architect-reviewer") in overlaps
    assert ("skill:code-reviewer", "skill:architect-reviewer") not in routes

    assert ("skill:orchestrator-beta", "skill:render-finalizer") in routes
    assert ("skill:orchestrator-beta", "skill:render-finalizer") not in overlaps
    assert ("skill:render-finalizer", "skill:orchestrator-beta") not in overlaps


# ---------------------------------------------------------------------------
# used-with
# ---------------------------------------------------------------------------

def test_used_with_edge_from_shared_session(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_invocation(conn, "skill:a", "2026-08-19T10:00:00", "sess-1")
    _insert_invocation(conn, "skill:b", "2026-08-19T10:05:00", "sess-1")

    edges.build(conn)

    got = _edges_of_type(conn, "used-with")
    assert ("skill:a", "skill:b") in got
    assert ("skill:b", "skill:a") in got
    assert got[("skill:a", "skill:b")][0] == 1.0


def test_used_with_weight_counts_distinct_sessions(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_invocation(conn, "skill:a", "2026-08-19T10:00:00", "sess-1")
    _insert_invocation(conn, "skill:b", "2026-08-19T10:05:00", "sess-1")
    _insert_invocation(conn, "skill:a", "2026-08-19T11:00:00", "sess-2")
    _insert_invocation(conn, "skill:b", "2026-08-19T11:05:00", "sess-2")

    edges.build(conn)

    got = _edges_of_type(conn, "used-with")
    assert got[("skill:a", "skill:b")][0] == 2.0


def test_no_used_with_edge_for_different_sessions(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_invocation(conn, "skill:a", "2026-08-19T10:00:00", "sess-1")
    _insert_invocation(conn, "skill:b", "2026-08-19T10:05:00", "sess-2")

    edges.build(conn)

    assert _edges_of_type(conn, "used-with") == {}


# ---------------------------------------------------------------------------
# build() idempotence and return value
# ---------------------------------------------------------------------------

def test_build_is_idempotent(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:brainstorming", "brainstorming", provider_plugin="superpowers", marketplace="anthropics")
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_invocation(conn, "skill:a", "2026-08-19T10:00:00", "sess-1")
    _insert_invocation(conn, "skill:b", "2026-08-19T10:05:00", "sess-1")

    first = edges.build(conn)
    rows_after_first = {
        (r["src"], r["dst"], r["type"]) for r in conn.execute("SELECT src, dst, type FROM edges")
    }

    second = edges.build(conn)
    rows_after_second = {
        (r["src"], r["dst"], r["type"]) for r in conn.execute("SELECT src, dst, type FROM edges")
    }

    assert first == second
    assert rows_after_first == rows_after_second


def test_build_return_value_matches_row_count_for_its_types(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", provider_plugin="acme")
    _insert_node(conn, "skill:b", "b")

    written = edges.build(conn)
    placeholders = ", ".join("?" * len(edges.EDGE_TYPES))
    (count,) = conn.execute(
        f"SELECT COUNT(*) FROM edges WHERE type IN ({placeholders})", edges.EDGE_TYPES
    ).fetchone()
    assert written == count


def test_build_leaves_other_edge_types_untouched(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    conn.execute(
        "INSERT INTO edges (src, dst, type, weight, evidence) VALUES ('skill:a', 'skill:z', 'manual-note', 1.0, 'kept')"
    )
    conn.commit()

    edges.build(conn)

    row = conn.execute("SELECT * FROM edges WHERE type = 'manual-note'").fetchone()
    assert row is not None
