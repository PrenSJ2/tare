import json
import shutil
import subprocess

from swarm import cli, doctor


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


def test_doctor_stays_silent_about_bmad_when_not_installed(swarm_home, tmp_path, monkeypatch, capsys):
    """A project that has never touched BMAD must see exactly today's
    output -- no new "no BMAD install" warning on every run. That would
    turn a diagnostic into noise, and a noisy diagnostic gets ignored,
    which defeats the point of the check."""
    path = swarm_home / "runs" / "2026-08-19-s1.jsonl"
    path.write_text(json.dumps(
        {"ts": "t", "session": "s1", "event": "subagent_start", "agent_id": "a1"}) + "\n")
    monkeypatch.chdir(tmp_path)  # no _bmad/bmm/config.yaml anywhere here

    assert cli.main(["doctor", str(path)]) == 0
    out = capsys.readouterr().out

    expected = doctor.render(doctor.inspect(path), doctor.check_hook_command())
    assert out.rstrip("\n") == expected


def test_doctor_surfaces_bmad_findings_when_installed(swarm_home, tmp_path, monkeypatch, capsys):
    path = swarm_home / "runs" / "2026-08-19-s1.jsonl"
    path.write_text(json.dumps(
        {"ts": "t", "session": "s1", "event": "subagent_start", "agent_id": "a1"}) + "\n")

    repo = tmp_path / "work"
    repo.mkdir()
    for args in (["init", "-q", "-b", "feature/x"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True)

    cfg = repo / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("project_name: demo\n")
    lonely = repo / "_bmad-output" / "specs" / "spec-planned"
    lonely.mkdir(parents=True)
    (lonely / "SPEC.md").write_text("# planned only\n")

    monkeypatch.chdir(repo)
    assert cli.main(["doctor", str(path)]) == 0
    out = capsys.readouterr().out
    assert "spec-planned" in out
    assert "planned, not broken down" in out


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


def test_queue_defaults_to_session_mode(capsys):
    """The existing behaviour is the default. Story mode is opted into."""
    from swarm import cli
    parser = cli.build_parser()
    args = parser.parse_args(["nightshift", "start", "--session", "abc"])
    assert args.queue == "session"


def test_queue_bmad_selects_story_mode():
    from swarm import cli
    parser = cli.build_parser()
    args = parser.parse_args(["nightshift", "start", "--queue", "bmad"])
    assert args.queue == "bmad"


def test_the_night_window_is_off_by_default_in_story_mode():
    from swarm import cli
    parser = cli.build_parser()
    args = parser.parse_args(["nightshift", "start", "--queue", "bmad"])
    assert args.window is False


def test_the_window_can_be_restored():
    from swarm import cli
    parser = cli.build_parser()
    args = parser.parse_args(["nightshift", "start", "--queue", "bmad", "--window"])
    assert args.window is True
