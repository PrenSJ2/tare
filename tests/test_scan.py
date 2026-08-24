"""Tests for scan.py: filesystem -> nodes rows.

This file was the most defect-prone in the previous build -- five separate
rounds of fixes, each found only after the previous shipped. Most tests
here are named after one of the ten numbered rules in the task brief /
a real defect rather than after a feature, because the failure mode
each rule prevents is the point, not the happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tare import db, paths, scan, vault


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


def make_plugin_skill(
    base: Path, marketplace: str, plugin: str, relpath: str, *, name: str | None = None, version: str = "1.0.0"
) -> Path:
    """Build a plugin skill at the REAL cache layout, confirmed against the
    pre-loss golden database: `<marketplace>/<plugin>/<version>/skills/
    <relpath>/SKILL.md` -- e.g.
    `ExampleMarket/toolkit/0.1.0/skills/asset/SKILL.md`. `relpath` is
    relative to that `skills/` directory, not to the plugin directory.
    """
    d = base / "plugins" / "cache" / marketplace / plugin / version / "skills" / relpath
    d.mkdir(parents=True)
    fm_name = name if name is not None else Path(relpath).name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: A plugin skill.\n---\n\nBody\n")
    return d


def nodes(conn) -> dict:
    return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM nodes")}


def snapshot(conn) -> set:
    """(id, origin, state, est_tokens) for every node -- the idempotence
    contract the task brief asks to verify explicitly."""
    return {
        (r["id"], r["origin"], r["state"], r["est_tokens"])
        for r in conn.execute("SELECT id, origin, state, est_tokens FROM nodes")
    }


def full_scan(conn) -> None:
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)
    scan.scan_agents(conn)
    scan.scan_plugin_skills(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Basic scanning behaviour
# ---------------------------------------------------------------------------


def test_scan_user_skills_inserts_a_node_per_directory(fake_home):
    make_skill(fake_home, "alpha")
    make_skill(fake_home, "beta", frontmatter_name="beta-declared")
    conn = db.connect()
    count = scan.scan_user_skills(conn)
    conn.commit()
    assert count == 2
    got = nodes(conn)
    assert "skill:alpha" in got
    assert got["skill:alpha"]["origin"] == "user-authored"
    assert got["skill:alpha"]["state"] == "live"
    assert "skill:beta-declared" in got
    assert got["skill:beta-declared"]["name"] == "beta-declared"


def test_scan_user_skills_falls_back_to_filesystem_name_on_no_frontmatter(fake_home):
    d = fake_home / "skills" / "gamma"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("not frontmatter at all\n")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:gamma" in got
    assert got["skill:gamma"]["parse_error"] is not None


def test_scan_agents_inserts_a_node_per_file(fake_home):
    make_agent(fake_home, "reviewer")
    make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer")
    conn = db.connect()
    count = scan.scan_agents(conn)
    conn.commit()
    assert count == 2
    got = nodes(conn)
    assert "agent:reviewer" in got
    assert "agent:architect-reviewer" in got  # frontmatter name wins over filename


def test_scan_plugin_skills_builds_plugin_scoped_id(fake_home):
    make_plugin_skill(fake_home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    conn = db.connect()
    count = scan.scan_plugin_skills(conn)
    conn.commit()
    assert count == 1
    got = nodes(conn)
    assert "skill:superpowers@anthropics:brainstorming" in got
    row = got["skill:superpowers@anthropics:brainstorming"]
    assert row["origin"] == "plugin"
    assert row["state"] == "live"
    assert row["provider_plugin"] == "superpowers"
    assert row["marketplace"] == "anthropics"
    assert row["name"] == "brainstorming"


def test_scan_plugin_skills_marks_disabled_plugin(fake_home):
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": False}}))
    conn = db.connect()
    scan.scan_plugin_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert got["skill:acme-plugin@acme-market:widget"]["state"] == "plugin-disabled"


def test_scan_plugin_skills_defaults_to_enabled_when_unmentioned(fake_home):
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"some-other@thing": False}}))
    conn = db.connect()
    scan.scan_plugin_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert got["skill:acme-plugin@acme-market:widget"]["state"] == "live"


def test_scan_plugin_skills_with_no_settings_file_defaults_enabled(fake_home):
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    conn = db.connect()
    scan.scan_plugin_skills(conn)
    conn.commit()
    assert nodes(conn)["skill:acme-plugin@acme-market:widget"]["state"] == "live"


# ---------------------------------------------------------------------------
# Rule 1: vault-aware pruning
# ---------------------------------------------------------------------------


def test_prune_deletes_node_whose_file_is_gone(fake_home):
    make_skill(fake_home, "ephemeral")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    assert "skill:ephemeral" in nodes(conn)

    import shutil

    shutil.rmtree(fake_home / "skills" / "ephemeral")
    scan.scan_user_skills(conn)
    conn.commit()
    assert "skill:ephemeral" not in nodes(conn)


def test_prune_protects_a_stashed_node_on_the_transitional_scan(fake_home):
    """The defect this rule exists for: right after `vault.stash` moves a
    skill's files away, the node row still reads state='live' because
    scan_vaulted has not run yet this pass. A prune guarded only on
    `state != 'vaulted'` would delete it here -- and cascade the loss of
    its usage history via mine.py's name-to-id matching. Consulting the
    manifest directly must protect it regardless.
    """
    make_skill(fake_home, "precious")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    conn.execute("INSERT INTO usage (node_id, invocations, sessions) VALUES ('skill:precious', 7, 3)")
    conn.commit()

    vault.stash(fake_home / "skills" / "precious", "skills")

    # Row still reads 'live' -- scan_vaulted has not run this pass.
    assert nodes(conn)["skill:precious"]["state"] == "live"

    scan.scan_user_skills(conn)  # the prune that must NOT delete it
    conn.commit()

    assert "skill:precious" in nodes(conn)
    usage = conn.execute("SELECT invocations FROM usage WHERE node_id = 'skill:precious'").fetchone()
    assert usage is not None and usage["invocations"] == 7


def test_prune_scoped_by_origin_and_state_not_id_shape(fake_home):
    """A previous build scoped the prune with `id LIKE '%@%'` and would
    have deleted a legitimate user skill literally named `vendor@thing`.
    """
    make_skill(fake_home, "vendor@thing")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    assert "skill:vendor@thing" in nodes(conn)

    scan.scan_user_skills(conn)  # second pass, file still present
    conn.commit()
    assert "skill:vendor@thing" in nodes(conn)


def test_protected_ids_built_before_prune_so_corrupt_manifest_aborts_cleanly(fake_home):
    make_skill(fake_home, "alpha")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()

    vault.ensure_vault()
    (paths.vault_dir() / "manifest.json").write_text("{not valid json")

    import shutil

    shutil.rmtree(fake_home / "skills" / "alpha")  # would otherwise be pruned

    with pytest.raises(ValueError):
        scan.scan_user_skills(conn)
    conn.rollback()

    # Nothing committed -- the node from before the corrupt-manifest call
    # is still exactly as it was, not silently deleted.
    assert "skill:alpha" in nodes(conn)


# ---------------------------------------------------------------------------
# Rule 2: gate every vault call on is_initialized()
# ---------------------------------------------------------------------------


def test_scan_user_skills_does_not_create_vault(fake_home):
    make_skill(fake_home, "alpha")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    assert not paths.vault_dir().exists()


def test_scan_vaulted_does_not_create_vault(fake_home):
    conn = db.connect()
    assert scan.scan_vaulted(conn) == 0
    conn.commit()
    assert not paths.vault_dir().exists()


def test_scan_agents_does_not_create_vault(fake_home):
    make_agent(fake_home, "reviewer")
    conn = db.connect()
    scan.scan_agents(conn)
    conn.commit()
    assert not paths.vault_dir().exists()


# ---------------------------------------------------------------------------
# Rule 3: shared vaulted-id derivation
# ---------------------------------------------------------------------------


def test_vaulted_id_uses_declared_frontmatter_name_not_manifest_key(fake_home):
    """Real case: agents/architect-review.md declares
    name: architect-reviewer. The manifest is keyed by filesystem name
    (architect-review); the node id must use the declared name.
    """
    make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer")
    vault.stash(fake_home / "agents" / "architect-review.md", "agents")

    conn = db.connect()
    scan.scan_vaulted(conn)
    conn.commit()
    got = nodes(conn)
    assert "agent:architect-reviewer" in got
    assert got["agent:architect-reviewer"]["state"] == "vaulted"


def test_protected_ids_match_vaulted_node_ids_exactly(fake_home):
    """If the two derivations ever drift, the protected set matches no row
    and silently protects nothing -- rule 1's bug recurs. Assert equality
    directly rather than only through end-to-end behaviour.
    """
    make_skill(fake_home, "widget", frontmatter_name="widget-declared")
    vault.stash(fake_home / "skills" / "widget", "skills")

    conn = db.connect()
    scan.scan_vaulted(conn)
    conn.commit()
    vaulted_ids = {r["id"] for r in conn.execute("SELECT id FROM nodes WHERE state = 'vaulted'")}
    assert vaulted_ids == scan._protected_ids("skills")


# ---------------------------------------------------------------------------
# Rule 4: symlink classification
# ---------------------------------------------------------------------------


def test_symlink_promoted_from_plugin_cache_keeps_plugin_scoped_id(fake_home):
    make_plugin_skill(fake_home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    (fake_home / "skills" / "brainstorming").symlink_to(
        fake_home / "plugins" / "cache" / "anthropics" / "superpowers" / "1.0.0" / "skills" / "brainstorming",
        target_is_directory=True,
    )
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:superpowers@anthropics:brainstorming" in got
    row = got["skill:superpowers@anthropics:brainstorming"]
    assert row["origin"] == "user-authored"
    assert row["provider_plugin"] == "superpowers"
    assert row["marketplace"] == "anthropics"


def test_promote_shelve_promote_loop_does_not_recur(fake_home):
    """The exact failure the promoted-id rule exists to prevent: once
    promoted, scan_plugin_skills must not re-claim the id and flip it back
    to plugin-disabled on the next scan.
    """
    make_plugin_skill(fake_home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    (fake_home / "skills" / "brainstorming").symlink_to(
        fake_home / "plugins" / "cache" / "anthropics" / "superpowers" / "1.0.0" / "skills" / "brainstorming",
        target_is_directory=True,
    )
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"superpowers@anthropics": False}}))

    conn = db.connect()
    scan.scan_user_skills(conn)
    scan.scan_plugin_skills(conn)
    conn.commit()

    row = nodes(conn)["skill:superpowers@anthropics:brainstorming"]
    assert row["origin"] == "user-authored"
    assert row["state"] == "live"


def test_symlink_restored_from_vault_is_user_authored(fake_home):
    make_skill(fake_home, "restored-one")
    vault.stash(fake_home / "skills" / "restored-one", "skills")
    vault.restore("restored-one", "skills")

    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    row = nodes(conn)["skill:restored-one"]
    assert row["origin"] == "user-authored"
    assert row["state"] == "live"


def test_symlink_external_tool_classified_correctly(fake_home):
    real = fake_home / "tools" / "agent-browser"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: agent-browser\ndescription: Browser automation.\n---\n")
    (fake_home / "skills" / "agent-browser").symlink_to(real, target_is_directory=True)

    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    row = nodes(conn)["skill:agent-browser"]
    assert row["origin"] == "external-tool"


def test_symlink_dangling_still_emits_a_node(fake_home):
    (fake_home / "skills" / "gone").symlink_to(fake_home / "skills" / "does-not-exist", target_is_directory=True)
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:gone" in got
    assert got["skill:gone"]["origin"] == "external-tool"
    assert got["skill:gone"]["parse_error"] == "dangling symlink"


def test_dangling_promoted_symlink_keeps_plugin_scoped_id(fake_home):
    """A promoted skill's plugin version directory can be removed later by
    `/plugin update` -- the symlink in skills_dir() then dangles, but
    `resolve(strict=False)` still reports the theoretical target inside the
    plugin cache, so classification must run BEFORE the existence check
    (rule 4 correction). Losing the plugin-scoped id here is the promote ->
    shelve loop again, just triggered by a missing target.
    """
    target = fake_home / "plugins" / "cache" / "anthropics" / "superpowers" / "1.0.0" / "skills" / "brainstorming"
    (fake_home / "skills" / "brainstorming").symlink_to(target, target_is_directory=True)
    # target never created -- dangling from the start
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:superpowers@anthropics:brainstorming" in got
    row = got["skill:superpowers@anthropics:brainstorming"]
    assert row["origin"] == "user-authored"
    assert row["provider_plugin"] == "superpowers"
    assert row["marketplace"] == "anthropics"
    assert row["parse_error"] == "dangling symlink"
    assert row["desc_raw"] == ""


def test_dangling_restored_symlink_stays_user_authored(fake_home):
    """A dangling symlink into the vault (e.g. someone deleted the vault
    copy out from under a restore) must still classify as 'user-authored',
    not 'external-tool' (rule 4 correction).
    """
    vault.ensure_vault()
    target = paths.vault_dir() / "skills" / "phantom"
    (fake_home / "skills" / "phantom").symlink_to(target, target_is_directory=True)
    # target never actually created in the vault -- dangling
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:phantom" in got
    row = got["skill:phantom"]
    assert row["origin"] == "user-authored"
    assert row["parse_error"] == "dangling symlink"


def test_symlink_relative_and_chained_resolves_correctly(fake_home):
    real = fake_home / "tools" / "find-skills"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: find-skills\ndescription: Finds skills.\n---\n")
    link1 = fake_home / "skills" / "_find-skills-inner"
    link1.symlink_to(Path("../tools/find-skills"), target_is_directory=True)
    link2 = fake_home / "skills" / "find-skills"
    link2.symlink_to(Path("_find-skills-inner"), target_is_directory=True)

    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert got["skill:find-skills"]["origin"] == "external-tool"


def test_dangling_symlink_agent(fake_home):
    (fake_home / "agents" / "ghost.md").symlink_to(fake_home / "agents" / "nope.md")
    conn = db.connect()
    scan.scan_agents(conn)
    conn.commit()
    got = nodes(conn)
    assert "agent:ghost" in got
    assert got["agent:ghost"]["parse_error"] == "dangling symlink"


# ---------------------------------------------------------------------------
# Rule 5: scan_vaulted skips restored: True entries
# ---------------------------------------------------------------------------


def test_scan_vaulted_skips_restored_entries(fake_home):
    make_skill(fake_home, "restored-two")
    vault.stash(fake_home / "skills" / "restored-two", "skills")
    vault.restore("restored-two", "skills")

    conn = db.connect()
    scan.scan_vaulted(conn)
    conn.commit()
    assert "skill:restored-two" not in nodes(conn)


def test_scan_vaulted_includes_non_restored_entries(fake_home):
    make_skill(fake_home, "still-shelved")
    vault.stash(fake_home / "skills" / "still-shelved", "skills")

    conn = db.connect()
    scan.scan_vaulted(conn)
    conn.commit()
    got = nodes(conn)
    assert got["skill:still-shelved"]["state"] == "vaulted"


# ---------------------------------------------------------------------------
# Rule 6: scan_plugin_skills must not re-claim a promoted node
# ---------------------------------------------------------------------------


def test_scan_plugin_skills_skips_id_already_claimed_as_promoted(fake_home):
    make_plugin_skill(fake_home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    (fake_home / "skills" / "brainstorming").symlink_to(
        fake_home / "plugins" / "cache" / "anthropics" / "superpowers" / "1.0.0" / "skills" / "brainstorming",
        target_is_directory=True,
    )
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    before = nodes(conn)["skill:superpowers@anthropics:brainstorming"]

    scan.scan_plugin_skills(conn)
    conn.commit()
    after = nodes(conn)["skill:superpowers@anthropics:brainstorming"]
    assert after["origin"] == "user-authored" == before["origin"]
    assert after["state"] == "live"


# ---------------------------------------------------------------------------
# Rule 7: rglob + sub-document skipping
# ---------------------------------------------------------------------------


def test_plugin_skill_deeply_nested_is_found(fake_home):
    """Real shape from the golden database:
    `swmansion/skills/0.1.0/skills/detour/migrate-to-detour/SKILL.md` --
    marketplace=swmansion, plugin=skills, version=0.1.0, relpath under
    `skills/` is `detour/migrate-to-detour`. This is the case a plain glob
    missed 22 times in the previous build.
    """
    make_plugin_skill(
        fake_home, "swmansion", "skills", "detour/migrate-to-detour", name="migrate-to-detour", version="0.1.0"
    )
    conn = db.connect()
    count = scan.scan_plugin_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert count == 1
    assert "skill:skills@swmansion:detour/migrate-to-detour" in got


def test_plugin_version_and_skills_segment_stripped_from_id(fake_home):
    """Real shape from the golden database:
    `ExampleMarket/toolkit/0.1.0/skills/asset/SKILL.md` ->
    `skill:toolkit@ExampleMarket:asset`, not
    `skill:toolkit@ExampleMarket:0.1.0/skills/asset`.
    """
    make_plugin_skill(fake_home, "ExampleMarket", "toolkit", "asset", name="asset", version="0.1.0")
    conn = db.connect()
    scan.scan_plugin_skills(conn)
    conn.commit()
    got = nodes(conn)
    assert "skill:toolkit@ExampleMarket:asset" in got
    assert got["skill:toolkit@ExampleMarket:asset"]["path"].endswith(
        "/plugins/cache/ExampleMarket/toolkit/0.1.0/skills/asset/SKILL.md"
    )


def test_plugin_unknown_version_literal_is_handled(fake_home):
    """Real shape from the golden database: a plugin shipped without
    version metadata gets a literal "unknown" version directory --
    `claude-plugins-official/frontend-design/unknown/skills/frontend-design/SKILL.md`.
    """
    make_plugin_skill(
        fake_home, "claude-plugins-official", "frontend-design", "frontend-design", name="frontend-design",
        version="unknown",
    )
    conn = db.connect()
    scan.scan_plugin_skills(conn)
    conn.commit()
    assert "skill:frontend-design@claude-plugins-official:frontend-design" in nodes(conn)


def test_plugin_multiple_version_dirs_uses_highest_numeric(fake_home):
    """A plugin may have more than one version directory present (e.g. left
    behind by `/plugin update`) -- only the highest-versioned one is
    scanned, and dotted numeric comparison must not regress to plain string
    sort (which would rank "0.10.0" below "0.9.0").
    """
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget", name="widget-old", version="0.9.0")
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget", name="widget-new", version="0.10.0")
    conn = db.connect()
    count = scan.scan_plugin_skills(conn)
    conn.commit()
    assert count == 1
    got = nodes(conn)["skill:acme-plugin@acme-market:widget"]
    assert got["name"] == "widget-new"
    assert "/0.10.0/skills/" in got["path"]


def test_plugin_nested_reference_skill_md_is_skipped_as_sub_document(fake_home):
    top = make_plugin_skill(fake_home, "acme-market", "acme-plugin", "top-skill", name="top-skill")
    ref = top / "references" / "sub-tool"
    ref.mkdir(parents=True)
    (ref / "SKILL.md").write_text("---\nname: sub-tool\ndescription: A reference doc.\n---\n")

    conn = db.connect()
    count = scan.scan_plugin_skills(conn)
    conn.commit()
    assert count == 1
    got = nodes(conn)
    assert "skill:acme-plugin@acme-market:top-skill" in got
    assert not any("sub-tool" in i for i in got)


# ---------------------------------------------------------------------------
# Rule 8: clear derived columns on file loss
# ---------------------------------------------------------------------------


def test_derived_columns_cleared_when_file_disappears(fake_home):
    make_skill(fake_home, "fading")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    conn.execute(
        "UPDATE nodes SET purpose_line=?, when_to_use=?, tags=?, tag_source=?, content_hash=? WHERE id='skill:fading'",
        ("Purpose.", "When to use.", "a,b", "llm", "deadbeef"),
    )
    conn.commit()

    import shutil

    shutil.rmtree(fake_home / "skills" / "fading")
    (fake_home / "skills" / "fading").symlink_to(fake_home / "skills" / "nonexistent-target")
    scan.scan_user_skills(conn)
    conn.commit()

    row = nodes(conn)["skill:fading"]
    assert row["purpose_line"] == ""
    assert row["when_to_use"] == ""
    assert row["tags"] == ""
    assert row["tag_source"] is None
    assert row["content_hash"] is None
    assert row["desc_raw"] == ""


def test_derived_columns_preserved_when_file_is_fine(fake_home):
    make_skill(fake_home, "steady")
    conn = db.connect()
    scan.scan_user_skills(conn)
    conn.commit()
    conn.execute(
        "UPDATE nodes SET purpose_line=?, tags=? WHERE id='skill:steady'",
        ("Purpose.", "a,b"),
    )
    conn.commit()

    scan.scan_user_skills(conn)  # file still present and readable
    conn.commit()

    row = nodes(conn)["skill:steady"]
    assert row["purpose_line"] == "Purpose."
    assert row["tags"] == "a,b"


# ---------------------------------------------------------------------------
# Rule 9: duplicate frontmatter names
# ---------------------------------------------------------------------------


def test_duplicate_frontmatter_name_disambiguated_not_lost(fake_home):
    make_skill(fake_home, "aaa-first", frontmatter_name="shared-name")
    make_skill(fake_home, "zzz-second", frontmatter_name="shared-name")

    conn = db.connect()
    count = scan.scan_user_skills(conn)
    conn.commit()
    assert count == 2
    got = nodes(conn)
    assert "skill:shared-name" in got  # winner: sorts first alphabetically
    assert got["skill:shared-name"]["parse_error"] is None
    # loser: disambiguated, still present, still findable
    loser_ids = [i for i in got if i != "skill:shared-name" and got[i]["name"] == "shared-name"]
    assert len(loser_ids) == 1
    assert got[loser_ids[0]]["parse_error"] is not None
    assert "duplicate" in got[loser_ids[0]]["parse_error"]


# ---------------------------------------------------------------------------
# Rule 10: errors="replace"
# ---------------------------------------------------------------------------


def test_invalid_utf8_in_skill_file_does_not_raise(fake_home):
    d = fake_home / "skills" / "badbytes"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(b"---\nname: badbytes\ndescription: \xff\xfe bad bytes.\n---\n")
    conn = db.connect()
    scan.scan_user_skills(conn)  # must not raise
    conn.commit()
    assert "skill:badbytes" in nodes(conn)


def test_invalid_utf8_in_agent_file_does_not_raise(fake_home):
    (fake_home / "agents" / "badbytes.md").write_bytes(b"---\nname: badbytes\ndescription: \xff\xfe bad.\n---\n")
    conn = db.connect()
    scan.scan_agents(conn)
    conn.commit()
    assert "agent:badbytes" in nodes(conn)


# ---------------------------------------------------------------------------
# Idempotence / order-independence
# ---------------------------------------------------------------------------


def test_full_scan_twice_is_byte_identical(fake_home):
    make_skill(fake_home, "alpha")
    make_skill(fake_home, "beta", frontmatter_name="beta-declared")
    make_agent(fake_home, "reviewer")
    make_plugin_skill(fake_home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    make_skill(fake_home, "shelved-one")

    conn = db.connect()
    full_scan(conn)
    vault.stash(fake_home / "skills" / "shelved-one", "skills")
    full_scan(conn)
    first = snapshot(conn)

    full_scan(conn)
    second = snapshot(conn)

    assert first == second


def _build_home(base: Path) -> Path:
    home = base / "claude"
    (home / "skills").mkdir(parents=True)
    (home / "agents").mkdir(parents=True)
    (home / "plugins" / "cache").mkdir(parents=True)
    (home / "plugins" / "marketplaces").mkdir(parents=True)
    (home / "projects").mkdir(parents=True)

    make_skill(home, "alpha")
    make_agent(home, "reviewer")
    make_plugin_skill(home, "anthropics", "superpowers", "brainstorming", name="brainstorming")
    (home / "skills" / "brainstorming").symlink_to(
        home / "plugins" / "cache" / "anthropics" / "superpowers" / "1.0.0" / "skills" / "brainstorming",
        target_is_directory=True,
    )
    make_skill(home, "shelved-two")
    return home


def test_scan_order_independent(tmp_path, monkeypatch):
    """Two structurally identical homes, scanned in opposite orders (and
    each scanned twice, with a stash in between): the resulting graphs must
    be identical regardless of which scanner ran first.
    """
    home_a = _build_home(tmp_path / "a")
    monkeypatch.setenv("TARE_HOME", str(home_a))
    conn_a = db.connect()
    scan.scan_vaulted(conn_a)
    scan.scan_user_skills(conn_a)
    scan.scan_agents(conn_a)
    scan.scan_plugin_skills(conn_a)
    vault.stash(home_a / "skills" / "shelved-two", "skills")
    scan.scan_vaulted(conn_a)
    scan.scan_user_skills(conn_a)
    scan.scan_agents(conn_a)
    scan.scan_plugin_skills(conn_a)
    conn_a.commit()
    snap_a = snapshot(conn_a)
    conn_a.close()

    home_b = _build_home(tmp_path / "b")
    monkeypatch.setenv("TARE_HOME", str(home_b))
    conn_b = db.connect()
    scan.scan_plugin_skills(conn_b)
    scan.scan_agents(conn_b)
    scan.scan_user_skills(conn_b)
    scan.scan_vaulted(conn_b)
    vault.stash(home_b / "skills" / "shelved-two", "skills")
    scan.scan_plugin_skills(conn_b)
    scan.scan_agents(conn_b)
    scan.scan_user_skills(conn_b)
    scan.scan_vaulted(conn_b)
    conn_b.commit()
    snap_b = snapshot(conn_b)
    conn_b.close()

    assert snap_a == snap_b


def test_scan_results_survive_reopening_the_database(fake_home):
    """Every scanner must commit.

    This is the test the suite was missing: uncommitted writes are visible on
    the SAME connection, so a scan that never commits looks perfectly correct
    to every other test here -- while writing nothing at all to disk. Found by
    running against a real configuration, where scan reported "63 agents" and
    the database was completely unchanged.
    """
    from tare import db, scan

    d = fake_home / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: alpha\ndescription: x.\n---\n\nBody.\n")
    (fake_home / "agents" / "beta.md").write_text("---\nname: beta\ndescription: y.\n---\n\nBody.\n")

    conn = db.connect()
    scan.scan_user_skills(conn)
    scan.scan_agents(conn)
    scan.scan_plugin_skills(conn)
    scan.scan_vaulted(conn)
    conn.close()

    fresh = db.connect()
    ids = {r["id"] for r in fresh.execute("SELECT id FROM nodes")}
    assert "skill:alpha" in ids, "scan_user_skills did not commit"
    assert "agent:beta" in ids, "scan_agents did not commit"


def test_a_previously_vaulted_node_returns_to_live_once_its_file_is_back(fake_home):
    """State must follow the filesystem, not linger from a previous run."""
    from tare import db, scan

    conn = db.connect()
    conn.execute(
        "INSERT INTO nodes (id, kind, name, origin, state, path) "
        "VALUES ('agent:gamma','agent','gamma','user-authored','vaulted','/gone/gamma.md')"
    )
    conn.commit()

    (fake_home / "agents" / "gamma.md").write_text("---\nname: gamma\ndescription: z.\n---\n\nBody.\n")
    scan.scan_agents(conn)
    conn.close()

    fresh = db.connect()
    row = fresh.execute("SELECT state, path FROM nodes WHERE id='agent:gamma'").fetchone()
    assert row["state"] == "live"
    assert row["path"].endswith("agents/gamma.md")
