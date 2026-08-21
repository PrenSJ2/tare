"""Tests for shelve.py -- the destructive end of harness.

Most tests here are named after either one of the guard's five properties
(transitivity, routes-to-only, any-origin seeding, attribution, cycle
safety) or one of the numbered requirements in the task brief /
the design notes, because the failure mode each one prevents is the
point, not the happy path.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tare import db, edges, paths, scan, shelve, vault

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_skill(base: Path, name: str, *, frontmatter_name: str | None = None, description: str = "Does a thing.", body: str = "Body\n") -> Path:
    d = base / "skills" / name
    d.mkdir(parents=True)
    fm_name = name if frontmatter_name is None else frontmatter_name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: {description}\n---\n\n{body}")
    return d


def make_agent(base: Path, filename: str, *, frontmatter_name: str | None = None, description: str = "Reviews things.") -> Path:
    p = base / "agents" / f"{filename}.md"
    fm_name = filename if frontmatter_name is None else frontmatter_name
    p.write_text(f"---\nname: {fm_name}\ndescription: {description}\n---\n\nBody\n")
    return p


def make_plugin_skill(base: Path, marketplace: str, plugin: str, relpath: str, *, name: str | None = None, version: str = "1.0.0", body: str = "Body\n") -> Path:
    d = base / "plugins" / "cache" / marketplace / plugin / version / "skills" / relpath
    d.mkdir(parents=True)
    fm_name = name if name is not None else Path(relpath).name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: A plugin skill.\n---\n\n{body}")
    return d


def mark_used(conn, node_id: str, invocations: int = 1) -> None:
    conn.execute(
        "INSERT INTO usage (node_id, invocations, sessions, last_used) VALUES (?, ?, 1, '2026-08-19T00:00:00')",
        (node_id, invocations),
    )
    conn.commit()


def seed_usage_evidence(conn) -> None:
    """A throwaway used node so `_require_usage_evidence` is satisfied in
    tests that aren't themselves about that check."""
    make_skill(paths.claude_home(), "keep-me-anchor")
    scan.scan_user_skills(conn)
    conn.commit()
    mark_used(conn, "skill:keep-me-anchor")


def _insert_node(conn, node_id, name, *, kind="skill", origin="user-authored", state="live", path=None, provider_plugin=None):
    conn.execute(
        "INSERT INTO nodes (id, kind, name, origin, state, path, provider_plugin) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (node_id, kind, name, origin, state, path, provider_plugin),
    )
    conn.commit()


def _insert_edge(conn, src, dst, edge_type):
    conn.execute(
        "INSERT INTO edges (src, dst, type, weight, evidence) VALUES (?, ?, ?, 1.0, 'test')",
        (src, dst, edge_type),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The guard: five properties
# ---------------------------------------------------------------------------


def test_guard_transitive_closure(fake_home):
    conn = db.connect()
    for n in "abcd":
        _insert_node(conn, f"skill:{n}", n)
    _insert_edge(conn, "skill:a", "skill:b", "routes-to")
    _insert_edge(conn, "skill:b", "skill:c", "routes-to")
    _insert_edge(conn, "skill:c", "skill:d", "routes-to")
    mark_used(conn, "skill:a")

    protected = shelve._protected_capabilities(conn)

    assert protected == {"skill:b": "skill:a", "skill:c": "skill:a", "skill:d": "skill:a"}


def test_guard_routes_to_only_overlaps_does_not_protect(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_edge(conn, "skill:a", "skill:b", "overlaps")
    mark_used(conn, "skill:a")

    protected = shelve._protected_capabilities(conn)

    assert protected == {}


def test_guard_seeds_from_any_origin(fake_home):
    """A used PLUGIN skill routing to a user-authored one must still
    protect it -- seeding is not restricted to user-authored origin."""
    conn = db.connect()
    _insert_node(conn, "skill:plugin-a", "plugin-a", origin="plugin", provider_plugin="acme")
    _insert_node(conn, "skill:user-b", "user-b", origin="user-authored")
    _insert_edge(conn, "skill:plugin-a", "skill:user-b", "routes-to")
    mark_used(conn, "skill:plugin-a")

    protected = shelve._protected_capabilities(conn)

    assert protected == {"skill:user-b": "skill:plugin-a"}


def test_guard_attribution_does_not_pass_through_a_used_intermediate(fake_home):
    """A(used) -> B(used) -> C: C must be attributed to B, not A. The
    previous build's seed-at-a-time walk let A's own traversal pass
    straight through B and misattribute C -- exactly the bug that produced
    32 protected capabilities all labelled with one arbitrary wrong source.
    """
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_node(conn, "skill:c", "c")
    _insert_edge(conn, "skill:a", "skill:b", "routes-to")
    _insert_edge(conn, "skill:b", "skill:c", "routes-to")
    mark_used(conn, "skill:a")
    mark_used(conn, "skill:b")

    protected = shelve._protected_capabilities(conn)

    assert protected == {"skill:c": "skill:b"}


def test_guard_cycle_does_not_hang_and_attributes_correctly(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_node(conn, "skill:b", "b")
    _insert_node(conn, "skill:c", "c")
    _insert_edge(conn, "skill:a", "skill:b", "routes-to")
    _insert_edge(conn, "skill:b", "skill:c", "routes-to")
    _insert_edge(conn, "skill:c", "skill:a", "routes-to")  # cycle back to the seed
    mark_used(conn, "skill:a")

    protected = shelve._protected_capabilities(conn)

    assert protected == {"skill:b": "skill:a", "skill:c": "skill:a"}


def test_guard_self_edge_on_a_seed_is_a_no_op(fake_home):
    conn = db.connect()
    _insert_node(conn, "skill:a", "a")
    _insert_edge(conn, "skill:a", "skill:a", "routes-to")
    mark_used(conn, "skill:a")

    protected = shelve._protected_capabilities(conn)

    assert protected == {}


# ---------------------------------------------------------------------------
# Requirement 1: nodes.path, never derived from name
# ---------------------------------------------------------------------------


def test_candidates_uses_nodes_path_when_frontmatter_name_diverges_from_filename(fake_home):
    conn = db.connect()
    make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer")
    scan.scan_agents(conn)
    conn.commit()

    cand = shelve.candidates(conn)
    entry = next(e for e in cand["agents"] if e["name"] == "architect-reviewer")

    assert entry["path"] == str(fake_home / "agents" / "architect-review.md")
    assert entry["eligible"] is True


def test_shelve_user_shelves_a_capability_whose_frontmatter_name_diverges_from_filename(fake_home):
    conn = db.connect()
    make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer")
    scan.scan_agents(conn)
    conn.commit()
    seed_usage_evidence(conn)

    results = shelve.shelve_user(conn, dry_run=False)

    entry = next(r for r in results if r["name"] == "architect-reviewer")
    assert entry["status"] == "shelved"
    assert not (fake_home / "agents" / "architect-review.md").exists()
    assert (paths.vault_dir() / "agents" / "architect-review.md").exists()


# ---------------------------------------------------------------------------
# Requirement 2: promotion collision is a failure, drops the plugin
# ---------------------------------------------------------------------------


def test_shelve_plugins_promotion_collision_drops_the_plugin_and_reports_failure(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "acme-market", "acme", "used-skill", name="used-skill", body="short\n")
    make_plugin_skill(fake_home, "acme-market", "acme", "cold-filler", name="cold-filler", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)
    used_id = "skill:acme@acme-market:used-skill"
    mark_used(conn, used_id)

    # Something else already occupies the promotion target.
    collision = fake_home / "skills" / "used-skill"
    collision.mkdir(parents=True)
    (collision / "SOMETHING").write_text("not harness's directory\n")

    result = shelve.shelve_plugins(conn, dry_run=False, floor_tokens=0)

    assert result["disabled"] == []
    assert result["promoted"] == []
    assert any(f["id"] == used_id for f in result["failed"])

    row = conn.execute("SELECT origin FROM nodes WHERE id = ?", (used_id,)).fetchone()
    assert row["origin"] == "plugin"  # never flipped over a failed promotion
    assert (collision / "SOMETHING").exists()  # untouched


# ---------------------------------------------------------------------------
# Requirement 3: dry_run defaults True, and is a faithful preview
# ---------------------------------------------------------------------------


def test_shelve_user_default_is_dry_run_and_touches_nothing(fake_home):
    conn = db.connect()
    make_skill(fake_home, "cold-skill")
    scan.scan_user_skills(conn)
    conn.commit()

    results = shelve.shelve_user(conn)  # no dry_run argument

    assert any(r["name"] == "cold-skill" and r["status"] == "would-shelve" for r in results)
    assert (fake_home / "skills" / "cold-skill").exists()
    assert not vault.is_initialized()


def test_shelve_plugins_default_is_dry_run_and_touches_nothing(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "cold-filler", name="cold-filler", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()

    result = shelve.shelve_plugins(conn, floor_tokens=0)  # no dry_run argument

    assert result["dry_run"] is True
    assert "plug@market" in result["disabled"]
    assert not paths.settings_path().exists()


def test_shelve_plugins_ownership_scope_filter_is_identical_in_both_modes(fake_home):
    """Two marketplaces both shipping a plugin literally named "toolkit"
    -- confirmed on the real machine this tool targets. Disabling one must
    never touch the other, in preview or in apply."""
    conn = db.connect()
    make_plugin_skill(fake_home, "marketA", "toolkit", "cold-one", name="cold-one", body="filler " * 400)
    make_plugin_skill(fake_home, "marketB", "toolkit", "cold-two", name="cold-two", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)

    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"toolkit@marketA": False}}))

    preview = shelve.shelve_plugins(conn, dry_run=True, floor_tokens=0)
    assert "toolkit@marketA" not in preview["disabled"]
    assert "toolkit@marketB" in preview["disabled"]

    applied = shelve.shelve_plugins(conn, dry_run=False, floor_tokens=0)
    assert "toolkit@marketA" not in applied["disabled"]
    assert "toolkit@marketB" in applied["disabled"]

    settings_after = json.loads(paths.settings_path().read_text())
    assert settings_after["enabledPlugins"]["toolkit@marketA"] is False
    assert settings_after["enabledPlugins"]["toolkit@marketB"] is False


# ---------------------------------------------------------------------------
# Requirement 4: never shelve a pinned capability
# ---------------------------------------------------------------------------


def test_candidates_marks_pinned_capability_ineligible(fake_home):
    conn = db.connect()
    make_skill(fake_home, "agent-browser")
    scan.scan_user_skills(conn)
    conn.commit()

    cand = shelve.candidates(conn)
    entry = next(e for e in cand["skills"] if e["name"] == "agent-browser")

    assert entry["eligible"] is False
    assert entry["reason"] == "pinned"


def test_shelve_user_never_shelves_a_pinned_capability(fake_home):
    conn = db.connect()
    make_skill(fake_home, "agent-browser")
    scan.scan_user_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)

    results = shelve.shelve_user(conn, dry_run=False)

    entry = next(r for r in results if r["name"] == "agent-browser")
    assert entry["status"] == "skipped"
    assert entry["reason"] == "pinned"
    assert (fake_home / "skills" / "agent-browser").exists()


def test_plugin_plan_never_lets_a_pinned_plugin_skill_go_cold(fake_home):
    """superpowers is pinned by id-component prefix -- see buckets.py."""
    conn = db.connect()
    make_plugin_skill(fake_home, "claude-plugins-official", "superpowers", "brainstorming", name="brainstorming", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()

    plan = shelve.plugin_plan(conn)

    assert plan["disable"] == []  # the only skill is pinned, so cold_tokens is 0


# ---------------------------------------------------------------------------
# Requirement 5: never shelve an already-vaulted capability
# ---------------------------------------------------------------------------


def test_candidates_excludes_a_restored_already_vaulted_capability(fake_home):
    conn = db.connect()
    make_skill(fake_home, "cold-skill")
    scan.scan_user_skills(conn)
    conn.commit()

    vault.stash(fake_home / "skills" / "cold-skill", "skills")
    vault.restore("cold-skill", "skills")
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)
    conn.commit()

    cand = shelve.candidates(conn)
    entry = next(e for e in cand["skills"] if e["name"] == "cold-skill")

    assert entry["eligible"] is False
    assert entry["reason"] == "already-vaulted"


# ---------------------------------------------------------------------------
# Requirement 6: refuse to apply with zero invocation events
# ---------------------------------------------------------------------------


def test_shelve_user_apply_refuses_with_zero_usage_events(fake_home):
    conn = db.connect()
    make_skill(fake_home, "cold-skill")
    scan.scan_user_skills(conn)
    conn.commit()

    with pytest.raises(RuntimeError, match="tare mine"):
        shelve.shelve_user(conn, dry_run=False)


def test_shelve_user_dry_run_still_lists_with_zero_usage_events(fake_home):
    conn = db.connect()
    make_skill(fake_home, "cold-skill")
    scan.scan_user_skills(conn)
    conn.commit()

    results = shelve.shelve_user(conn, dry_run=True)

    assert any(r["name"] == "cold-skill" for r in results)


def test_shelve_plugins_apply_refuses_with_zero_usage_events(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "cold-filler", name="cold-filler", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()

    with pytest.raises(RuntimeError, match="tare mine"):
        shelve.shelve_plugins(conn, dry_run=False)


# ---------------------------------------------------------------------------
# Requirement 7: per-capability error handling continues the sweep
# ---------------------------------------------------------------------------


def test_shelve_user_continues_the_sweep_after_one_failure(fake_home):
    conn = db.connect()
    make_skill(fake_home, "good-skill")
    make_skill(fake_home, "bad-skill")
    scan.scan_user_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)

    # The node still exists in the DB, but its file is gone by apply time.
    shutil.rmtree(fake_home / "skills" / "bad-skill")

    results = shelve.shelve_user(conn, dry_run=False)

    good = next(r for r in results if r["name"] == "good-skill")
    bad = next(r for r in results if r["name"] == "bad-skill")
    assert good["status"] == "shelved"
    assert bad["status"] == "failed"
    assert "error" in bad


# ---------------------------------------------------------------------------
# Requirement 8: unperformable check (failure) vs. nothing-to-do (not an error)
# ---------------------------------------------------------------------------


def test_shelve_plugins_unreadable_settings_json_is_a_failure_in_both_modes(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "cold-filler", name="cold-filler", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)

    paths.settings_path().write_text("{not valid json")

    with pytest.raises(RuntimeError):
        shelve.shelve_plugins(conn, dry_run=True)
    with pytest.raises(RuntimeError):
        shelve.shelve_plugins(conn, dry_run=False)


def test_shelve_plugins_nothing_to_disable_is_not_an_error(fake_home):
    conn = db.connect()
    seed_usage_evidence(conn)

    result = shelve.shelve_plugins(conn, dry_run=False)

    assert result["disabled"] == []
    assert result["failed"] == []


# ---------------------------------------------------------------------------
# Requirement 9: stage symlinks first, roll back if the settings write fails
# ---------------------------------------------------------------------------


def test_shelve_plugins_rolls_back_symlinks_if_settings_write_fails(fake_home, monkeypatch):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "used-one", name="used-one", body="short\n")
    make_plugin_skill(fake_home, "market", "plug", "cold-one", name="cold-one", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)
    used_id = "skill:plug@market:used-one"
    mark_used(conn, used_id)

    def boom(_data):
        raise OSError("disk full")

    monkeypatch.setattr(shelve, "_write_settings", boom)

    with pytest.raises(RuntimeError):
        shelve.shelve_plugins(conn, dry_run=False, floor_tokens=0)

    assert not (fake_home / "skills" / "used-one").exists()  # symlink rolled back
    row = conn.execute("SELECT origin FROM nodes WHERE id = ?", (used_id,)).fetchone()
    assert row["origin"] == "plugin"  # DB never touched


# ---------------------------------------------------------------------------
# plugin_plan: floor_tokens and widening promote to protected skills
# ---------------------------------------------------------------------------


def test_plugin_plan_respects_floor_tokens(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "tiny-cold", name="tiny-cold", body="x\n")
    scan.scan_plugin_skills(conn)
    conn.commit()

    assert shelve.plugin_plan(conn)["disable"] == []  # default floor, tiny skill well under it
    assert len(shelve.plugin_plan(conn, floor_tokens=0)["disable"]) == 1


def test_plugin_plan_widens_promote_to_include_guard_protected_skills(fake_home):
    conn = db.connect()
    make_plugin_skill(
        fake_home, "market", "plug", "orchestrator", name="orchestrator",
        body="Routes to helper for the hard part.\n" + "filler " * 200,
    )
    make_plugin_skill(fake_home, "market", "plug", "helper", name="helper", body="Does the hard part.\n")
    make_plugin_skill(fake_home, "market", "plug", "cold-filler", name="cold-filler", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    edges.build(conn)
    conn.commit()
    mark_used(conn, "skill:plug@market:orchestrator")

    plan = shelve.plugin_plan(conn, floor_tokens=0)

    promoted_ids = {p["id"] for p in plan["promote"]}
    assert "skill:plug@market:orchestrator" in promoted_ids
    assert "skill:plug@market:helper" in promoted_ids

    helper_entry = next(p for p in plan["promote"] if p["id"] == "skill:plug@market:helper")
    assert helper_entry["reason"] == "protected"
    assert helper_entry["protected_by"] == "orchestrator"

    cold_names = {name for d in plan["disable"] for name in d["cold_skills"]}
    assert "cold-filler" in cold_names
    assert "helper" not in cold_names


def test_shelve_plugins_apply_promotes_and_disables_end_to_end(fake_home):
    conn = db.connect()
    make_plugin_skill(fake_home, "market", "plug", "used-one", name="used-one", body="short\n")
    make_plugin_skill(fake_home, "market", "plug", "cold-one", name="cold-one", body="filler " * 400)
    scan.scan_plugin_skills(conn)
    conn.commit()
    seed_usage_evidence(conn)
    used_id = "skill:plug@market:used-one"
    mark_used(conn, used_id)

    result = shelve.shelve_plugins(conn, dry_run=False, floor_tokens=0)

    assert result["disabled"] == ["plug@market"]
    assert result["promoted"] == [used_id]
    assert result["failed"] == []

    link = fake_home / "skills" / "used-one"
    assert link.is_symlink()
    assert (link / "SKILL.md").exists()

    row = conn.execute("SELECT origin, path FROM nodes WHERE id = ?", (used_id,)).fetchone()
    assert row["origin"] == "user-authored"
    assert row["path"] == str(link / "SKILL.md")

    settings_after = json.loads(paths.settings_path().read_text())
    assert settings_after["enabledPlugins"]["plug@market"] is False
