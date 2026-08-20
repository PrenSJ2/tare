"""Tests for audit.py: token cost, buckets, duplicates, mixed plugins,
coverage.

`fake_home` throughout; a golden-database section at the bottom validates
against the real, read-only pre-loss corpus and skips cleanly when it isn't
present on this machine.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from harness import audit, db, paths


def _insert_node(
    conn,
    node_id,
    name,
    *,
    kind="skill",
    state="live",
    bucket=None,
    provider_plugin=None,
    marketplace=None,
    origin="user-authored",
    est_tokens=100,
    path=None,
):
    conn.execute(
        """
        INSERT INTO nodes
            (id, kind, name, state, bucket, provider_plugin, marketplace, origin, est_tokens, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (node_id, kind, name, state, bucket, provider_plugin, marketplace, origin, est_tokens, path),
    )
    conn.commit()


def _insert_usage(conn, node_id, invocations):
    conn.execute(
        "INSERT INTO usage (node_id, invocations, sessions, last_used) VALUES (?, ?, 1, NULL)",
        (node_id, invocations),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# total_tokens / never_invoked_tokens: state='live' only
# ---------------------------------------------------------------------------


def test_total_tokens_counts_only_live_state(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:live-one", "live-one", state="live", est_tokens=100)
    _insert_node(conn, "skill:vaulted-one", "vaulted-one", state="vaulted", est_tokens=500)
    _insert_node(conn, "skill:disabled-one", "disabled-one", state="plugin-disabled", est_tokens=300)

    a = audit.audit(conn)
    assert a.total_tokens == 100


def test_never_invoked_tokens_only_counts_live_nodes_with_zero_invocations(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:used", "used", state="live", est_tokens=50)
    _insert_usage(conn, "skill:used", 5)
    _insert_node(conn, "skill:unused", "unused", state="live", est_tokens=70)
    _insert_node(conn, "skill:vaulted-unused", "vaulted-unused", state="vaulted", est_tokens=999)

    a = audit.audit(conn)
    assert a.total_tokens == 120  # used + unused, both live
    assert a.never_invoked_tokens == 70  # only the live-and-unused one


def test_disabled_tokens_and_count_come_from_plugin_disabled_state(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", state="plugin-disabled", est_tokens=100)
    _insert_node(conn, "skill:b", "b", state="plugin-disabled", est_tokens=200)
    _insert_node(conn, "skill:c", "c", state="live", est_tokens=50)

    a = audit.audit(conn)
    assert a.disabled_skills == 2
    assert a.disabled_tokens == 300


# ---------------------------------------------------------------------------
# by_bucket
# ---------------------------------------------------------------------------


def test_by_bucket_counts(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", bucket="always")
    _insert_node(conn, "skill:b", "b", bucket="always")
    _insert_node(conn, "skill:c", "c", bucket="sometimes")
    _insert_node(conn, "skill:d", "d", bucket="rarely")
    _insert_node(conn, "skill:e", "e", bucket=None)

    a = audit.audit(conn)
    assert a.by_bucket == {"always": 2, "sometimes": 1, "rarely": 1}


# ---------------------------------------------------------------------------
# mixed plugins
# ---------------------------------------------------------------------------


def test_mixed_plugin_reports_used_over_total_and_stuck_tokens(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:p@mk:used-1", "used-1", provider_plugin="p", marketplace="mk", est_tokens=100)
    _insert_usage(conn, "skill:p@mk:used-1", 3)
    _insert_node(conn, "skill:p@mk:unused-1", "unused-1", provider_plugin="p", marketplace="mk", est_tokens=200)
    _insert_node(conn, "skill:p@mk:unused-2", "unused-2", provider_plugin="p", marketplace="mk", est_tokens=300)

    a = audit.audit(conn)
    assert len(a.mixed_plugins) == 1
    mp = a.mixed_plugins[0]
    assert mp["label"] == "p@mk"
    assert mp["used"] == 1
    assert mp["total"] == 3
    assert mp["tokens_stuck"] == 500


def test_mixed_plugin_includes_plugin_disabled_skills_in_total_and_tokens(fake_home):
    """A partial shelve may have already flipped some of a plugin's skills
    to plugin-disabled; the operator still needs the whole plugin's picture,
    not just the currently-loaded slice."""
    conn = db.connect()
    _insert_node(
        conn, "skill:p@mk:used-1", "used-1", provider_plugin="p", marketplace="mk", state="live", est_tokens=50
    )
    _insert_usage(conn, "skill:p@mk:used-1", 1)
    _insert_node(
        conn,
        "skill:p@mk:disabled-unused",
        "disabled-unused",
        provider_plugin="p",
        marketplace="mk",
        state="plugin-disabled",
        est_tokens=400,
    )

    a = audit.audit(conn)
    mp = a.mixed_plugins[0]
    assert mp["used"] == 1
    assert mp["total"] == 2
    assert mp["tokens_stuck"] == 400


def test_wholly_unused_plugin_is_not_mixed(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:p@mk:a", "a", provider_plugin="p", marketplace="mk", est_tokens=100)
    _insert_node(conn, "skill:p@mk:b", "b", provider_plugin="p", marketplace="mk", est_tokens=100)

    a = audit.audit(conn)
    assert a.mixed_plugins == []


def test_wholly_used_plugin_is_not_mixed(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:p@mk:a", "a", provider_plugin="p", marketplace="mk", est_tokens=100)
    _insert_usage(conn, "skill:p@mk:a", 1)
    _insert_node(conn, "skill:p@mk:b", "b", provider_plugin="p", marketplace="mk", est_tokens=100)
    _insert_usage(conn, "skill:p@mk:b", 1)

    a = audit.audit(conn)
    assert a.mixed_plugins == []


def test_mixed_plugin_label_omits_at_sign_without_marketplace(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:p:a", "a", provider_plugin="p", marketplace=None, est_tokens=100)
    _insert_usage(conn, "skill:p:a", 1)
    _insert_node(conn, "skill:p:b", "b", provider_plugin="p", marketplace=None, est_tokens=100)

    a = audit.audit(conn)
    assert a.mixed_plugins[0]["label"] == "p"


# ---------------------------------------------------------------------------
# duplicates
# ---------------------------------------------------------------------------


def test_duplicates_detects_same_name_different_source(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:frontend-design", "frontend-design", origin="user-authored")
    _insert_node(
        conn,
        "skill:frontend-design@claude-plugins-official:frontend-design",
        "frontend-design",
        origin="plugin",
        provider_plugin="frontend-design",
        marketplace="claude-plugins-official",
    )

    a = audit.audit(conn)
    assert len(a.duplicates) == 1
    dup = a.duplicates[0]
    assert dup["name"] == "frontend-design"
    assert set(dup["ids"]) == {
        "skill:frontend-design",
        "skill:frontend-design@claude-plugins-official:frontend-design",
    }


def test_no_duplicates_for_unique_names(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    a = audit.audit(conn)
    assert a.duplicates == []


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------


def test_coverage_counts_classified_against_found_skill_md_files(fake_home):
    conn = db.connect()
    skill_dir = fake_home / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: my-skill\n---\nbody\n")
    _insert_node(conn, "skill:my-skill", "my-skill", path=str(skill_file))

    a = audit.audit(conn)
    assert a.coverage == (1, 1)


def test_coverage_reports_gap_for_unclassified_skill_md_layout(fake_home):
    """A SKILL.md at a layout scan.py never reaches (e.g. nested under a
    plugin's `.agents/skills/`) must show up as found-but-not-classified,
    not silently vanish from the count."""
    conn = db.connect()
    classified_dir = fake_home / "skills" / "known-skill"
    classified_dir.mkdir(parents=True)
    classified_file = classified_dir / "SKILL.md"
    classified_file.write_text("---\nname: known-skill\n---\nbody\n")
    _insert_node(conn, "skill:known-skill", "known-skill", path=str(classified_file))

    stray_dir = fake_home / "plugins" / "cache" / "mk" / "odd-plugin" / ".agents" / "skills" / "hidden"
    stray_dir.mkdir(parents=True)
    (stray_dir / "SKILL.md").write_text("---\nname: hidden\n---\nbody\n")

    a = audit.audit(conn)
    assert a.coverage == (1, 2)


def test_coverage_when_home_directory_missing_reports_zero_zero(fake_home, monkeypatch):
    conn = db.connect()
    monkeypatch.setenv("HARNESS_HOME", str(fake_home / "does-not-exist"))
    a = audit.audit(conn)
    assert a.coverage == (0, 0)


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_render_includes_headline_and_bucket_line(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a", state="live", bucket="always", est_tokens=1000)
    _insert_node(conn, "skill:b", "b", state="live", bucket="rarely", est_tokens=2000)

    a = audit.audit(conn)
    out = audit.render(a)
    assert "Always-loaded index: ~3,000 tokens per turn" in out
    assert "Buckets: always=1, rarely=1, sometimes=0" in out


def test_render_always_prints_coverage_line_even_when_clean(fake_home):
    conn = db.connect()
    a = audit.audit(conn)
    out = audit.render(a)
    assert "Coverage:" in out


def test_render_includes_mixed_plugin_line(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:p@mk:used", "used", provider_plugin="p", marketplace="mk", est_tokens=100)
    _insert_usage(conn, "skill:p@mk:used", 1)
    _insert_node(conn, "skill:p@mk:unused", "unused", provider_plugin="p", marketplace="mk", est_tokens=200)

    a = audit.audit(conn)
    out = audit.render(a)
    assert "p@mk: 1/2 used, ~200 tok stuck" in out


def test_render_includes_duplicates_line(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "dup-name")
    _insert_node(conn, "skill:b", "dup-name", provider_plugin="p", marketplace="mk")

    a = audit.audit(conn)
    out = audit.render(a)
    assert "dup-name" in out


def test_render_omits_mixed_and_duplicates_sections_when_empty(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")

    a = audit.audit(conn)
    out = audit.render(a)
    assert "Mixed plugins" not in out
    assert "Duplicates" not in out


# ---------------------------------------------------------------------------
# Golden database validation
# ---------------------------------------------------------------------------

GOLDEN_DB = os.path.expanduser("~/Documents/Code/harness-recovery/harness-original.db")
pytestmark_golden = pytest.mark.skipif(
    not os.path.exists(GOLDEN_DB), reason="golden database not present on this machine"
)


def _golden_conn():
    conn = sqlite3.connect(f"file:{GOLDEN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def golden_safe_home(tmp_path, monkeypatch):
    """`audit()` also walks the filesystem for `coverage` (via
    paths.claude_home()) -- these golden-db tests must NOT let that fall
    through to the operator's real ~/.claude just because the golden-db
    node rows happen to carry real ~/.claude paths from before the loss.
    Point HARNESS_HOME at an empty scratch dir so coverage reads nothing
    real; the golden connection itself is opened separately, unaffected."""
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "not-real-claude-home"))


@pytestmark_golden
def test_golden_db_audit_runs_and_reports_the_documented_bucket_split(golden_safe_home):
    a = audit.audit(_golden_conn())
    # These bucket counts are the ones documented in the design notes /
    # the task spec as measured on the real machine.
    assert a.by_bucket == {"always": 17, "sometimes": 5, "rarely": 186}


@pytestmark_golden
def test_golden_db_marketing_skills_plugin_is_mixed_with_documented_figures(golden_safe_home):
    a = audit.audit(_golden_conn())
    mp = next(m for m in a.mixed_plugins if m["label"] == "marketing-skills@marketingskills")
    assert mp["used"] == 2
    assert mp["total"] == 49
    assert mp["tokens_stuck"] == 8261


@pytestmark_golden
def test_golden_db_render_does_not_raise(golden_safe_home):
    a = audit.audit(_golden_conn())
    out = audit.render(a)
    assert "Always-loaded index" in out
