"""Write the `harness` skill and register the `SessionStart` hook that make
shelved capabilities findable again.

This is the safety layer around the vault gate: `harness lookup` only helps
an operator if something tells them it exists, and that "something" is this
skill plus the hook that keeps its own health checkable. Getting install/
uninstall wrong either breaks the operator's whole settings.json (rule 1) or
silently corrupts another tool's hook (rule 2) -- both defects the previous
build shipped. See the design notes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import paths

HOOK_EVENT = "SessionStart"

# The basename we look for in a registered hook command, and the exact
# argument it must be invoked with. Matching on basename -- never on a
# substring of the whole command -- is what keeps `uninstall` from deleting
# a foreign hook like "/opt/my-harness-tool/bin/run hookline" (rule 3):
# that command's basename is "run", not "harness", so it is correctly left
# alone even though the string "harness" and the suffix " hookline" both
# appear in it.
_HOOK_BASENAME = "harness"
_HOOK_ARG = "hookline"

# 21 words. Always loaded, so short is load-bearing -- this project's own
# audit flags a skill description over 30 words as bloat.
_SKILL_DESCRIPTION = (
    "Use before concluding that no skill or subagent exists for a task. "
    "Most capabilities are shelved and unlisted; this finds them."
)

# Every command named below must be a real `harness` subcommand (rule 6):
# scan, mine, tag, build, lookup, audit, graph, install, uninstall, hookline,
# activate, deactivate, doctor, vault.
_SKILL_BODY = f"""---
name: harness
description: {_SKILL_DESCRIPTION}
---

# harness

Most of this machine's skills and agents are shelved out of the always-loaded
context to save tokens -- moved, not deleted. Before telling the user no
matching capability exists, look here first.

## Finding a capability

```
harness lookup "<what you need>"
```

Searches the whole graph, live and vaulted alike, ranked by relevance and
past usage.

## Bringing a shelved capability back

```
harness activate <name-or-id>
```

Restores a vaulted skill or agent, or re-enables a disabled plugin, so it
loads normally again. `harness deactivate <name-or-id>` reverses a restore.

## Everything else

`harness scan`, `harness mine`, `harness tag` and `harness build` maintain the
underlying inventory; `harness audit` reports what the always-loaded context
costs; `harness doctor` checks for drift; `harness vault` shelves never-invoked
capabilities (dry run by default, `--apply` performs it). `harness install` /
`harness uninstall` manage this skill and its hook. `harness hookline` is what
Claude Code runs at session start -- not meant to be invoked by hand.

Every command named on this page exists. If one is missing, the skill is stale:
re-run `harness install`.
"""


def _is_our_hook_command(command: str) -> bool:
    """Shape+marker check for rule 3: a command is ours only if it is
    exactly `<something>/harness hookline` -- basename match on the first
    token, exact match (not `.endswith`) on the second. Anything with extra
    arguments, or whose executable's basename differs, is a foreign hook.
    """
    parts = command.split()
    return len(parts) == 2 and Path(parts[0]).name == _HOOK_BASENAME and parts[1] == _HOOK_ARG


def _group_command(group) -> str | None:
    """If `group` (one entry of settings["hooks"][HOOK_EVENT]) is shaped
    exactly like an entry we would have written -- a single command hook
    whose command is ours per `_is_our_hook_command` -- return that command
    string. Otherwise None. This shape check is what lets `uninstall` remove
    only entries we wrote (rule 4): a foreign matcher-group with a
    differently-shaped `hooks` list is never touched.
    """
    if not isinstance(group, dict):
        return None
    hooks_list = group.get("hooks")
    if not isinstance(hooks_list, list) or len(hooks_list) != 1:
        return None
    hook = hooks_list[0]
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return None
    command = hook.get("command")
    if isinstance(command, str) and _is_our_hook_command(command):
        return command
    return None


def _executable_path() -> str:
    """Absolute path to the `harness` executable this process is running
    as. Prefers the installed console script found on PATH (works
    regardless of which venv this process happens to be running from);
    falls back to `sys.argv[0]` resolved, for a dev checkout invoked as
    `python -m harness` or similar where no console script exists yet.
    """
    found = shutil.which(_HOOK_BASENAME)
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())


def _load() -> dict:
    """Read settings.json. A missing file is not an error -- it just means
    nothing has been configured yet, and install() creates it. Unparseable
    or non-object content IS an error: raise rather than silently treating
    a corrupt config as empty, which would make `install()` overwrite it
    with a fresh file that discards whatever the operator actually had.
    """
    path = paths.settings_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"settings.json at {path} could not be read: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings.json at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"settings.json at {path} is not a JSON object")
    return data


def _save(data: dict) -> None:
    """Write settings.json without ever risking a torn or corrupt file.

    Order, all load-bearing (rule 1):
    1. If a live file exists, copy it to a timestamped `.bak` first -- a
       recovery path that costs nothing and survives every failure mode
       below.
    2. Write the new content to a temp file in the same directory (so the
       final `os.replace` is on the same filesystem and therefore atomic).
    3. Re-parse the TEMP FILE -- not the in-memory `data` -- before it is
       ever allowed to become the live settings file. This is what catches
       a `data` that `json.dump` can serialize but that would round-trip
       into something unusable (or a filesystem that truncates the write).
    4. `os.replace` onto the real path: atomic on every platform this
       project targets, so a reader never observes a half-written file.
    """
    path = paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        shutil.copy2(path, path.with_name(f"{path.name}.{ts}.bak"))

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")

        reparsed = json.loads(tmp_path.read_text(encoding="utf-8"))
        if not isinstance(reparsed, dict):
            raise ValueError("refusing to save settings.json: content is not a JSON object")

        os.replace(tmp_path, path)
    finally:
        # Only reached without the replace having happened -- on success
        # tmp_path no longer exists at this path.
        if tmp_path.exists():
            tmp_path.unlink()


def registered_command() -> str | None:
    """The full `<exe> hookline` command currently registered as our
    SessionStart hook, or None if there isn't one. Does not check whether
    the executable still exists -- that's `is_installed`'s job (rule 2).
    """
    try:
        data = _load()
    except ValueError:
        return None
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return None
    for group in hooks.get(HOOK_EVENT, []) if isinstance(hooks.get(HOOK_EVENT), list) else []:
        command = _group_command(group)
        if command is not None:
            return command
    return None


def is_installed() -> bool:
    """True only if a hook entry of ours is registered AND the executable
    it points at still exists on disk (rule 2). A hook pointing at a
    deleted binary previously read as "installed" while being unable to
    ever run -- silently defeating the whole vault gate, which relies on
    the lookup skill actually being reachable.
    """
    command = registered_command()
    if command is None:
        return False
    exe = command.split()[0]
    return Path(exe).exists()


def install() -> None:
    """Write the `harness` skill and register the SessionStart hook.

    Idempotent: re-running replaces our own previous entry (e.g. after the
    executable moved) rather than accumulating a duplicate.
    """
    skill_path = paths.skill_install_path()
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(_SKILL_BODY, encoding="utf-8")

    command = f"{_executable_path()} {_HOOK_ARG}"

    data = _load()
    hooks = data.setdefault("hooks", {})
    session_start = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(session_start, list):
        raise ValueError(f"settings.json hooks.{HOOK_EVENT} is not a list; refusing to install over it")

    session_start[:] = [g for g in session_start if _group_command(g) is None]
    session_start.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
    hooks[HOOK_EVENT] = session_start

    _save(data)


def uninstall() -> None:
    """Remove exactly what `install()` wrote, and nothing else.

    The hook entry is matched by shape + marker + exact event name (rule 4)
    -- never by any looser test -- so a foreign SessionStart hook, or a
    harness-looking hook under a different event, survives untouched. The
    skill file (and its now-empty containing directory) is removed
    unconditionally; it is only ever written by `install()`.
    """
    data = _load()
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get(HOOK_EVENT), list):
        session_start = hooks[HOOK_EVENT]
        remaining = [g for g in session_start if _group_command(g) is None]
        if len(remaining) != len(session_start):
            if remaining:
                hooks[HOOK_EVENT] = remaining
            else:
                del hooks[HOOK_EVENT]
            if not hooks:
                data.pop("hooks", None)
            _save(data)

    skill_path = paths.skill_install_path()
    if skill_path.exists() or skill_path.is_symlink():
        skill_path.unlink()
        try:
            skill_path.parent.rmdir()  # only succeeds if nothing else is in there
        except OSError:
            pass
