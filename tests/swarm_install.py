import json

import pytest

from swarm import install, paths


def settings(home):
    return json.loads((home / "settings.json").read_text())


def test_install_creates_hooks_key(swarm_home):
    (swarm_home / "settings.json").write_text(json.dumps({"tui": "fullscreen"}))
    touched = install.install("/abs/path/swarm-hook")
    data = settings(swarm_home)
    assert set(touched) == set(install.EVENTS)
    assert "hooks" in data
    assert data["tui"] == "fullscreen", "must not disturb unrelated settings"


def test_install_writes_a_backup_first(swarm_home):
    (swarm_home / "settings.json").write_text(json.dumps({"tui": "fullscreen"}))
    install.install("/abs/path/swarm-hook")
    backups = list(swarm_home.glob("settings.json.bak*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"tui": "fullscreen"}


def test_install_preserves_existing_unrelated_hooks(swarm_home):
    (swarm_home / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}
    }))
    install.install("/abs/path/swarm-hook")
    data = settings(swarm_home)
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "other"
    assert "SubagentStart" in data["hooks"]


def test_install_is_idempotent(swarm_home):
    (swarm_home / "settings.json").write_text("{}")
    install.install("/abs/path/swarm-hook")
    install.install("/abs/path/swarm-hook")
    data = settings(swarm_home)
    assert len(data["hooks"]["SubagentStart"]) == 1


def test_uninstall_removes_only_swarm_hooks(swarm_home):
    (swarm_home / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "other"}]}]}
    }))
    install.install("/abs/path/swarm-hook")
    install.uninstall()
    data = settings(swarm_home)
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "other"
    assert "SubagentStart" not in data.get("hooks", {})


def test_two_writes_produce_two_backups(swarm_home):
    """install-then-uninstall lands in the same second; a colliding backup name
    would leave the operator holding the post-install state, not their original."""
    (swarm_home / "settings.json").write_text(json.dumps({"tui": "fullscreen"}))
    install.install("/abs/path/swarm-hook")
    install.uninstall()
    assert len(list(swarm_home.glob("settings.json.bak*"))) == 2


def test_install_rejects_a_command_not_named_swarm_hook(swarm_home):
    """The marker lives in the command; a differently-named executable would
    register entries uninstall could never identify."""
    (swarm_home / "settings.json").write_text("{}")
    with pytest.raises(ValueError, match="swarm-hook"):
        install.install("/abs/path/some-other-name")


def test_uninstall_leaves_a_foreign_hook_that_mentions_swarm_hook(swarm_home):
    """A substring match would delete someone else's hook."""
    (swarm_home / "settings.json").write_text(json.dumps({
        "hooks": {"SubagentStart": [
            {"hooks": [{"type": "command", "command": "/opt/wrapper --near swarm-hook --log"}]}
        ]}
    }))
    install.install("/abs/path/swarm-hook")
    install.uninstall()
    data = settings(swarm_home)
    remaining = [h["command"] for e in data["hooks"]["SubagentStart"] for h in e["hooks"]]
    assert remaining == ["/opt/wrapper --near swarm-hook --log"]


def test_uninstall_does_not_touch_events_swarm_never_registers(swarm_home):
    (swarm_home / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "/opt/other/swarm-hook PreToolUse"}]}
        ]}
    }))
    install.install("/abs/path/swarm-hook")
    install.uninstall()
    assert "PreToolUse" in settings(swarm_home)["hooks"]


def test_malformed_settings_raises_rather_than_clobbering(swarm_home):
    (swarm_home / "settings.json").write_text("{ not json")
    with pytest.raises(ValueError, match="could not be parsed"):
        install.install("/abs/path/swarm-hook")
    assert (swarm_home / "settings.json").read_text() == "{ not json"


def test_install_quotes_a_command_containing_a_space(swarm_home):
    """An unquoted command with a space in its path would register a hook
    that splits on the wrong word and never runs again."""
    (swarm_home / "settings.json").write_text("{}")
    install.install("/abs/path with space/swarm-hook")
    data = settings(swarm_home)
    cmd = data["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
    assert cmd == '"/abs/path with space/swarm-hook" SubagentStart'


def test_uninstall_with_no_settings_file_is_a_noop(swarm_home):
    """Nothing of swarm's is registered when there is no settings.json at
    all; uninstall must not create one containing just {}."""
    assert not (swarm_home / "settings.json").exists()
    touched = install.uninstall()
    assert touched == []
    assert not (swarm_home / "settings.json").exists()


def test_uninstall_with_nothing_of_swarms_does_not_touch_the_file(swarm_home):
    """A no-op uninstall must not reformat the operator's file or drop a
    needless backup."""
    original = json.dumps({"tui": "fullscreen"})
    (swarm_home / "settings.json").write_text(original)
    touched = install.uninstall()
    assert touched == []
    assert (swarm_home / "settings.json").read_text() == original
    assert list(swarm_home.glob("settings.json.bak*")) == []


def test_registered_command_round_trips_through_install(swarm_home):
    (swarm_home / "settings.json").write_text("{}")
    install.install("/abs/path/swarm-hook")
    assert install.registered_command() == "/abs/path/swarm-hook"


def test_registered_command_is_none_when_nothing_installed(swarm_home):
    assert install.registered_command() is None


def test_registered_command_degrades_on_malformed_settings(swarm_home):
    (swarm_home / "settings.json").write_text("{ not json")
    assert install.registered_command() is None
