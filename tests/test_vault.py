"""Tests for the vault -- the git-backed store holding the only copy of
shelved user-authored skills and agents.

Every test here is named after a real defect this project shipped once
rather than after a feature: this module's failure behaviour matters more
than anything else in the codebase, so the failure modes are the point.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tare import paths, vault


def make_skill(fake_home: Path, name: str, frontmatter_name: str | None = None) -> Path:
    d = fake_home / "skills" / name
    d.mkdir(parents=True)
    fm_name = name if frontmatter_name is None else frontmatter_name
    (d / "SKILL.md").write_text(f"---\nname: {fm_name}\ndescription: Does a thing.\n---\n\nBody\n")
    return d


def make_agent(fake_home: Path, filename: str, frontmatter_name: str | None = None) -> Path:
    p = fake_home / "agents" / f"{filename}.md"
    fm_name = filename if frontmatter_name is None else frontmatter_name
    p.write_text(f"---\nname: {fm_name}\ndescription: Reviews things.\n---\n\nBody\n")
    return p


# ---------------------------------------------------------------------------
# ensure_vault / is_initialized
# ---------------------------------------------------------------------------


def test_ensure_vault_creates_full_tree(fake_home):
    root = vault.ensure_vault()
    assert root == paths.vault_dir()
    assert (root / ".git").exists()
    assert (root / "manifest.json").exists()
    assert (root / "skills").is_dir()
    assert (root / "agents").is_dir()
    assert vault.is_initialized()


def test_ensure_vault_is_safe_to_call_repeatedly(fake_home):
    vault.ensure_vault()
    before = vault.manifest()
    vault.ensure_vault()
    vault.ensure_vault()
    assert vault.manifest() == before


def test_is_initialized_false_when_nothing_exists(fake_home):
    assert not vault.is_initialized()


def test_is_initialized_false_for_bare_directory(fake_home):
    # A directory existing is not enough -- this is exactly what a process
    # killed at the start of ensure_vault() would leave behind.
    paths.vault_dir().mkdir(parents=True)
    assert not vault.is_initialized()


def test_is_initialized_false_when_git_present_but_manifest_missing(fake_home):
    root = paths.vault_dir()
    (root / ".git").mkdir(parents=True)
    assert not (root / "manifest.json").exists()
    assert not vault.is_initialized()


def test_is_initialized_true_for_linked_worktree_git_file(fake_home):
    # A linked worktree's .git is a FILE, not a directory. Checking .is_dir()
    # would misread a perfectly good vault as broken.
    vault.ensure_vault()
    root = paths.vault_dir()
    shutil.rmtree(root / ".git")
    (root / ".git").write_text("gitdir: /somewhere/else/.git/worktrees/vault\n")
    assert vault.is_initialized()


# ---------------------------------------------------------------------------
# manifest()
# ---------------------------------------------------------------------------


def test_manifest_raises_on_invalid_json_and_names_recovery_command(fake_home):
    vault.ensure_vault()
    path = paths.vault_dir() / "manifest.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError) as excinfo:
        vault.manifest()
    message = str(excinfo.value)
    assert f"git -C {paths.vault_dir()} checkout HEAD -- manifest.json" in message
    # The corrupt copy is still on disk -- that's what the git command recovers.
    assert path.exists()


def test_manifest_raises_on_non_dict_json(fake_home):
    vault.ensure_vault()
    path = paths.vault_dir() / "manifest.json"
    path.write_text(json.dumps(["not", "a", "mapping"]))
    with pytest.raises(ValueError):
        vault.manifest()
    assert path.exists()


def test_manifest_raises_when_missing_entirely(fake_home):
    paths.vault_dir().mkdir(parents=True)
    with pytest.raises(ValueError):
        vault.manifest()


# ---------------------------------------------------------------------------
# stash()
# ---------------------------------------------------------------------------


def test_stash_moves_skill_into_vault_and_indexes_it(fake_home):
    skill = make_skill(fake_home, "foo")
    dest = vault.stash(skill, "skills")
    assert dest == paths.vault_dir() / "skills" / "foo"
    assert dest.is_dir()
    assert not skill.exists()
    assert vault.manifest()["skills"]["foo"] == {"restored": False}


def test_stash_agent_keys_manifest_by_stem(fake_home):
    agent = make_agent(fake_home, "bar")
    dest = vault.stash(agent, "agents")
    assert dest == paths.vault_dir() / "agents" / "bar.md"
    assert dest.is_file()
    assert not agent.exists()
    assert vault.manifest()["agents"]["bar"] == {"restored": False}


def test_stash_with_corrupt_manifest_leaves_source_exactly_where_it_was(fake_home):
    # The original bug: shutil.move ran before the manifest was validated,
    # so a corrupt manifest left the capability gone from ~/.claude/skills,
    # absent from the vault index, and the command reported success.
    vault.ensure_vault()
    (paths.vault_dir() / "manifest.json").write_text("{still broken")
    skill = make_skill(fake_home, "foo")

    with pytest.raises(ValueError):
        vault.stash(skill, "skills")

    assert skill.exists()
    assert not (paths.vault_dir() / "skills" / "foo").exists()


def test_stash_refuses_to_overwrite_existing_vault_entry(fake_home):
    skill = make_skill(fake_home, "foo")
    vault.stash(skill, "skills")
    skill2 = make_skill(fake_home, "foo")
    with pytest.raises(FileExistsError):
        vault.stash(skill2, "skills")


def test_stash_rejects_unknown_kind(fake_home):
    with pytest.raises(ValueError):
        vault.stash(fake_home / "skills" / "foo", "widgets")


# ---------------------------------------------------------------------------
# resolve_name()
# ---------------------------------------------------------------------------


def test_resolve_name_matches_filesystem_key(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    assert vault.resolve_name("foo", "skills") == "foo"


def test_resolve_name_matches_declared_frontmatter_name(fake_home):
    # Real case: agents/architect-review.md declaring name: architect-reviewer.
    vault.stash(make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer"), "agents")
    assert vault.resolve_name("architect-reviewer", "agents") == "architect-review"
    assert vault.resolve_name("architect-review", "agents") == "architect-review"


def test_resolve_name_returns_none_when_absent(fake_home):
    vault.ensure_vault()
    assert vault.resolve_name("nope", "skills") is None


def test_resolve_name_raises_lookuperror_naming_candidates_on_collision(fake_home):
    vault.stash(make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer"), "agents")
    vault.stash(make_agent(fake_home, "architect-reviewer", frontmatter_name="architect-reviewer"), "agents")
    with pytest.raises(LookupError) as excinfo:
        vault.resolve_name("architect-reviewer", "agents")
    message = str(excinfo.value)
    assert "architect-review" in message and "architect-reviewer" in message


def test_resolve_name_falls_back_to_key_when_frontmatter_unparseable(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    (paths.vault_dir() / "skills" / "foo" / "SKILL.md").write_text("not frontmatter at all")
    assert vault.resolve_name("foo", "skills") == "foo"


def test_resolve_name_survives_invalid_utf8_in_vaulted_file(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    (paths.vault_dir() / "skills" / "foo" / "SKILL.md").write_bytes(b"---\nname: \xff\xfebroken\n---\n")
    # Must not raise UnicodeDecodeError; key-match still resolves.
    assert vault.resolve_name("foo", "skills") == "foo"


def test_resolve_name_rejects_unknown_kind(fake_home):
    with pytest.raises(ValueError):
        vault.resolve_name("foo", "widgets")


# ---------------------------------------------------------------------------
# restore() / unrestore()
# ---------------------------------------------------------------------------


def test_restore_symlinks_vault_copy_and_marks_restored(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    live = vault.restore("foo", "skills")
    assert live == paths.skills_dir() / "foo"
    assert live.is_symlink()
    assert live.resolve() == (paths.vault_dir() / "skills" / "foo").resolve()
    assert vault.manifest()["skills"]["foo"]["restored"] is True


def test_restore_by_declared_name(fake_home):
    vault.stash(make_agent(fake_home, "architect-review", frontmatter_name="architect-reviewer"), "agents")
    live = vault.restore("architect-reviewer", "agents")
    assert live == paths.agents_dir() / "architect-review.md"
    assert live.is_symlink()


def test_restore_is_idempotent(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.restore("foo", "skills")
    live = vault.restore("foo", "skills")  # must not raise
    assert live.is_symlink()
    assert vault.manifest()["skills"]["foo"]["restored"] is True


def test_restore_raises_on_foreign_symlink_and_does_not_claim_success(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    elsewhere = fake_home / "elsewhere"
    elsewhere.mkdir()
    (paths.skills_dir() / "foo").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(FileExistsError):
        vault.restore("foo", "skills")

    # The original bug: this returned success without setting restored=True.
    assert vault.manifest()["skills"]["foo"]["restored"] is False
    assert (paths.skills_dir() / "foo").resolve() == elsewhere.resolve()


def test_restore_raises_on_non_symlink_occupant(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    (paths.skills_dir() / "foo").mkdir()
    with pytest.raises(FileExistsError):
        vault.restore("foo", "skills")


def test_restore_raises_for_name_not_in_vault(fake_home):
    vault.ensure_vault()
    with pytest.raises(LookupError):
        vault.restore("nope", "skills")


def test_unrestore_removes_symlink_and_keeps_vault_copy(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.restore("foo", "skills")
    vault.unrestore("foo", "skills")
    assert not (paths.skills_dir() / "foo").exists()
    assert (paths.vault_dir() / "skills" / "foo").exists()
    assert vault.manifest()["skills"]["foo"]["restored"] is False


def test_unrestore_is_idempotent_when_never_restored(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    vault.unrestore("foo", "skills")  # must not raise
    assert vault.manifest()["skills"]["foo"]["restored"] is False


def test_unrestore_leaves_foreign_symlink_untouched(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    elsewhere = fake_home / "elsewhere"
    elsewhere.mkdir()
    (paths.skills_dir() / "foo").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(FileExistsError):
        vault.unrestore("foo", "skills")

    assert (paths.skills_dir() / "foo").is_symlink()
    assert (paths.skills_dir() / "foo").resolve() == elsewhere.resolve()


# ---------------------------------------------------------------------------
# is_stashed()
# ---------------------------------------------------------------------------


def test_is_stashed_false_before_vault_exists(fake_home):
    assert vault.is_stashed("foo", "skills") is False


def test_is_stashed_true_after_stash_false_for_others(fake_home):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    assert vault.is_stashed("foo", "skills") is True
    assert vault.is_stashed("bar", "skills") is False


# ---------------------------------------------------------------------------
# git failures must not be silent
# ---------------------------------------------------------------------------


def test_ensure_vault_raises_when_git_init_fails(fake_home, monkeypatch):
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "init"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="simulated init failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        vault.ensure_vault()


def test_stash_raises_when_commit_fails(fake_home, monkeypatch):
    vault.ensure_vault()
    skill = make_skill(fake_home, "foo")
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="simulated commit failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        vault.stash(skill, "skills")


def test_restore_raises_when_commit_fails(fake_home, monkeypatch):
    vault.stash(make_skill(fake_home, "foo"), "skills")
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="simulated commit failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        vault.restore("foo", "skills")
