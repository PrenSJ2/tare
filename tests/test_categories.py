"""Domain grouping.

Every case here is a real misclassification the 209-node graph produced, kept
as a regression: the failures of a keyword taxonomy are not hypothetical, and
each fix traded one kind of wrongness for another until the ordering settled.
"""

from tare import categories


def test_name_beats_tags_for_language_agents():
    # The tags on these describe what the agent touches, not what it is, and
    # scattered the language experts across four domains.
    assert categories.of("golang-pro", tags="design,backend") == "code"
    assert categories.of("elixir-pro", tags="agent") == "code"
    assert categories.of("php-pro", tags="data") == "code"


def test_tags_beat_prose():
    # "writing and reviewing idiomatic Rust" put rust-pro in `writing`.
    assert categories.of(
        "rust-pro", tags="rust,systems-programming",
        purpose="writing and reviewing idiomatic Rust") == "code"
    # "design patterns" put docs-architect in `design`.
    assert categories.of(
        "docs-architect", tags="documentation,api-docs",
        purpose="documents architecture and design patterns") == "writing"


def test_purpose_is_the_last_resort_not_the_first():
    # Nothing in the name or tags: prose is all there is, so it decides.
    assert categories.of("zz-unknown", tags="", purpose="edits video footage") == "video"


def test_hyphenated_terms_match_hyphenated_names():
    # The name pass normalizes hyphens to spaces. Normalizing only one side
    # meant no two-word term could ever match, and both of these fell to
    # `other` despite an exact term existing for them.
    assert categories.of("content-strategy") == "marketing"
    assert categories.of("react-native-best-practices") == "code"


def test_plurals_match():
    # Bounded matching rejects `referrals` against the term `referral`; all
    # three of these landed in `other`.
    assert categories.of("referrals") == "marketing"
    assert categories.of("emails") == "marketing"
    assert categories.of("paywalls") == "marketing"


def test_bounded_matching_rejects_substrings():
    # "ad" inside "readme", "ai" inside "explain" -- the failure mode that
    # unbounded substring matching over this vocabulary produces.
    assert categories.of("readme-writer", tags="documentation") == "writing"
    assert categories.explain("explain-this", tags="", purpose="")[0] != "data"


def test_unclassifiable_falls_back_with_no_reason():
    domain, why = categories.explain("typegpu", tags="", purpose="")
    assert domain == categories.FALLBACK
    assert why is None


def test_explain_returns_the_deciding_term():
    domain, why = categories.explain("terraform-specialist", tags="iac", purpose="")
    assert (domain, why) == ("infra", "terraform")


def test_counts_covers_every_node(fake_home):
    from tare import db  # noqa: PLC0415 - test-local; `fake_home` must redirect paths first

    conn = db.connect()
    conn.executemany(
        "INSERT INTO nodes (id, kind, name, origin, state, tags, purpose_line) "
        "VALUES (?,?,?,?,?,?,?)",
        [("a", "skill", "rust-pro", "user", "live", "rust", ""),
         ("b", "skill", "seo-audit", "user", "live", "seo", ""),
         ("c", "skill", "typegpu", "user", "live", "", "")],
    )
    assert sum(categories.counts(conn).values()) == 3
    assert categories.counts(conn)["other"] == 1
