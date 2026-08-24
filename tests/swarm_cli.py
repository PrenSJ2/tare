import json
import shutil

from swarm import cli


def test_doctor_reports_a_stream(swarm_home, capsys):
    path = swarm_home / "runs" / "2026-08-19-s1.jsonl"
    path.write_text(json.dumps(
        {"ts": "t", "session": "s1", "event": "subagent_start", "agent_id": "a1"}) + "\n")
    assert cli.main(["doctor", str(path)]) == 0
    assert "swarm doctor" in capsys.readouterr().out


def test_doctor_with_no_argument_uses_the_newest_stream(swarm_home, capsys):
    (swarm_home / "runs" / "2026-08-18-old.jsonl").write_text("")
    newest = swarm_home / "runs" / "2026-08-19-new.jsonl"
    newest.write_text(json.dumps(
        {"ts": "t", "session": "s1", "event": "subagent_start", "agent_id": "a1"}) + "\n")
    assert cli.main(["doctor"]) == 0
    assert "2026-08-19-new" in capsys.readouterr().out


def test_doctor_with_no_streams_is_not_an_error(swarm_home, capsys):
    assert cli.main(["doctor"]) == 0
    assert "no streams" in capsys.readouterr().out.lower()


def test_list_shows_streams(swarm_home, capsys):
    (swarm_home / "runs" / "2026-08-19-s1.jsonl").write_text("")
    assert cli.main(["list"]) == 0
    assert "2026-08-19-s1" in capsys.readouterr().out


def test_install_reports_missing_swarm_hook_and_exits_1(swarm_home, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "swarm-hook not found on PATH" in out


def test_install_with_explicit_command_registers_hooks(swarm_home, tmp_path, capsys):
    hook = tmp_path / "swarm-hook"
    hook.write_text("#!/bin/sh\n")
    assert cli.main(["install", "--command", str(hook)]) == 0
    out = capsys.readouterr().out
    assert "registered 5 hooks" in out


def test_install_on_malformed_settings_exits_1(swarm_home, tmp_path, capsys):
    (swarm_home / "settings.json").write_text("{ not json")
    hook = tmp_path / "swarm-hook"
    hook.write_text("#!/bin/sh\n")
    assert cli.main(["install", "--command", str(hook)]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "could not be parsed" in err


def test_uninstall_runs_via_cli(swarm_home, capsys):
    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert "removed swarm hooks" in out


def test_uninstall_on_malformed_settings_exits_1(swarm_home, capsys):
    (swarm_home / "settings.json").write_text("{ not json")
    assert cli.main(["uninstall"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
