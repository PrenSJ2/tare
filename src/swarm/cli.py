"""swarm — inspect and manage the agent event stream."""

import argparse
import shutil
import sys
from pathlib import Path

from swarm import doctor, install, paths


def _newest_stream():
    runs = paths.runs_dir()
    if not runs.is_dir():
        return None
    streams = sorted(runs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return streams[-1] if streams else None


def _cmd_doctor(args) -> int:
    path = Path(args.path) if args.path else _newest_stream()
    if path is None:
        print("no streams found in", paths.runs_dir())
        return 0
    print(doctor.render(doctor.inspect(path), doctor.check_hook_command()))
    return 0


def _cmd_list(args) -> int:
    runs = paths.runs_dir()
    streams = sorted(runs.glob("*.jsonl")) if runs.is_dir() else []
    if not streams:
        print("no streams found in", runs)
        return 0
    for path in streams:
        print(f"{path.stat().st_size:>10,}  {path.name}")
    return 0


def _cmd_install(args) -> int:
    resolved = args.command or shutil.which("swarm-hook")
    if not resolved:
        print("swarm-hook not found on PATH. Install with `uv tool install --editable .`,")
        print("or pass its absolute path: swarm install --command /abs/path/swarm-hook")
        return 1
    touched = install.install(str(resolved))
    print(f"registered {len(touched)} hooks in {paths.settings_path()}")
    print("  " + ", ".join(touched))

    # The Stop hook goes in alongside, but does nothing until a repository is
    # armed with `swarm keepgoing on` -- registering it is not opting in.
    keepgoing_cmd = shutil.which("swarm-keepgoing")
    if keepgoing_cmd:
        install.install_keepgoing(keepgoing_cmd)
        print("  Stop (keepgoing -- inactive until `swarm keepgoing on`)")
    else:
        print("  swarm-keepgoing not on PATH; automatic continuation unavailable")
    print("\nHooks are live-reloaded; no restart needed.")
    return 0


def _cmd_uninstall(args) -> int:
    touched = install.uninstall()
    install.uninstall_keepgoing()
    print(f"removed swarm hooks from {len(touched)} event(s) in {paths.settings_path()}")
    return 0


def _cmd_watch(args) -> int:
    from . import watch as watch_mod

    return watch_mod.watch(args.session, interval=args.interval, redact=args.redact)


def _cmd_monitor(args) -> int:
    from datetime import datetime

    from . import monitor as monitor_mod
    from . import reader

    session = args.session or reader.current_session()
    if session is None:
        print("no session transcript found")
        return 1
    runs = reader.read_session(session, redact=args.redact)
    findings = monitor_mod.assess(runs, now=datetime.now().astimezone(), goal=args.goal)
    print(monitor_mod.render(findings, goal=args.goal))
    # Non-zero only when something is worth acting on, so this can gate a script.
    return 1 if any(f.severity == "act" for f in findings) else 0


def _cmd_nightshift(args) -> int:
    from datetime import datetime, timedelta

    from . import nightshift as ns
    from . import reader

    if args.action == "recap":
        since = None
        if args.since_hours:
            since = datetime.now().astimezone() - timedelta(hours=args.since_hours)
        print(ns.recap(ns.read_ledger(since=since)))
        return 0

    if args.action == "stop":
        ns.stop_file().parent.mkdir(parents=True, exist_ok=True)
        ns.stop_file().touch()
        print(f"asked the shift to stop after its current step ({ns.stop_file()})")
        print("remove that file before starting another shift")
        return 0

    if args.action == "status":
        entries = ns.read_ledger()
        if not entries:
            print("no shift has ever run")
            return 0
        last = entries[-1]
        print(f"last entry {last.get('at')}: {last.get('event')} "
              f"{last.get('reason') or last.get('recommendation') or ''}")
        if ns.stop_file().exists():
            print(f"a stop file is present at {ns.stop_file()} -- start will refuse")
        return 0

    # start
    if ns.stop_file().exists():
        print(f"a stop file is present at {ns.stop_file()}; remove it to start")
        return 1
    if args.apply and not ns.available():
        print("`claude` is not on PATH -- nothing to dispatch to")
        return 1

    session = args.session or reader.current_session()
    if session is None:
        print("no session transcript found")
        return 1
    repo = paths.working_tree(args.repo)

    shift = ns.run_shift(
        session, repo,
        apply=args.apply,
        max_steps=args.max_steps,
        max_minutes=args.max_minutes,
        step_timeout_minutes=args.step_timeout,
        ignore_window=args.anytime,
        wait_for_window=args.wait,
        on_event=print,
    )
    print(f"\nended: {shift.ended}")
    print(f"{len(shift.steps)} step(s). Read them back with: swarm nightshift recap")
    # Non-zero when nothing was carried forward, so a wrapper script can tell.
    return 0 if any(s.dispatched for s in shift.steps) else 1


def _cmd_keepgoing(args) -> int:
    """Arm or disarm automatic continuation for a repository."""
    from . import keepgoing as kg
    from . import nightshift as ns

    repo = paths.working_tree(args.repo)

    if args.action == "on":
        kg.arm(repo)
        print(f"armed: {repo}")
        print("Sessions here will carry on by themselves instead of waiting for")
        print("\"keep going\". They hand back when the work names nothing outstanding,")
        print("asks you a question, or would touch production.")
        if not _keepgoing_hook_installed():
            print("\nThe Stop hook is NOT registered yet -- run: swarm install")
        return 0

    if args.action == "off":
        kg.disarm(repo)
        print(f"disarmed: {repo}")
        return 0

    armed = kg.armed_repos()
    print("armed repositories:" if armed else "no repositories are armed")
    for path in armed:
        print(f"  {path}")
    print(f"\nStop hook registered: {_keepgoing_hook_installed()}")

    recent = [e for e in ns.read_ledger() if e.get("event") == "keepgoing"][-12:]
    if recent:
        print("\nrecent decisions:")
        for entry in recent:
            mark = "->" if entry.get("kept_going") else "--"
            stamp = entry.get("at", "")[:16].replace("T", " ")
            print(f"  {stamp}  {mark} {entry.get('reason')}")
    return 0


def _keepgoing_hook_installed() -> bool:
    import json as _json

    try:
        data = _json.loads(paths.settings_path().read_text(encoding="utf-8"))
    except Exception:
        return False
    for entry in data.get("hooks", {}).get("Stop", []):
        for hook in entry.get("hooks", []):
            if "swarm-keepgoing" in str(hook.get("command", "")):
                return True
    return False


def _cmd_shells(args) -> int:
    """What every live session is running in a shell, right now."""
    from . import shells as shells_mod

    found = shells_mod.live()
    print(shells_mod.render(found))
    if args.services:
        services = [s for s in found if s.kind == "service"]
        if services:
            print("\nMCP services:")
            for s in services:
                print(f"  {s.project or '?':14} {shells_mod._clock(s.seconds):>7}  {s.command[:70]}")
    return 0


def _cmd_show(args) -> int:
    import shutil
    from datetime import datetime

    from . import reader
    from . import watch as watch_mod

    session = args.session or reader.current_session()
    if session is None:
        print("no session transcript found")
        return 1
    runs = reader.read_session(session, redact=args.redact)
    width = shutil.get_terminal_size((88, 24)).columns
    print(watch_mod.render(runs, now=datetime.now().astimezone(), width=width))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="swarm", description="Agent event stream.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="validate a stream and report what is missing")
    p_doctor.add_argument("path", nargs="?")
    p_doctor.set_defaults(fn=_cmd_doctor)

    sub.add_parser("list", help="list captured streams").set_defaults(fn=_cmd_list)

    p_install = sub.add_parser("install", help="register hooks in settings.json")
    p_install.add_argument("--command", help="absolute path to the swarm-hook entrypoint")
    p_install.set_defaults(fn=_cmd_install)

    sub.add_parser("uninstall", help="remove swarm hooks").set_defaults(fn=_cmd_uninstall)

    p_watch = sub.add_parser("watch", help="live view of this session's agents")
    p_watch.add_argument("session", nargs="?", help="session id (default: the most recent)")
    p_watch.add_argument("--interval", type=float, default=2.0)
    p_watch.add_argument("--redact", action="store_true", help="hide dispatch labels")
    p_watch.set_defaults(fn=_cmd_watch)

    p_monitor = sub.add_parser("monitor", help="what needs attention in this run, with evidence")
    p_monitor.add_argument("session", nargs="?")
    p_monitor.add_argument("--goal", help="what this run is trying to achieve")
    p_monitor.add_argument("--redact", action="store_true")
    p_monitor.set_defaults(fn=_cmd_monitor)

    p_night = sub.add_parser(
        "nightshift",
        help="carry a run forward unattended, and recap what happened")
    p_night.add_argument("action", choices=("start", "recap", "status", "stop"))
    p_night.add_argument("session", nargs="?")
    p_night.add_argument("--repo", default=".", help="the working tree to continue in")
    p_night.add_argument("--apply", action="store_true",
                         help="actually dispatch (default shows the first step only)")
    p_night.add_argument("--anytime", action="store_true",
                         help="run outside the night window")
    p_night.add_argument("--wait", action="store_true",
                         help="arm now and sleep until the window opens")
    p_night.add_argument("--max-steps", type=int, default=6, dest="max_steps")
    p_night.add_argument("--max-minutes", type=int, default=240, dest="max_minutes")
    p_night.add_argument("--step-timeout", type=int, default=45, dest="step_timeout",
                         help="minutes one continuation may take")
    p_night.add_argument("--since-hours", type=float, dest="since_hours",
                         help="recap only: how far back to read")
    p_night.set_defaults(fn=_cmd_nightshift)

    p_keep = sub.add_parser(
        "keepgoing", help="let sessions in a repo carry on without being asked")
    p_keep.add_argument("action", nargs="?", default="status",
                        choices=("on", "off", "status"))
    p_keep.add_argument("--repo", default=".")
    p_keep.set_defaults(fn=_cmd_keepgoing)

    p_shells = sub.add_parser("shells", help="what each session is running in a shell now")
    p_shells.add_argument("--services", action="store_true",
                          help="also list the MCP servers each session started")
    p_shells.set_defaults(fn=_cmd_shells)

    p_show = sub.add_parser("show", help="print the agent list once and exit")
    p_show.add_argument("session", nargs="?")
    p_show.add_argument("--redact", action="store_true")
    p_show.set_defaults(fn=_cmd_show)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
