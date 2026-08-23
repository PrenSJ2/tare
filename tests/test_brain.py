"""Findings: retrieval of what has been learned.

The ranking rules here are the whole feature. A finding nobody can retrieve is
a file, and a retrieval that quietly withholds one is worse than no retrieval
at all — you conclude you never wrote it down.
"""

from tare import brain, db


def _write(home, name, scope="universal", body="A claim about how a thing behaves.",
           confidence="verified", superseded_by="null", tools="[]", projects="[]"):
    directory = home / "brain" / "findings"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ntype: finding\nscope: {scope}\ntools: {tools}\n"
        f"projects: {projects}\nconfidence: {confidence}\n"
        f"superseded_by: {superseded_by}\ntags: []\n---\n\n{body}\n",
        encoding="utf-8",
    )


# --- parsing ---------------------------------------------------------------

def test_a_finding_is_parsed_into_its_claim(fake_home):
    _write(fake_home, "docker-restart-is-not-boot",
           body="`restart: unless-stopped` is not start-on-boot.\n\n**Why:** it means restart unless a human stopped it.")
    found = brain.load()
    assert len(found) == 1
    # The summary is the claim, which is what a result list has room for.
    assert found[0].summary == "`restart: unless-stopped` is not start-on-boot."
    assert found[0].scope == "universal"


def test_a_file_without_frontmatter_is_not_a_finding(fake_home):
    directory = fake_home / "brain" / "findings"
    directory.mkdir(parents=True)
    (directory / "stray.md").write_text("just some notes someone dropped in\n")
    # Skipped rather than guessed at: a note is not a claim.
    assert brain.load() == []


def test_an_unknown_scope_falls_back_to_the_narrowest(fake_home):
    _write(fake_home, "typo", scope="univeral")   # misspelled
    # A typo must hide a finding from other projects, never leak a project
    # detail into all of them.
    assert brain.load()[0].scope == "project"


def test_wikilinks_are_collected(fake_home):
    _write(fake_home, "a", body="Claim.\n\nSee also [[b]] and [[c]] and [[b]] again.")
    assert brain.load()[0].links == ["b", "c"]


# --- retrieval -------------------------------------------------------------

def test_a_finding_can_be_recalled_by_what_it_is_about(fake_home):
    _write(fake_home, "macos-caches-negative-dns",
           body="macOS caches NXDOMAIN, so a name keeps failing after DNS is fixed.")
    conn = db.connect()
    brain.reindex(conn)
    results = brain.recall(conn, "dns name still failing after fix")
    assert results and results[0].name == "macos-caches-negative-dns"


def test_a_superseded_finding_never_outranks_what_replaced_it(fake_home):
    _write(fake_home, "bandwidth-is-precious", scope="project",
           body="Bandwidth is precious on this network, everything is on a hotspot.",
           superseded_by="wired-wan-now")
    _write(fake_home, "wired-wan-now", scope="project",
           body="Bandwidth is no longer precious on this network: the WAN is wired now.")
    conn = db.connect()
    brain.reindex(conn)
    results = brain.recall(conn, "is bandwidth precious on this network")
    names = [r.name for r in results]
    assert names.index("wired-wan-now") < names.index("bandwidth-is-precious")
    # ...and it still comes back, flagged, because "we used to believe this" is
    # what stops the belief being re-derived from scratch.
    old = next(r for r in results if r.name == "bandwidth-is-precious")
    assert old.superseded_by == "wired-wan-now"


def test_another_projects_finding_is_ranked_down_but_never_withheld(fake_home):
    """Scope is a weight, not a filter — and not a sort key either.

    Sorting on scope demoted every project-scoped finding below every
    universal one, and `limit` then cut them off entirely. Asking "is
    bandwidth expensive here" from another directory returned three unrelated
    findings and neither of the two that answered the question.
    """
    _write(fake_home, "homelab-bandwidth", scope="project", projects="[homelab]",
           body="Bandwidth on this network is metered and expensive.")
    for i in range(4):
        _write(fake_home, f"filler-{i}", body=f"Some unrelated network claim {i}.")
    conn = db.connect()
    brain.reindex(conn)

    results = brain.recall(conn, "bandwidth expensive network", project="mobile-app", limit=3)
    assert "homelab-bandwidth" in [r.name for r in results]
    assert next(r for r in results if r.name == "homelab-bandwidth").in_scope_here is False


def test_a_finding_from_this_project_is_in_scope(fake_home):
    _write(fake_home, "local-thing", scope="project", projects="[mobile-app]",
           body="A claim about the build in this repository.")
    conn = db.connect()
    brain.reindex(conn)
    result = brain.recall(conn, "claim about the build", project="mobile-app")[0]
    assert result.in_scope_here is True


def test_universal_findings_are_in_scope_everywhere(fake_home):
    _write(fake_home, "everywhere", scope="universal", body="A claim true in all repositories.")
    conn = db.connect()
    brain.reindex(conn)
    assert brain.recall(conn, "claim true repositories", project="anything")[0].in_scope_here


# --- the index is a rebuild ------------------------------------------------

def test_reindex_rebuilds_rather_than_accumulating(fake_home):
    _write(fake_home, "one", body="First claim about caching.")
    conn = db.connect()
    assert brain.reindex(conn) == 1

    (fake_home / "brain" / "findings" / "one.md").unlink()
    _write(fake_home, "two", body="Second claim about caching.")
    assert brain.reindex(conn) == 1
    # Files are the source of truth; a deleted finding must not linger in the
    # index answering queries.
    assert [r.name for r in brain.recall(conn, "claim caching")] == ["two"]


def test_no_brain_directory_is_not_an_error(fake_home):
    conn = db.connect()
    assert brain.load() == []
    assert brain.reindex(conn) == 0
    assert brain.recall(conn, "anything") == []
    assert "nothing known" in brain.render([])


def test_an_empty_query_returns_nothing_rather_than_everything(fake_home):
    _write(fake_home, "a", body="A claim.")
    conn = db.connect()
    brain.reindex(conn)
    assert brain.recall(conn, "   ") == []
