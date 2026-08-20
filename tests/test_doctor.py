"""Tests for doctor.py: drift detection.

Named after the rules in the task brief / the design notes --
especially rule 1 (strictly read-only, must never create a vault) and rule
5 (do not cry wolf on legitimate states).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import db, doctor, install, paths, scan, vault


def make_skill(base: Path, name: str, *, frontmatter_name: str | None = None) -> Path:
    d = base / "skills" / name
    d.mkdir(parents=True)
    fm_name = name if frontmatter_name is None else frontmatter_name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: Does a thing.\n---\n\nBody\n")
    return d


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


def install_with_fake_exe(monkeypatch, fake_home: Path) -> Path:
    exe_dir = fake_home / "bin"
    exe_dir.mkdir(exist_ok=True)
    exe = exe_dir / "harness"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install, "_executable_path", lambda: str(exe))
    install.install()
    return exe


def findings_by_check(report, check):
    return [f for f in report.findings if f.check == check]


# ---------------------------------------------------------------------------
# Read-only guarantee (rule 1)
# ---------------------------------------------------------------------------


def test_inspect_creates_nothing_when_vault_never_existed(fake_home):
    conn = db.connect()
    before = set(fake_home.rglob("*"))
    report = doctor.inspect(conn)
    after = set(fake_home.rglob("*"))
    # harness.db itself is allowed (db.connect() created it before inspect
    # ran) -- nothing new besides what already existed must appear.
    assert after == before
    assert not paths.vault_dir().exists()
    assert report.vault_state == "absent"


def test_inspect_does_not_initialize_a_bare_vault_directory(fake_home):
    paths.vault_dir().mkdir(parents=True)
    conn = db.connect()
    report = doctor.inspect(conn)
    assert report.vault_state == "invalid"
    assert not (paths.vault_dir() / ".git").exists()
    assert not (paths.vault_dir() / "manifest.json").exists()


def test_inspect_reports_healthy_vault_without_writing_to_it(fake_home):
    vault.ensure_vault()
    conn = db.connect()
    before_manifest = (paths.vault_dir() / "manifest.json").read_bytes()
    before_log = _git_log(paths.vault_dir())

    report = doctor.inspect(conn)

    assert report.vault_state == "healthy"
    assert (paths.vault_dir() / "manifest.json").read_bytes() == before_manifest
    assert _git_log(paths.vault_dir()) == before_log


def _git_log(root: Path) -> str:
    import subprocess
    return subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout


# ---------------------------------------------------------------------------
# Corrupt manifest is a finding, not a crash (rule 2)
# ---------------------------------------------------------------------------


def test_inspect_reports_corrupt_manifest_without_raising(fake_home):
    vault.ensure_vault()
    (paths.vault_dir() / "manifest.json").write_text("{not json")
    conn = db.connect()

    report = doctor.inspect(conn)  # must not raise

    assert report.vault_state == "healthy"  # .git and manifest.json both exist
    assert findings_by_check(report, "vault-manifest-corrupt")


# ---------------------------------------------------------------------------
# Manifest <-> vault directory cross-checks, both directions
# ---------------------------------------------------------------------------


def test_inspect_reports_manifest_entry_with_nothing_on_disk(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    import shutil
    shutil.rmtree(paths.vault_dir() / "skills" / "foo")
    conn = db.connect()

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "manifest-entry-missing-from-vault")
    assert len(findings) == 1
    assert "foo" in findings[0].message


def test_inspect_reports_vault_entry_with_no_manifest_record(fake_home):
    vault.ensure_vault()
    stray = paths.vault_dir() / "skills" / "stray"
    stray.mkdir(parents=True)
    (stray / "SKILL.md").write_text("---\nname: stray\ndescription: x\n---\n")
    conn = db.connect()

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "vault-entry-missing-from-manifest")
    assert len(findings) == 1
    assert "stray" in findings[0].message


# ---------------------------------------------------------------------------
# restored: True legitimacy (rule 5) vs a genuinely missing symlink
# ---------------------------------------------------------------------------


def test_inspect_does_not_flag_healthy_restored_entry(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.restore("foo", "skills")
    conn = db.connect()

    report = doctor.inspect(conn)
    assert findings_by_check(report, "restored-symlink-missing") == []


def test_inspect_flags_restored_entry_with_missing_symlink(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.restore("foo", "skills")
    (paths.skills_dir() / "foo").unlink()  # symlink removed by hand
    conn = db.connect()

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "restored-symlink-missing")
    assert len(findings) == 1
    assert "harness activate foo" in findings[0].fix


# ---------------------------------------------------------------------------
# Vaulted-without-node
# ---------------------------------------------------------------------------


def test_inspect_flags_vaulted_entry_missing_from_graph(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    conn = db.connect()  # deliberately not scanned

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "vaulted-without-node")
    assert len(findings) == 1
    assert "harness scan" in findings[0].fix


def test_inspect_does_not_flag_vaulted_entry_present_in_graph(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    conn = db.connect()
    full_scan(conn)

    report = doctor.inspect(conn)
    assert findings_by_check(report, "vaulted-without-node") == []


def test_inspect_does_not_flag_restored_vaulted_entry_as_missing_node(fake_home):
    # entry.restored=True is served by the live scanners instead -- must not
    # also be expected as a 'vaulted' node.
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.restore("foo", "skills")
    conn = db.connect()
    full_scan(conn)

    report = doctor.inspect(conn)
    assert findings_by_check(report, "vaulted-without-node") == []


# ---------------------------------------------------------------------------
# Stale promoted symlinks (rule 4 -- remediation must actually work)
# ---------------------------------------------------------------------------


def test_inspect_flags_stale_promoted_symlink_with_working_remediation(fake_home):
    cache_dir = make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    symlink = paths.skills_dir() / "widget"
    symlink.symlink_to(cache_dir, target_is_directory=True)
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": False}}))
    conn = db.connect()
    full_scan(conn)
    assert conn.execute("SELECT state FROM nodes WHERE id = 'skill:acme-plugin@acme-market:widget'").fetchone()["state"] == "live"

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "stale-promoted-symlink")
    assert len(findings) == 1
    # The previous build's dead-end advice named `harness activate`, which
    # no-ops on a live node. The fix here must not repeat that.
    assert "harness activate" not in findings[0].fix.split(";")[0]


def test_inspect_does_not_flag_promoted_symlink_when_plugin_still_enabled(fake_home):
    # A promoted symlink into a versioned plugin cache path is completely
    # normal while its plugin remains enabled -- rule 5, do not cry wolf.
    cache_dir = make_plugin_skill(fake_home, "acme-market", "acme-plugin", "widget")
    symlink = paths.skills_dir() / "widget"
    symlink.symlink_to(cache_dir, target_is_directory=True)
    paths.settings_path().write_text(json.dumps({"enabledPlugins": {"acme-plugin@acme-market": True}}))
    conn = db.connect()
    full_scan(conn)

    report = doctor.inspect(conn)
    assert findings_by_check(report, "stale-promoted-symlink") == []


# ---------------------------------------------------------------------------
# Dangling symlinks (rule 5 -- legitimate, still reported as fact)
# ---------------------------------------------------------------------------


def test_inspect_reports_dangling_skill_symlink(fake_home):
    dangling = paths.skills_dir() / "ghost"
    dangling.symlink_to(fake_home / "nonexistent-target")
    conn = db.connect()

    report = doctor.inspect(conn)
    findings = findings_by_check(report, "dangling-symlink")
    assert len(findings) == 1
    assert "ghost" in findings[0].message


def test_inspect_does_not_flag_normal_directory_skill(fake_home):
    make_skill(fake_home, "alpha")
    conn = db.connect()
    full_scan(conn)

    report = doctor.inspect(conn)
    assert findings_by_check(report, "dangling-symlink") == []


# ---------------------------------------------------------------------------
# Install / settings health
# ---------------------------------------------------------------------------


def test_inspect_reports_missing_skill_and_hook(fake_home):
    conn = db.connect()
    report = doctor.inspect(conn)
    assert report.skill_installed is False
    assert report.hook_installed is False
    assert findings_by_check(report, "skill-not-installed")
    assert findings_by_check(report, "hook-not-installed")


def test_inspect_reports_healthy_install(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    conn = db.connect()
    report = doctor.inspect(conn)
    assert report.skill_installed is True
    assert report.hook_installed is True
    assert findings_by_check(report, "skill-not-installed") == []
    assert findings_by_check(report, "hook-not-installed") == []


def test_inspect_reports_hook_pointing_at_deleted_binary_distinctly(fake_home, monkeypatch):
    exe = install_with_fake_exe(monkeypatch, fake_home)
    exe.unlink()
    conn = db.connect()

    report = doctor.inspect(conn)
    assert report.hook_installed is False
    findings = findings_by_check(report, "hook-command-missing")
    assert len(findings) == 1
    # Distinct from "no hook at all".
    assert findings_by_check(report, "hook-not-installed") == []


def test_inspect_reports_unreadable_settings(fake_home):
    paths.settings_path().write_text("{not json")
    conn = db.connect()

    report = doctor.inspect(conn)
    assert report.settings_readable is False
    assert findings_by_check(report, "settings-unreadable")


# ---------------------------------------------------------------------------
# render() -- no truncation (rule 3)
# ---------------------------------------------------------------------------


def test_render_no_problems_found(fake_home):
    conn = db.connect()
    report = doctor.Report(vault_state="absent", skill_installed=True, hook_installed=True, settings_readable=True)
    text = doctor.render(report)
    assert "no problems found" in text


def test_render_lists_every_finding_not_just_the_first_few(fake_home):
    report = doctor.Report(vault_state="healthy", skill_installed=True, hook_installed=True, settings_readable=True)
    for i in range(40):
        report.findings.append(doctor.Finding("warning", "synthetic", f"problem #{i}", f"fix #{i}"))
    text = doctor.render(report)
    for i in range(40):
        assert f"problem #{i}" in text


def test_render_distinguishes_skipped_checks_from_clean(fake_home):
    report = doctor.Report(vault_state="healthy", skill_installed=True, hook_installed=True, settings_readable=True)
    report.skipped.append("vault git status could not be checked")
    text = doctor.render(report)
    assert "vault git status could not be checked" in text
    assert "no problems found" not in text or "checks that could not run" in text


def test_render_separates_errors_from_warnings(fake_home):
    report = doctor.Report(vault_state="healthy", skill_installed=True, hook_installed=True, settings_readable=True)
    report.findings.append(doctor.Finding("error", "e1", "an error", "fix it"))
    report.findings.append(doctor.Finding("warning", "w1", "a warning", "maybe fix it"))
    text = doctor.render(report)
    assert "1 error(s)" in text
    assert "1 warning(s)" in text
