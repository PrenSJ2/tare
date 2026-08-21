"""Keep the fleet viewer running.

The viewer is `agent-flow` (github.com/patoles/agent-flow) — an existing,
maintained project that renders Claude Code's agent orchestration as a live
interactive graph. harness does not reimplement it. A half-built local server
was written here first and thrown away once agent-flow was found: it does more,
it is maintained, and shipping a worse duplicate would have been the wrong call.

harness's only job is making sure it is up.

## Why the checks below are strict

This runs from a SessionStart hook, on every session of every project, so it
has to behave like one:

- **Check the port before spawning.** A connection that is refused is the truth;
  a pid file survives a reboot and a `kill -9` and lies. Two servers racing for
  one port fail at exactly the moment someone is trying to see what is going on.
- **Never block.** `npx` may download the package on first run, which is slow.
  The child is detached and the parent returns immediately.
- **Never speak.** Anything printed from a hook lands in the operator's session.
  Every path here is silent and swallows its exceptions.
- **Never install anything implicitly.** `ensure()` starts a viewer that is
  already available; it does not fetch one. First use is an explicit
  `tare viewer --start`, so nothing arrives on the machine unasked.

## What it costs

agent-flow registers `PreToolUse`/`PostToolUse` hooks, which fire on *every*
tool call rather than once per agent. swarm deliberately refused that trade for
its own capture — several hundred records per run, and latency on every call.
It is the right trade for a live view and the wrong one for a background
recorder, which is why the two coexist rather than one replacing the other.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

HOST = "127.0.0.1"
# agent-flow's own default is 3001, which is also the default for a Next.js dev
# server -- and one of those had owned the port here for three days. Picking a
# less contested default avoids a collision that is invisible until you notice
# the viewer is empty.
DEFAULT_PORT = 3737
DISCOVERY_DIR_NAME = "agent-flow"
PACKAGE = "agent-flow-app"

# `npx -y agent-flow-app` resolves to whatever npx already has cached and never
# checks npm again -- so a viewer installed once stays on that version forever.
# Pinning `@latest` is what makes this a real upstream dependency: harness does
# not vendor or fork agent-flow, it runs whatever upstream currently publishes.
SPEC = f"{PACKAGE}@latest"

# Set to anything to stop harness touching the viewer at all.
DISABLE_ENV = "TARE_NO_VIEWER"


def _discovery_dir() -> Path:
    return Path.home() / ".claude" / DISCOVERY_DIR_NAME


def instances() -> list[dict]:
    """Live agent-flow instances, from its own discovery files.

    This is the authoritative check, and a plain port probe is not. agent-flow
    writes `{port, pid, workspace}` per instance and its hook forwards events
    only to instances found here with a live pid -- so "the viewer is up" means
    "a live instance is registered", not "something answers on a port".

    That distinction cost real time: a Next.js dev server had held agent-flow's
    default port for three days, so a port probe reported the viewer running
    while every agent event was being dropped on the floor, with nothing
    anywhere saying so.
    """
    found = []
    try:
        for path in _discovery_dir().glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            pid, port = data.get("pid"), data.get("port")
            if not isinstance(pid, int) or not isinstance(port, int):
                continue
            try:
                os.kill(pid, 0)          # signal 0 tests existence, kills nothing
            except OSError:
                continue                 # stale file; agent-flow prunes these itself
            found.append(data)
    except OSError:
        return []
    return found


def port_in_use(port: int) -> bool:
    """Is anything at all bound to this port -- ours or a stranger's?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            return probe.connect_ex((HOST, port)) == 0
    except OSError:
        return False


def is_up(port: int = DEFAULT_PORT) -> bool:
    """Is the VIEWER up -- not merely something on its port."""
    return bool(instances())


def url(port: int = DEFAULT_PORT) -> str:
    return f"http://{HOST}:{port}/"


def available() -> bool:
    """Can the viewer be launched at all? Requires npx on PATH."""
    return shutil.which("npx") is not None


def workspace_root() -> Path:
    """The directory the viewer should claim as its workspace.

    This is not cosmetic. agent-flow's hook forwards an event only when the
    event's `cwd` is inside the workspace its instance registered:

        if (resolvedCwd === ws || resolvedCwd.startsWith(ws + path.sep))

    and it silently exits otherwise. So an instance started from one project
    sees that project and nothing else -- and because `ensure()` binds a single
    port, the FIRST project to start it would capture the workspace and every
    other project's agents would vanish with no error anywhere.

    Spawning from home makes the workspace an ancestor of every project, which
    is what "see all the agents across my projects" actually requires.
    """
    return Path(os.environ.get("TARE_VIEWER_ROOT") or Path.home())


def _spawn(port: int, *, open_browser: bool) -> bool:
    args = ["npx", "-y", SPEC, "--port", str(port)]
    if not open_browser:
        args.append("--no-open")
    try:
        subprocess.Popen(
            args,
            cwd=str(workspace_root()),   # decides which events reach it — see above
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # outlives the hook process that started it
        )
        return True
    except OSError:
        return False


def ensure(port: int = DEFAULT_PORT) -> bool:
    """Start the viewer if it is not already up. Silent and non-blocking.

    Returns True if a viewer should be reachable shortly. Safe to call on every
    session start; never raises.
    """
    try:
        if os.environ.get(DISABLE_ENV):
            return False
        if is_up(port):
            return True
        if not available():
            return False
        return _spawn(port, open_browser=False)
    except Exception:
        return False


def versions() -> tuple[str | None, str | None]:
    """(cached, latest) versions of the viewer, or (None, None) if unknown.

    Reported rather than acted on, for the same reason `tare update` reports
    plugin drift rather than upgrading: knowing you are three versions behind is
    useful, and silently changing a running tool underneath someone is not.
    """
    cached = latest = None
    try:
        for pkg in (Path.home() / ".npm" / "_npx").glob(f"*/node_modules/{PACKAGE}/package.json"):
            cached = json.loads(pkg.read_text()).get("version")
            break
    except (OSError, json.JSONDecodeError):
        pass
    try:
        out = subprocess.run(["npm", "view", PACKAGE, "version"],
                             capture_output=True, text=True, timeout=6)
        if out.returncode == 0:
            latest = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return cached, latest


def status(port: int = DEFAULT_PORT) -> str:
    if os.environ.get(DISABLE_ENV):
        return f"disabled ({DISABLE_ENV} is set)"

    live = instances()
    if live:
        # NOTE the discovery file's `port` is agent-flow's internal event port,
        # which the hook POSTs to -- serving text/plain, not the UI. The web UI
        # is on the port it was launched with. Reporting the discovery port
        # sends the operator to a blank page.
        lines = []
        for inst in live:
            covers = inst.get("workspace", "?")
            lines.append(f"running at {url(port)}  (watching {covers})")
        cached, latest = versions()
        if cached and latest and cached != latest:
            lines.append(f"  {PACKAGE} {cached} cached, {latest} published — "
                         f"restart it to pick the new one up")
        elif cached:
            lines.append(f"  {PACKAGE} {cached} (upstream, not vendored)")
        return "\n".join(lines)

    if port_in_use(port):
        # Say this explicitly. It is the failure that looks like success.
        return (f"not running — and something else already holds port {port}. "
                f"Start it elsewhere: `tare viewer --start --port {port + 1}`")
    if not available():
        return "not running — needs `npx` on PATH (install Node.js)"
    return "not running — start it with `tare viewer --start`"
