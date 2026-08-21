"""The git-backed store for shelved user-authored skills and agents.

This module holds the ONLY copy of whatever it stashes. Everything here is
written the way it is because the first version of this file lost data, or
lied about having kept it, in a specific and previously-observed way. Each
rule below has a matching test for the failure it prevents; see
the design notes for the incident each one is named after.

Layout, all under `paths.vault_dir()`:
    manifest.json          {"skills": {key: {"restored": bool}}, "agents": {...}}
    skills/<name>/         a stashed skill directory (contains SKILL.md)
    agents/<name>.md       a stashed agent file

The manifest is keyed by *filesystem* name (`source.name` for skills,
`source.stem` for agents). The rest of the system -- the operator typing a
command, the graph's node names -- uses the *frontmatter* name instead, and
the two diverge in real capabilities (e.g. `agents/architect-review.md`
declaring `name: architect-reviewer`). `resolve_name` is the one place that
reconciles them; nothing else in this module, or outside it, should
re-derive that mapping.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import frontmatter, paths

KINDS = ("skills", "agents")

# Every git invocation gets an explicit identity. Relying on the caller's
# global `user.name`/`user.email` means `git commit` fails on a machine (or
# CI sandbox, or fresh test tmp dir) where that was never configured -- and
# per rule 6 below, that failure must raise, not vanish.
_GIT_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME": "harness",
    "GIT_AUTHOR_EMAIL": "harness@localhost",
    "GIT_COMMITTER_NAME": "harness",
    "GIT_COMMITTER_EMAIL": "harness@localhost",
}


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in the vault. Silent failure here is the defect rule 6 exists
    to prevent: `check=False` everywhere let a failed `git init` still leave
    `ensure_vault` writing the manifest and `stash` moving files, while
    `is_initialized()` stayed false forever -- voiding the "a later scan
    reconciles this" contract other modules depend on. Callers that truly
    want to probe (e.g. "is there anything staged") pass check=False
    themselves and inspect the result.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV_OVERRIDES},
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _commit_if_changed(root: Path, message: str) -> None:
    """`git commit` exits non-zero when the index is clean, which would
    otherwise turn legitimate no-ops (restoring an already-restored symlink,
    initializing an already-initialized vault) into raised errors. Check
    first rather than swallowing the commit's own exit code.
    """
    staged = _git(["diff", "--cached", "--quiet"], cwd=root, check=False)
    if staged.returncode == 0:
        return
    _git(["commit", "-m", message], cwd=root)


def _read_text(path: Path) -> str:
    # errors="replace" is load-bearing: strict UTF-8 decoding raises
    # UnicodeDecodeError, which is not an OSError, so it escapes uncaught
    # through recovery paths that only guard against OSError. A capability
    # with an odd byte in it must still degrade to a readable (if slightly
    # mangled) name instead of crashing the whole resolution.
    return path.read_text(encoding="utf-8", errors="replace")


def _require_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")


def _vault_entry_path(key: str, kind: str) -> Path:
    root = paths.vault_dir()
    if kind == "skills":
        return root / "skills" / key
    return root / "agents" / f"{key}.md"


def _live_entry_path(key: str, kind: str) -> Path:
    if kind == "skills":
        return paths.skills_dir() / key
    return paths.agents_dir() / f"{key}.md"


def _manifest_source_path(key: str, kind: str) -> Path:
    """The frontmatter-bearing file for a manifest entry: the SKILL.md
    inside a stashed skill directory, or the agent file itself."""
    entry = _vault_entry_path(key, kind)
    if kind == "skills":
        return entry / "SKILL.md"
    return entry


def _declared_name(key: str, kind: str) -> str | None:
    """The frontmatter `name` of a vaulted entry, or None if the file is
    missing or will not parse. Falling back to None (and letting the caller
    fall back to the filesystem key) is deliberate: this is the recovery
    path, and a capability tare cannot read must still resolve by its
    filesystem name rather than disappear.
    """
    path = _manifest_source_path(key, kind)
    try:
        text = _read_text(path)
    except OSError:
        return None
    fm, err = frontmatter.parse(text)
    if fm is None or not fm.name:
        return None
    return fm.name


def is_initialized() -> bool:
    """The shared predicate every other module gates on before touching the
    vault. A directory existing is not enough -- a process killed midway
    through `ensure_vault()` leaves a directory that passes `is_dir()` and
    then gets silently "completed" (and its state assumed) by whatever calls
    `manifest()` next. Require BOTH `.git` and `manifest.json`; `ensure_vault`
    creates the subdirectories before running `git init` specifically so
    that `.git` present implies the rest is too.

    `.git` is checked with `.exists()`, not `.is_dir()`: a linked worktree's
    `.git` is a *file* (containing a `gitdir:` pointer), and misreading that
    as "broken" would treat a perfectly good vault as uninitialized.
    """
    root = paths.vault_dir()
    return (root / ".git").exists() and (root / "manifest.json").exists()


def ensure_vault() -> Path:
    """Create the vault if it doesn't exist. Safe to call repeatedly --
    every step is guarded so a second call (or a call against a
    half-initialized vault left by a killed process) only finishes what's
    missing rather than re-doing or clobbering anything.

    Order matters: subdirectories are created before `git init` so that
    `is_initialized()`'s `.git`-implies-the-rest assumption actually holds.
    """
    root = paths.vault_dir()
    for kind in KINDS:
        (root / kind).mkdir(parents=True, exist_ok=True)

    if not (root / ".git").exists():
        _git(["init"], cwd=root)  # raises on failure -- rule 6

    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        _write_manifest_raw(root, {kind: {} for kind in KINDS})
        _git(["add", "-A"], cwd=root)
        # Commit the empty manifest so the recovery command named in
        # manifest()'s error message (`git checkout HEAD -- manifest.json`)
        # has something to check out even before the first stash.
        _commit_if_changed(root, "initialize vault")

    return root


def _write_manifest_raw(root: Path, data: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_manifest(data: dict) -> None:
    _write_manifest_raw(paths.vault_dir(), data)


def manifest() -> dict:
    """Read and validate the manifest. This is the ONLY index to what's
    stashed, so degrading silently (returning {} on a parse failure, say)
    would make every already-shelved capability invisible while looking
    like "nothing has ever been stashed." Raise instead, and name the exact
    git command that recovers the last-known-good copy -- the manifest is
    committed on every write, so `git checkout HEAD -- manifest.json` always
    has something to restore from.
    """
    path = paths.vault_dir() / "manifest.json"
    recovery = f"git -C {paths.vault_dir()} checkout HEAD -- manifest.json"
    try:
        text = _read_text(path)
    except OSError as exc:
        raise ValueError(f"vault manifest at {path} could not be read ({exc}); recover with: {recovery}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"vault manifest at {path} is corrupt ({exc}); recover with: {recovery}") from exc

    if not isinstance(data, dict) or not all(isinstance(data.get(kind), dict) for kind in KINDS):
        raise ValueError(f"vault manifest at {path} is not a valid mapping; recover with: {recovery}")

    return data


def resolve_name(name: str, kind: str) -> str | None:
    """The single name-resolution rule for the whole system. The manifest
    is keyed by filesystem name; everything else (the operator, the graph)
    uses the frontmatter-declared name. Map an operator-supplied name to a
    manifest key by matching either the key itself or that key's declared
    name -- read fresh from the vaulted file every time, never cached in the
    manifest, because a second source of truth for the same fact is exactly
    what would drift.
    """
    _require_kind(kind)
    entries = manifest().get(kind, {})

    matches: list[str] = []
    for key in entries:
        if key == name:
            matches.append(key)
            continue
        declared = _declared_name(key, kind)
        if declared is not None and declared == name:
            matches.append(key)

    if not matches:
        return None
    if len(matches) > 1:
        raise LookupError(f"{name!r} resolves to more than one vaulted {kind} entry: {matches}")
    return matches[0]


def is_stashed(name: str, kind: str) -> bool:
    _require_kind(kind)
    if not is_initialized():
        return False
    try:
        return resolve_name(name, kind) is not None
    except LookupError:
        # Ambiguous still means "yes, something is stashed under this name" --
        # the ambiguity is resolve_name's problem to raise on, not this
        # predicate's problem to hide.
        return True


def stash(source: Path, kind: str) -> Path:
    """Move `source` into the vault and record it in the manifest.

    Read and validate the manifest BEFORE moving anything. The original
    version called `shutil.move` first: a corrupt manifest then left the
    capability gone from `~/.claude/skills`, absent from the vault index,
    and the command still reported success. Validating first means a
    corrupt manifest raises with the source untouched.
    """
    _require_kind(kind)
    source = Path(source)
    if not source.exists():
        raise FileNotFoundError(source)

    root = ensure_vault()
    data = manifest()  # raises before anything moves, per the rule above

    name = source.name if kind == "skills" else source.stem
    dest = _vault_entry_path(name, kind)
    if dest.exists():
        raise FileExistsError(f"vault already holds a {kind} entry named {name!r} at {dest}")

    shutil.move(str(source), str(dest))

    data.setdefault(kind, {})[name] = {"restored": False}
    _write_manifest(data)
    _git(["add", "-A"], cwd=root)
    _commit_if_changed(root, f"stash {kind}/{name}")
    return dest


def restore(name: str, kind: str) -> Path:
    """Symlink the vault copy onto the live load path. The copy stays in
    the vault -- this is a link, not a move -- and the manifest entry's
    `restored` flag flips to True.

    Must not claim success over a pre-existing foreign path. The original
    version returned early on any existing symlink without checking where
    it pointed, so `activate` reported success while the thing actually
    loaded was somebody else's capability. Here: a non-symlink occupant
    raises, and a symlink that does not resolve into this vault entry
    raises too -- only a symlink that already points at the vault copy (or
    no occupant at all) is treated as success.
    """
    _require_kind(kind)
    key = resolve_name(name, kind)
    if key is None:
        raise LookupError(f"{name!r} is not a vaulted {kind} entry")

    root = paths.vault_dir()
    source = _vault_entry_path(key, kind)
    source_resolved = source.resolve()
    live = _live_entry_path(key, kind)

    if live.is_symlink():
        try:
            target_resolved = live.resolve()
        except OSError:
            target_resolved = None
        if target_resolved != source_resolved:
            raise FileExistsError(
                f"{live} is already a symlink to {target_resolved}, not the vault copy at "
                f"{source_resolved}; refusing to report success over someone else's capability"
            )
        # Already correctly restored -- idempotent, fall through to the
        # manifest update below (which is itself a no-op if already True).
    elif live.exists():
        raise FileExistsError(f"{live} already exists and is not a symlink; refusing to overwrite it")
    else:
        live.parent.mkdir(parents=True, exist_ok=True)
        live.symlink_to(source, target_is_directory=(kind == "skills"))

    data = manifest()
    entry = data.setdefault(kind, {}).setdefault(key, {})
    if entry.get("restored") is not True:
        entry["restored"] = True
        _write_manifest(data)
        _git(["add", "-A"], cwd=root)
        _commit_if_changed(root, f"restore {kind}/{key}")
    return live


def unrestore(name: str, kind: str) -> None:
    """Remove the live symlink and flip `restored` back to False. Only ever
    removes a symlink that resolves into this exact vault entry -- a
    foreign file or a symlink pointing elsewhere is left alone, for the same
    reason `restore` refuses to overwrite one (rule 7): unrestore is not
    licensed to destroy something it did not create.
    """
    _require_kind(kind)
    key = resolve_name(name, kind)
    if key is None:
        raise LookupError(f"{name!r} is not a vaulted {kind} entry")

    root = paths.vault_dir()
    source = _vault_entry_path(key, kind)
    source_resolved = source.resolve()
    live = _live_entry_path(key, kind)

    if live.is_symlink():
        try:
            target_resolved = live.resolve()
        except OSError:
            target_resolved = None
        if target_resolved != source_resolved:
            raise FileExistsError(
                f"{live} is a symlink to {target_resolved}, not the vault copy at {source_resolved}; "
                "refusing to remove a link unrestore did not create"
            )
        live.unlink()
    elif live.exists():
        raise FileExistsError(f"{live} exists and is not a symlink; refusing to remove it")
    # else: nothing at the live path already -- idempotent no-op.

    data = manifest()
    entry = data.setdefault(kind, {}).setdefault(key, {})
    if entry.get("restored") is not False:
        entry["restored"] = False
        _write_manifest(data)
        _git(["add", "-A"], cwd=root)
        _commit_if_changed(root, f"unrestore {kind}/{key}")
