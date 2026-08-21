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
  `harness viewer --start`, so nothing arrives on the machine unasked.

## What it costs

agent-flow registers `PreToolUse`/`PostToolUse` hooks, which fire on *every*
tool call rather than once per agent. swarm deliberately refused that trade for
its own capture — several hundred records per run, and latency on every call.
It is the right trade for a live view and the wrong one for a background
recorder, which is why the two coexist rather than one replacing the other.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess

HOST = "127.0.0.1"
DEFAULT_PORT = 3001
PACKAGE = "agent-flow-app"

# Set to anything to stop harness touching the viewer at all.
DISABLE_ENV = "HARNESS_NO_VIEWER"


def is_up(port: int = DEFAULT_PORT) -> bool:
    """Is something accepting connections on the viewer's port?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            return probe.connect_ex((HOST, port)) == 0
    except OSError:
        return False


def url(port: int = DEFAULT_PORT) -> str:
    return f"http://{HOST}:{port}/"


def available() -> bool:
    """Can the viewer be launched at all? Requires npx on PATH."""
    return shutil.which("npx") is not None


def _spawn(port: int, *, open_browser: bool) -> bool:
    args = ["npx", "-y", PACKAGE, "--port", str(port)]
    if not open_browser:
        args.append("--no-open")
    try:
        subprocess.Popen(
            args,
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


def status(port: int = DEFAULT_PORT) -> str:
    if os.environ.get(DISABLE_ENV):
        return f"disabled ({DISABLE_ENV} is set)"
    if is_up(port):
        return f"running at {url(port)}"
    if not available():
        return "not running — needs `npx` on PATH (install Node.js)"
    return "not running — start it with `harness viewer --start`"
