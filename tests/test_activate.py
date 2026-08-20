"""Tests for activate.py: the way back from the vault, and from a disabled
plugin.

Named after the rules in the task brief / the design notes: the
ambiguity and name-divergence defects that made the previous build's
version of this file permanently strand real capabilities.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import activate, db, paths, scan, vault


def make_skill(base: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    d = base / "skills" / name
    d.mkdir(parents=True)
    fm_name = name if frontmatter_name is None else frontmatter_name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: Does a thing.\n---\n\nBody\n")
    return d


def make_agent(base: Path, filename: str, *, frontmatter_name: str | None = None) -> Path:
    p = base / "agents" / f"{filename}.md"
    fm_name = filename if frontmatter_name is None else frontmatter_name
    p.write_text(f"---\nname: {fm_name}\ndescription: Reviews things.\n---\n\nBody\n")
    return p


def make_plugin_skill(base: Path, marketplace: str, plugin: str, relpath: str, *, name: str | None = None, version: str = "1.0.0") -> Path:
    d = base / "plugins" / "cache" / marketplace / plugin / version / "skills" / relpath
    d.mkdir(parents=True)
    fm_name = name if name is not None else Path(relpath).name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: A plugin skill.\n---\n\nBody\n")
    return d


def full_scan(conn) -> None:
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)
    scan.scan_agents(conn)
    scan.scan_plugin_skills(conn)
    conn.commit()


def node(conn, node_id):
    return conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


# ---------------------------------------------------------------------------
# activate() -- vaulted user capability
# ---------------------------------------------------------------------------


def test_activate_restores_vaulted_skill_by_name(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    conn = db.connect()
    full_scan(conn)
    assert node(conn, "skill:foo")["state"] == "vaulted"

    result = activate.activate(conn, "foo")

    assert result["ok"] is True
    assert result["action"] == "restore"
    assert result["id"] == "skill:foo"
    assert (paths.skills_dir() / "foo").is_symlink()
    assert node(conn, "skill:foo")["state"] == "live"


def test_activate_restores_vaulted_agent_by_id(fake_home):
    vault.stash(make_agent(fake_home, "reviewer"), "agents")
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "agent:reviewer")
    assert result["ok"] is True
    assert node(conn, "agent:reviewer")["state"] == "live"


def test_activate_on_already_live_node_is_a_noop(fake_home):
    make_skill(fake_home, "alpha")
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "alpha")
    assert result["ok"] is True
    assert result["action"] == "none"
    assert "already live" in result["message"]


def test_activate_resolves_declared_name_diverging_from_filesystem_key(fake_home):
    # Real case from the design notes: agents/architect-review.md declares
    # name: architect-reviewer. The vault manifest key is "architect-review"
    # (filesystem stem); the operator and the graph both use the declared
    # name. Both must work.
    vault.stash(make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer"), "agents")
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "architect-reviewer")
    assert result["ok"] is True
    assert node(conn, "agent:architect-reviewer")["state"] == "live"


def test_activate_finds_genuinely_vaulted_capability_before_graph_scan(fake_home):
    # rule 1: resolving through vault.resolve_name must work even when the
    # graph hasn't been scanned yet (no node row exists at all) -- this must
    # not be misreported as "no such plugin" either.
    vault.stash(make_skill(fake_home, "foo"), "skills")
    conn = db.connect()  # deliberately no scan

    result = activate.activate(conn, "foo")
    assert result["ok"] is True
    assert (paths.skills_dir() / "foo").is_symlink()


# ---------------------------------------------------------------------------
# activate() -- ambiguity (rule 2)
# ---------------------------------------------------------------------------


def test_activate_raises_lookuperror_when_name_matches_skill_and_agent(fake_home):
    vault.stash(make_skill(fake_home, "shared"), "skills")
    vault.stash(make_agent(fake_home, "shared"), "agents")
    conn = db.connect()
    full_scan(conn)

    with pytest.raises(LookupError) as excinfo:
        activate.activate(conn, "shared")
    message = str(excinfo.value)
    assert "skill:shared" in message and "agent:shared" in message


def test_activate_by_id_resolves_the_ambiguity(fake_home):
    vault.stash(make_skill(fake_home, "shared"), "skills")
    vault.stash(make_agent(fake_home, "shared"), "agents")
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "skill:shared")
    assert result["ok"] is True
    assert result["id"] == "skill:shared"
    # The other node, sharing the name, must remain untouched and vaulted.
    assert node(conn, "agent:shared")["state"] == "vaulted"


def test_activate_vaulted_node_sharing_name_with_live_node_is_reachable_by_id(fake_home):
    # The exact previous-build bug: a vaulted node shares a name with a live
    # one. Activating by name must raise ambiguity (never silently pick the
    # live one and say "already live" about the wrong node) -- and the
    # vaulted one must be reachable by id. Using a plugin-provided live skill
    # here (rather than a user-authored one) keeps the two nodes' ids
    # distinct -- a live user-authored skill named "dup" and a vaulted entry
    # whose declared name is also "dup" would collide on the very same id
    # (`skill:dup`), which is a pre-existing scan.py id-collision case, not
    # the graph-level *name* ambiguity this test targets.
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "dup", name="dup")
    conn = db.connect()
    full_scan(conn)
    live_id = "skill:acme-plugin@acme-market:dup"
    assert node(conn, live_id)["state"] == "live"

    other = make_skill(fake_home, "dup-vaulted-source", frontmatter_name="dup")
    vault.stash(other, "skills")
    full_scan(conn)

    rows = conn.execute("SELECT id, state FROM nodes WHERE name = 'dup'").fetchall()
    assert len(rows) == 2

    with pytest.raises(LookupError):
        activate.activate(conn, "dup")

    vaulted_id = next(r["id"] for r in rows if r["state"] == "vaulted")
    result = activate.activate(conn, vaulted_id)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# activate() -- disabled plugin, and un-promotion (rule 4)
# ---------------------------------------------------------------------------


def test_activate_reenables_disabled_plugin(fake_home):
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": False}}))
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "acme-plugin")
    assert result["ok"] is True
    assert result["action"] == "enable-plugin"
    assert result["id"] == "acme-plugin@acme-market"

    data = json.loads(paths.settings_path().read_text())
    assert data["enabledPlugins"]["acme-plugin@acme-market"] is True


def test_activate_plugin_ambiguous_across_marketplaces_requires_exact_key(fake_home):
    settings = {"enabledPlugins": {"acme-plugin@market-a": False, "acme-plugin@market-b": False}}
    paths.settings_path().write_text(json.dumps(settings))
    conn = db.connect()

    with pytest.raises(LookupError):
        activate.activate(conn, "acme-plugin")

    result = activate.activate(conn, "acme-plugin@market-a")
    assert result["ok"] is True
    assert result["id"] == "acme-plugin@market-a"


def test_activate_reports_no_match_for_unknown_name(fake_home):
    conn = db.connect()
    result = activate.activate(conn, "nothing-like-this-exists")
    assert result["ok"] is False
    assert "does not match" in result["message"]


def test_activate_reenabling_plugin_unpromotes_symlinked_skill(fake_home):
    # A promoted skill: a symlink in skills_dir() to the plugin cache,
    # origin='user-authored', provider_plugin/marketplace set. Re-enabling
    # the plugin must remove the symlink and flip this node back to
    # plugin-served, or the skill is served twice (rule 4).
    cache_dir = make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    symlink = paths.skills_dir() / "widget"
    symlink.symlink_to(cache_dir, target_is_directory=True)
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": False}}))

    conn = db.connect()
    full_scan(conn)
    promoted_id = "skill:acme-plugin@acme-market:widget"
    assert node(conn, promoted_id)["origin"] == "user-authored"

    result = activate.activate(conn, "acme-plugin")
    assert result["ok"] is True
    assert result["unpromoted"] == [promoted_id]
    assert "un-promoted" in result["message"]
    assert not symlink.exists()
    assert not symlink.is_symlink()
    assert node(conn, promoted_id)["origin"] == "plugin"


def test_activate_reenabling_plugin_with_no_promotion_reports_none(fake_home):
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": False}}))
    conn = db.connect()
    full_scan(conn)

    result = activate.activate(conn, "acme-plugin")
    assert result["ok"] is True
    assert result["unpromoted"] == []
    assert "un-promoted" not in result["message"]


def test_activate_unpromote_scoped_by_marketplace_not_just_plugin_name(fake_home):
    # Plugin names repeat across marketplaces -- un-promoting acme-plugin@a
    # must not touch a same-named skill promoted from acme-plugin@b.
    cache_a = make_plugin_skill(fake_home, "market-a", "acme-plugin", "widget")
    cache_b = make_plugin_skill(fake_home, "market-b", "acme-plugin", "widget")
    link_a = paths.skills_dir() / "widget-a"
    link_a.symlink_to(cache_a, target_is_directory=True)
    # Manually register both as promoted via a scan pass with distinct symlink names.
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@market-a": False, "acme-plugin@market-b": False}}))

    conn = db.connect()
    full_scan(conn)
    id_a = "skill:acme-plugin@market-a:widget"
    id_b = "skill:acme-plugin@market-b:widget"
    # id_b was never promoted (no symlink for it), only scanned as plugin-disabled.
    assert node(conn, id_b)["origin"] == "plugin"

    result = activate.activate(conn, "acme-plugin@market-a")
    assert result["unpromoted"] == [id_a]
    # market-b's plugin is untouched -- still disabled.
    data = json.loads(paths.settings_path().read_text())
    assert data["enabledPlugins"]["acme-plugin@market-b"] is False


# ---------------------------------------------------------------------------
# deactivate()
# ---------------------------------------------------------------------------


def test_deactivate_unrestores_live_node(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    conn = db.connect()
    full_scan(conn)
    activate.activate(conn, "foo")
    assert node(conn, "skill:foo")["state"] == "live"

    result = activate.deactivate(conn, "foo")
    assert result["ok"] is True
    assert result["action"] == "unrestore"
    assert not (paths.skills_dir() / "foo").exists()
    assert node(conn, "skill:foo")["state"] == "vaulted"


def test_deactivate_resolves_declared_name_diverging_from_filesystem_key(fake_home):
    vault.stash(make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer"), "agents")
    conn = db.connect()
    full_scan(conn)
    activate.activate(conn, "architect-reviewer")

    result = activate.deactivate(conn, "architect-reviewer")
    assert result["ok"] is True
    assert node(conn, "agent:architect-reviewer")["state"] == "vaulted"


def test_deactivate_never_vaulted_capability_reports_plainly(fake_home):
    make_skill(fake_home, "plain")
    conn = db.connect()
    full_scan(conn)

    result = activate.deactivate(conn, "plain")
    assert result["ok"] is False
    assert "not vaulted" in result["message"] or "was not restored" in result["message"]


def test_deactivate_cannot_disable_a_plugin_and_says_so(fake_home):
    # docstring rule: deactivate cannot re-disable a plugin -- must state
    # this plainly, never silently no-op.
    make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": True}}))
    conn = db.connect()
    full_scan(conn)

    result = activate.deactivate(conn, "acme-plugin")
    assert result["ok"] is False
    assert "cannot re-disable a plugin" in result["message"]

    # Definitely did not touch settings.json.
    data = json.loads(paths.settings_path().read_text())
    assert data["enabledPlugins"]["acme-plugin@acme-market"] is True


def test_deactivate_promoted_plugin_skill_cannot_disable_plugin(fake_home):
    cache_dir = make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    symlink = paths.skills_dir() / "widget"
    symlink.symlink_to(cache_dir, target_is_directory=True)
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": True}}))
    conn = db.connect()
    full_scan(conn)
    promoted_id = "skill:acme-plugin@acme-market:widget"

    result = activate.deactivate(conn, promoted_id)
    assert result["ok"] is False
    assert "plugin" in result["message"]
    # The symlink must survive -- deactivate on a promoted node is not
    # licensed to unpromote it either.
    assert symlink.is_symlink()


def test_deactivate_raises_lookuperror_on_ambiguous_name(fake_home):
    vault.stash(make_skill(fake_home, "shared"), "skills")
    vault.stash(make_agent(fake_home, "shared"), "agents")
    conn = db.connect()
    full_scan(conn)
    activate.activate(conn, "skill:shared")
    activate.activate(conn, "agent:shared")

    with pytest.raises(LookupError):
        activate.deactivate(conn, "shared")


def test_deactivate_by_id_resolves_ambiguity(fake_home):
    vault.stash(make_skill(fake_home, "shared"), "skills")
    vault.stash(make_agent(fake_home, "shared"), "agents")
    conn = db.connect()
    full_scan(conn)
    activate.activate(conn, "skill:shared")

    result = activate.deactivate(conn, "skill:shared")
    assert result["ok"] is True
    assert node(conn, "skill:shared")["state"] == "vaulted"
