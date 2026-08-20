"""Tests for install.py: the harness skill + SessionStart hook, and the
settings.json writer everything else in this module leans on.

Named after the rules in the task brief / the design notes, not just
the happy path -- the failure modes are the point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import install, paths


def make_fake_exe(fake_home: Path, name: str = "harness") -> Path:
    """A file that stands in for the installed console script -- it must
    exist on disk so is_installed()'s existence check can pass or fail
    deliberately."""
    exe_dir = fake_home / "bin"
    exe_dir.mkdir(exist_ok=True)
    exe = exe_dir / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def install_with_fake_exe(monkeypatch, fake_home: Path, name: str = "harness") -> Path:
    exe = make_fake_exe(fake_home, name)
    monkeypatch.setattr(install, "_executable_path", lambda: str(exe))
    install.install()
    return exe


# ---------------------------------------------------------------------------
# _load / _save
# ---------------------------------------------------------------------------


def test_load_returns_empty_dict_when_settings_missing(fake_home):
    assert install._load() == {}


def test_load_raises_on_invalid_json(fake_home):
    paths.settings_path().write_text("{not json")
    with pytest.raises(ValueError):
        install._load()


def test_load_raises_on_non_object_json(fake_home):
    paths.settings_path().write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError):
        install._load()


def test_save_writes_readable_json(fake_home):
    install._save({"a": 1})
    assert json.loads(paths.settings_path().read_text()) == {"a": 1}


def test_save_creates_timestamped_backup_of_existing_file(fake_home):
    paths.settings_path().write_text(json.dumps({"old": True}))
    install._save({"new": True})

    backups = list(paths.settings_path().parent.glob("settings.json.*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"old": True}
    assert json.loads(paths.settings_path().read_text()) == {"new": True}


def test_save_does_not_create_backup_when_no_prior_file(fake_home):
    install._save({"a": 1})
    backups = list(paths.settings_path().parent.glob("settings.json.*.bak"))
    assert backups == []


def test_save_never_leaves_a_torn_file_on_reparse_failure(fake_home, monkeypatch):
    # A value json.dump can write but that a stricter reparse would reject.
    # Simulate by making json.loads (the reparse step) blow up and checking
    # the ORIGINAL file survives untouched.
    paths.settings_path().write_text(json.dumps({"old": True}))
    import harness.install as install_mod

    real_loads = json.loads
    calls = {"n": 0}

    def flaky_loads(text, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:  # the reparse-of-tempfile call inside _save
            raise json.JSONDecodeError("simulated", text, 0)
        return real_loads(text, *a, **kw)

    monkeypatch.setattr(install_mod.json, "loads", flaky_loads)
    with pytest.raises(json.JSONDecodeError):
        install._save({"new": True})

    assert json.loads(paths.settings_path().read_text()) == {"old": True}
    # No leftover temp file.
    assert not list(paths.settings_path().parent.glob(".settings.json.*.tmp"))


def test_save_atomic_replace_leaves_no_temp_file_on_success(fake_home):
    install._save({"a": 1})
    assert not list(paths.settings_path().parent.glob(".settings.json.*.tmp"))


# ---------------------------------------------------------------------------
# registered_command / is_installed
# ---------------------------------------------------------------------------


def test_registered_command_none_when_nothing_installed(fake_home):
    assert install.registered_command() is None
    assert install.is_installed() is False


def test_registered_command_and_is_installed_true_after_install(fake_home, monkeypatch):
    exe = install_with_fake_exe(monkeypatch, fake_home)
    assert install.registered_command() == f"{exe} hookline"
    assert install.is_installed() is True


def test_is_installed_false_when_hook_points_at_deleted_binary(fake_home, monkeypatch):
    # Rule 2: a hook entry existing is not enough -- the executable it names
    # must still be on disk, or `is_installed()` must say False.
    exe = install_with_fake_exe(monkeypatch, fake_home)
    exe.unlink()
    assert install.registered_command() == f"{exe} hookline"  # entry still present
    assert install.is_installed() is False  # but unusable


def test_registered_command_ignores_foreign_hook_with_harness_substring(fake_home):
    # Rule 3: "/opt/my-harness-tool/bin/run hookline" contains "harness" as a
    # substring and ends with " hookline" -- a substring check would wrongly
    # treat this as ours. Basename of the command is "run", not "harness".
    data = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "/opt/my-harness-tool/bin/run hookline"}]}
            ]
        }
    }
    paths.settings_path().write_text(json.dumps(data))
    assert install.registered_command() is None
    assert install.is_installed() is False


def test_registered_command_ignores_wrong_argument(fake_home):
    data = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/harness other"}]}]
        }
    }
    paths.settings_path().write_text(json.dumps(data))
    assert install.registered_command() is None


def test_registered_command_ignores_extra_arguments(fake_home):
    data = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/harness hookline --extra"}]}
            ]
        }
    }
    paths.settings_path().write_text(json.dumps(data))
    assert install.registered_command() is None


def test_registered_command_returns_none_on_corrupt_settings(fake_home):
    paths.settings_path().write_text("{not json")
    assert install.registered_command() is None
    assert install.is_installed() is False


# ---------------------------------------------------------------------------
# install()
# ---------------------------------------------------------------------------


def test_install_writes_skill_file(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    skill_path = paths.skill_install_path()
    assert skill_path.exists()
    text = skill_path.read_text()
    assert text.startswith("---\nname: harness\n")


def test_install_skill_description_is_short(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    text = paths.skill_install_path().read_text()
    for line in text.splitlines():
        if line.startswith("description:"):
            desc = line[len("description:"):].strip()
            assert len(desc.split()) <= 30
            return
    pytest.fail("no description: line found")


def test_install_skill_only_mentions_real_subcommands(fake_home, monkeypatch):
    # Rule 6: every `harness <word>` mentioned in the skill body must be a
    # real subcommand. The previous build advertised `harness list`, which
    # never existed.
    allowed = {
        "scan", "mine", "tag", "build", "lookup", "audit", "graph",
        "install", "uninstall", "hookline", "activate", "deactivate",
        "doctor", "vault",
    }
    install_with_fake_exe(monkeypatch, fake_home)
    text = paths.skill_install_path().read_text()
    import re
    for m in re.finditer(r"harness ([a-z][a-z-]*)", text):
        word = m.group(1)
        assert word in allowed, f"skill text mentions unknown subcommand: harness {word}"


def test_install_registers_session_start_hook(fake_home, monkeypatch):
    exe = install_with_fake_exe(monkeypatch, fake_home)
    data = json.loads(paths.settings_path().read_text())
    group = data["hooks"]["SessionStart"][0]
    assert group["hooks"][0]["command"] == f"{exe} hookline"
    assert group["hooks"][0]["type"] == "command"


def test_install_preserves_unrelated_settings(fake_home, monkeypatch):
    paths.settings_path().write_text(json.dumps({"otherSetting": "keep-me", "enabledPlugins": {"foo@bar": True}}))
    install_with_fake_exe(monkeypatch, fake_home)
    data = json.loads(paths.settings_path().read_text())
    assert data["otherSetting"] == "keep-me"
    assert data["enabledPlugins"] == {"foo@bar": True}


def test_install_preserves_foreign_session_start_hooks(fake_home, monkeypatch):
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "/opt/other-tool/bin/run something"}]}
    paths.settings_path().write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}))
    install_with_fake_exe(monkeypatch, fake_home)
    data = json.loads(paths.settings_path().read_text())
    commands = [g["hooks"][0]["command"] for g in data["hooks"]["SessionStart"]]
    assert "/opt/other-tool/bin/run something" in commands
    assert any(c.endswith("hookline") for c in commands)


def test_install_is_idempotent_no_duplicate_entries(fake_home, monkeypatch):
    exe = make_fake_exe(fake_home)
    monkeypatch.setattr(install, "_executable_path", lambda: str(exe))
    install.install()
    install.install()
    data = json.loads(paths.settings_path().read_text())
    session_start = data["hooks"]["SessionStart"]
    ours = [g for g in session_start if g["hooks"][0]["command"] == f"{exe} hookline"]
    assert len(ours) == 1


def test_install_replaces_stale_entry_pointing_at_old_executable(fake_home, monkeypatch):
    old_exe = make_fake_exe(fake_home, "harness")
    monkeypatch.setattr(install, "_executable_path", lambda: str(old_exe))
    install.install()

    new_dir = fake_home / "bin2"
    new_dir.mkdir()
    new_exe = new_dir / "harness"
    new_exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(install, "_executable_path", lambda: str(new_exe))
    install.install()

    data = json.loads(paths.settings_path().read_text())
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 1
    assert session_start[0]["hooks"][0]["command"] == f"{new_exe} hookline"


# ---------------------------------------------------------------------------
# uninstall()
# ---------------------------------------------------------------------------


def test_uninstall_removes_skill_file(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    install.uninstall()
    assert not paths.skill_install_path().exists()


def test_uninstall_removes_hook_entry(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    install.uninstall()
    assert install.registered_command() is None


def test_uninstall_leaves_foreign_hooks_untouched(fake_home, monkeypatch):
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "/opt/other-tool/bin/run something"}]}
    paths.settings_path().write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}))
    install_with_fake_exe(monkeypatch, fake_home)
    install.uninstall()

    data = json.loads(paths.settings_path().read_text())
    commands = [g["hooks"][0]["command"] for g in data["hooks"]["SessionStart"]]
    assert commands == ["/opt/other-tool/bin/run something"]


def test_uninstall_does_not_touch_substring_lookalike_hook(fake_home):
    # Rule 3, from uninstall's side: a foreign command containing "harness"
    # as a substring and ending in " hookline" must survive uninstall.
    lookalike = {"matcher": "", "hooks": [{"type": "command", "command": "/opt/my-harness-tool/bin/run hookline"}]}
    paths.settings_path().write_text(json.dumps({"hooks": {"SessionStart": [lookalike]}}))
    install.uninstall()
    data = json.loads(paths.settings_path().read_text())
    assert data["hooks"]["SessionStart"] == [lookalike]


def test_uninstall_only_removes_session_start_event(fake_home, monkeypatch):
    # Rule 4: exact event name match -- a harness-shaped hook entry under a
    # different event must not be removed.
    exe = make_fake_exe(fake_home)
    other_event_entry = {"matcher": "", "hooks": [{"type": "command", "command": f"{exe} hookline"}]}
    monkeypatch.setattr(install, "_executable_path", lambda: str(exe))
    install.install()
    data = json.loads(paths.settings_path().read_text())
    data.setdefault("hooks", {})["Stop"] = [other_event_entry]
    paths.settings_path().write_text(json.dumps(data))

    install.uninstall()

    data = json.loads(paths.settings_path().read_text())
    assert data["hooks"]["Stop"] == [other_event_entry]
    assert "SessionStart" not in data.get("hooks", {})


def test_uninstall_is_safe_when_nothing_installed(fake_home):
    install.uninstall()  # must not raise
    assert install.registered_command() is None


def test_uninstall_removes_empty_harness_skill_directory(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    install.uninstall()
    assert not paths.skill_install_path().parent.exists()


def test_uninstall_backs_up_settings_before_writing(fake_home, monkeypatch):
    install_with_fake_exe(monkeypatch, fake_home)
    before = list(paths.settings_path().parent.glob("settings.json.*.bak"))
    install.uninstall()
    after = list(paths.settings_path().parent.glob("settings.json.*.bak"))
    assert len(after) > len(before)


# ---------------------------------------------------------------------------
# git-failure-style robustness isn't applicable here (no git), but a
# read-only filesystem / permission-style failure during _save must not
# leave install() claiming success. We approximate by making the temp write
# fail outright.
# ---------------------------------------------------------------------------


def test_save_propagates_write_failures(fake_home, monkeypatch):
    def boom(*a, **kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr("harness.install.tempfile.mkstemp", boom)
    with pytest.raises(OSError):
        install._save({"a": 1})
