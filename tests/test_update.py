"""Upstream drift tests."""

from __future__ import annotations

import json

from harness import db, update


def install_plugin(home, marketplace, plugin, version):
    d = home / "plugins" / "cache" / marketplace / plugin / version / "skills" / "thing"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: thing\ndescription: x.\n---\n\nBody.\n")


def declare(home, marketplace, plugins):
    d = home / "plugins" / "marketplaces" / marketplace / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(json.dumps(
        {"name": marketplace, "plugins": [{"name": n, "version": v} for n, v in plugins]}))


def test_a_plugin_behind_its_marketplace_is_reported(fake_home):
    install_plugin(fake_home, "mkt", "plug", "1.0.0")
    declare(fake_home, "mkt", [("plug", "1.2.0")])
    report = update.check(db.connect())
    assert len(report.behind) == 1
    assert (report.behind[0].installed, report.behind[0].available) == ("1.0.0", "1.2.0")


def test_a_current_plugin_is_not_reported(fake_home):
    install_plugin(fake_home, "mkt", "plug", "1.2.0")
    declare(fake_home, "mkt", [("plug", "1.2.0")])
    report = update.check(db.connect())
    assert report.behind == [] and report.current == 1


def test_versions_compare_numerically_not_as_strings(fake_home):
    """Plain string order ranks 0.10.0 below 0.9.0 and would report a freshly
    updated plugin as behind."""
    install_plugin(fake_home, "mkt", "plug", "0.10.0")
    declare(fake_home, "mkt", [("plug", "0.9.0")])
    assert update.check(db.connect()).behind == []


def test_the_newest_installed_version_is_the_one_compared(fake_home):
    install_plugin(fake_home, "mkt", "plug", "1.0.0")
    install_plugin(fake_home, "mkt", "plug", "2.0.0")
    declare(fake_home, "mkt", [("plug", "2.0.0")])
    assert update.check(db.connect()).behind == []


def test_a_plugin_in_no_manifest_is_reported_separately(fake_home):
    """Not the same as up to date: there is nothing to compare against."""
    install_plugin(fake_home, "mkt", "orphan", "1.0.0")
    report = update.check(db.connect())
    assert report.behind == [] and report.unknown == ["orphan@mkt"]


def test_an_unreadable_manifest_does_not_crash(fake_home):
    install_plugin(fake_home, "mkt", "plug", "1.0.0")
    d = fake_home / "plugins" / "marketplaces" / "mkt" / ".claude-plugin"
    d.mkdir(parents=True)
    (d / "marketplace.json").write_text("{not json")
    report = update.check(db.connect())
    assert report.unknown == ["plug@mkt"]


def test_a_promoted_skill_on_a_stale_plugin_is_called_out(fake_home):
    """An update moves the version directory a promoted symlink points at."""
    install_plugin(fake_home, "mkt", "plug", "1.0.0")
    declare(fake_home, "mkt", [("plug", "2.0.0")])
    conn = db.connect()
    conn.execute(
        "INSERT INTO nodes (id, kind, name, origin, state, provider_plugin, marketplace) "
        "VALUES ('skill:plug@mkt:thing','skill','thing','user-authored','live','plug','mkt')")
    conn.commit()
    report = update.check(conn)
    assert report.behind[0].promoted == 1
    assert "symlink into the old path" in update.render(report)


def test_render_says_what_it_compared_when_clean(fake_home):
    out = update.render(update.check(db.connect()))
    assert "never contacts a remote" in out
