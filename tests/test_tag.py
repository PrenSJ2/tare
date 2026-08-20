from harness import db, mine, tag


def seed(conn, node_id="skill:a", name="a", desc="Does a thing in prose."):
    conn.execute(
        "INSERT INTO nodes (id, kind, name, origin, desc_raw) VALUES (?, 'skill', ?, 'user-authored', ?)",
        (node_id, name, desc),
    )
    conn.commit()


def test_signature_is_shared_with_mine_not_duplicated():
    """If these drift, mine stops excluding harness's own tagging exhaust."""
    assert tag.TAG_PROMPT_SIGNATURE is mine.TAG_PROMPT_SIGNATURE
    assert tag.PROMPT_TEMPLATE.startswith(mine.TAG_PROMPT_SIGNATURE)


def test_tags_a_node_and_caches_it(fake_home):
    conn = db.connect()
    seed(conn)
    calls = []

    def ask(name, desc):
        calls.append(name)
        return {"purpose_line": "Does a thing.", "when_to_use": "When x.", "tags": "a, b"}

    r = tag.tag_all(conn, ask=ask)
    assert (r.tagged, r.cached, r.failed) == (1, 0, 0)
    row = conn.execute("SELECT purpose_line, tags FROM nodes WHERE id='skill:a'").fetchone()
    assert row["purpose_line"] == "Does a thing." and row["tags"] == "a, b"

    r2 = tag.tag_all(conn, ask=ask)
    assert (r2.tagged, r2.cached) == (0, 1) and len(calls) == 1


def test_a_failure_is_counted_not_silently_skipped(fake_home):
    conn = db.connect()
    seed(conn)
    r = tag.tag_all(conn, ask=lambda n, d: None)
    assert r.failed == 1 and r.tagged == 0


def test_trailing_prose_after_the_json_is_tolerated():
    got = tag._extract_json('Sure!\n{"purpose_line": "x"}\nHope that helps.')
    assert got == {"purpose_line": "x"}


def test_nested_objects_survive():
    assert tag._extract_json('{"a": {"b": 1}}') == {"a": {"b": 1}}


def test_a_string_where_tags_should_be_a_list_is_rejected():
    """Iterating a string yields characters and previously stored garbage as success."""
    assert tag._normalise({"purpose_line": "p", "when_to_use": "w", "tags": "abc"}, "n") is None


def test_empty_purpose_falls_back_to_the_name():
    out = tag._normalise({"purpose_line": "  ", "when_to_use": "w", "tags": []}, "slideshow")
    assert out["purpose_line"] == "slideshow"


def test_non_string_purpose_is_rejected():
    assert tag._normalise({"purpose_line": 3, "when_to_use": "w", "tags": []}, "n") is None
