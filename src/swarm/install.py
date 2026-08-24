"""Register and remove swarm's hooks in settings.json.

settings.json is the operator's file and holds unrelated configuration. It is
never edited in place: parse, back up, write, replace. A malformed file raises
rather than being overwritten -- losing someone's settings to a tool that only
observes would be indefensible.
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from swarm import paths

EVENTS = ("SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "SessionEnd")

_MARKER = "swarm-hook"

# The Stop hook is registered separately and under its own name, because it is
# a different kind of thing: the recording hooks are required to be silent and
# exit 0, while this one exits 2 on purpose to block a stop. Sharing an
# entrypoint would put a hook that can speak into every event that must not.
_KEEPGOING_MARKER = "swarm-keepgoing"
_KEEPGOING_EVENT = "Stop"
# Generous next to the recording hooks' 2s: this one reads a transcript tail
# and the operator is waiting on it. Still far below any model call, which is
# why the decision is made from text already in hand.
_KEEPGOING_TIMEOUT = 10


def _load() -> dict:
    path = paths.settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} could not be parsed as JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} could not be parsed as a JSON object")
    return data


def _save(data: dict) -> None:
    path = paths.settings_path()
    if path.exists():
        # Microseconds, not seconds: an install immediately followed by an
        # uninstall lands in the same second, and a colliding name would leave
        # the operator holding a backup of the post-install state rather than
        # their original -- failing the one scenario a backup exists for.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        shutil.copy2(path, path.with_suffix(f".json.bak{stamp}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # never replace with garbage
    tmp.replace(path)


def _is_swarm(entry, event: str) -> bool:
    """Is this hook entry one swarm registered for this event?

    Deliberately strict on both sides. A bare substring check would delete an
    unrelated hook whose command merely mentions swarm-hook, and matching
    loosely across every event key widens the blast radius far beyond what
    install() ever touched. So: the entry must be shaped like ours, name the
    swarm-hook executable, and end with this exact event name.
    """
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks") or []:
        command = (hook or {}).get("command", "")
        if _MARKER in command and command.strip().endswith(f" {event}"):
            return True
    return False


def install_keepgoing(hook_command: str) -> None:
    """Register the Stop hook that lets an armed repo's sessions carry on."""
    if Path(hook_command).name != _KEEPGOING_MARKER:
        raise ValueError(
            f"hook command must be named {_KEEPGOING_MARKER!r}, "
            f"got {Path(hook_command).name!r}"
        )
    data = _load()
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault(_KEEPGOING_EVENT, [])
    # Other tools own Stop hooks too -- the console has one. Only ours is
    # replaced, and the rest are left exactly as they are.
    entries[:] = [e for e in entries if not _is_keepgoing(e)]
    entries.append({"hooks": [{
        "type": "command",
        "command": f'"{hook_command}"',
        "timeout": _KEEPGOING_TIMEOUT,
    }]})
    _save(data)


def uninstall_keepgoing() -> None:
    data = _load()
    entries = data.get("hooks", {}).get(_KEEPGOING_EVENT)
    if not entries:
        return
    entries[:] = [e for e in entries if not _is_keepgoing(e)]
    if not entries:
        data["hooks"].pop(_KEEPGOING_EVENT, None)
    _save(data)


def _is_keepgoing(entry: dict) -> bool:
    for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
        if _KEEPGOING_MARKER in str(hook.get("command", "")):
            return True
    return False


def install(hook_command: str) -> list[str]:
    # The marker lives in the command, so the executable must carry the name.
    # Without this, `swarm install --command /some/other/name` would register
    # entries that uninstall could never identify and remove -- orphaned in the
    # operator's settings file forever.
    if Path(hook_command).name != _MARKER:
        raise ValueError(
            f"hook command must be named {_MARKER!r}, got {Path(hook_command).name!r}"
        )
    data = _load()
    hooks = data.setdefault("hooks", {})
    for event in EVENTS:
        existing = [e for e in hooks.get(event, []) if not _is_swarm(e, event)]
        # Quoted: an unquoted command containing a space (a path with one, on
        # a machine where that happens) would register a hook that splits on
        # the wrong word and never runs again -- permanently, and silently,
        # since hooks fail open.
        existing.append({
            "hooks": [{"type": "command", "command": f'"{hook_command}" {event}'}]
        })
        hooks[event] = existing
    _save(data)
    return list(EVENTS)


def registered_command() -> str | None:
    """The absolute path swarm's hooks currently point at, or None.

    Reads back whatever the last install() wrote. `swarm doctor` uses this to
    check that path still exists -- a reinstall into a different venv freezes
    a new path only if `swarm install` is run again; the old registration
    otherwise stops firing with nothing to report it. Degrades to None on any
    malformed settings.json rather than raising: this is a reporting path,
    not one that may ever crash the reader.
    """
    try:
        data = _load()
    except ValueError:
        return None
    hooks = data.get("hooks", {})
    for event in EVENTS:
        for entry in hooks.get(event, []) or []:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks") or []:
                command = (hook or {}).get("command", "")
                if _MARKER in command and command.strip().endswith(f" {event}"):
                    body = command.strip()[: -len(f" {event}")].strip()
                    if len(body) >= 2 and body[0] == body[-1] == '"':
                        body = body[1:-1]
                    return body
    return None


def uninstall() -> list[str]:
    data = _load()
    hooks = data.get("hooks", {})
    touched = []
    # Only the five events install() writes. Scanning every key would let
    # swarm delete hooks it never created.
    for event in EVENTS:
        if event not in hooks:
            continue
        kept = [e for e in hooks[event] if not _is_swarm(e, event)]
        if len(kept) != len(hooks[event]):
            touched.append(event)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not touched:
        # Nothing of swarm's was registered. On a machine with no
        # settings.json, saving here would create one containing just {}; on
        # a machine with one, it would reformat the operator's file and drop
        # a needless backup for a no-op. Leave the file untouched.
        return touched
    if not hooks:
        data.pop("hooks", None)
    _save(data)
    return touched
