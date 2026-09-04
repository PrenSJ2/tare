"""The boundary, which is a git worktree and one hook.

The tool policy this loop runs under is wide by choice, and `nightshift`'s own
docstring calls the narrow allowlist "the only real control". Widening it
without putting something in its place would leave no control at all, so this
module is that replacement.

## What it buys, and what it does not

A worktree buys **reviewability, not confinement**. Nothing merges; every
night's work is a branch and a diff read in the morning. Under the widened
policy, filesystem writes outside the repository and network egress are
unconstrained, and this module does not pretend otherwise.

The pre-push hook is a guardrail, not containment, and the distinction
matters: it stops an agent that pushes somewhere it did not mean to, not one
that is trying to push somewhere it is not supposed to. The hook is a file
inside the worktree the agent has write access to, so an agent that wants
around it has several one-line ways through: `git push --no-verify`;
`git -c core.hooksPath= push`; overriding config for the single invocation
with `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`; or simply
deleting `.nightshift-hooks/pre-push` before pushing. Each of those was
verified against this module's own hook. None require anything beyond the
tool access an unattended run already has.

That does not make the hook worthless. The realistic failure mode for a
non-adversarial agent is an accident, not an escape attempt: `git push
origin HEAD:main` typed from habit, a stray `--tags`, a `--mirror` meant for
somewhere else. Against exactly that class of mistake the hook is
deterministic and turns it into a hard refusal instead of a successful push
to the wrong ref. It is not a defense against an agent that is trying to get
past it, and this module does not claim otherwise.

## Why the restriction is a hook and not an --allowedTools pattern

`--allowedTools` matches command prefixes. `Bash(git push:*)` is all-or-
nothing and `Bash(git push origin nightshift/:*)` walks straight through on
`git push origin HEAD:main`, which has the same prefix up to the point where
it stops mattering. A refspec restriction cannot be written as a prefix, so it
is enforced where refspecs actually exist: a `pre-push` hook that reads the
remote ref off stdin and exits non-zero for anything outside
`refs/heads/nightshift/`. Deterministic and inspectable for the refspecs it
actually receives from git, and fails closed on them: an unmatched or
malformed line is a refusal, not a pass. That is a narrower claim than
"cannot be bypassed" -- see above for what it does not defend against.

## The trap this module exists to avoid

A worktree does NOT get its own hooks directory. `.git/hooks` lives in the
common directory and is shared with the main working tree, so writing a
`pre-push` there would silently install it into the operator's real
repository -- a tool meant to restrict an unattended run instead breaking the
human's own pushes.

The fix is `git config --worktree core.hooksPath`, which needs
`extensions.worktreeConfig=true` set on the repository first. That extension is
set here, deliberately and once: it is a repository-level change, it is the
only one this module makes, and it is inert for anyone not using worktrees.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_NAMESPACE = "nightshift"
ALLOWED_REF_PREFIX = f"refs/heads/{BRANCH_NAMESPACE}/"

# Where the per-worktree hooks live, relative to the worktree root.
HOOKS_DIRNAME = ".nightshift-hooks"

# The hook. `pre-push` receives one line per ref on stdin:
#   <local ref> <local sha> <remote ref> <remote sha>
# The remote ref is the one that matters -- it is what the push will actually
# write, and it is the field `git push origin HEAD:main` sets to
# `refs/heads/main` no matter what the local branch is called.
PRE_PUSH_HOOK = f"""#!/bin/sh
# Installed by swarm.worktree. Do not edit; it is rewritten on every create.
#
# Refuses any push outside {ALLOWED_REF_PREFIX}. This is the boundary that
# replaces the narrow tool allowlist, so it fails closed: an unreadable line
# is a refusal, not a pass -- including a final line with no trailing
# newline, which plain `while read` would otherwise silently skip rather
# than refuse. `|| [ -n "$remote_ref" ]` is what makes the loop body run one
# more time for that line before `read` finally reports end-of-input.
while read -r local_ref local_sha remote_ref remote_sha || [ -n "$remote_ref" ]
do
    case "$remote_ref" in
        {ALLOWED_REF_PREFIX}?*)
            ;;
        *)
            echo "swarm: refusing to push '$remote_ref'." >&2
            echo "swarm: unattended runs may only push {ALLOWED_REF_PREFIX}*" >&2
            exit 1
            ;;
    esac
done
exit 0
"""

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    repo: Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def branch_for(slug: str, story_id: str) -> str:
    """`nightshift/<slug>/<id>`.

    Both components are sanitised: a slug is a directory name and an id comes
    from a YAML file, and neither is trusted to be a safe ref component. A
    branch called `nightshift/../../main` would defeat the whole hook.
    """
    safe_slug = _UNSAFE.sub("-", slug) or "unknown"
    safe_id = _UNSAFE.sub("-", story_id) or "unknown"
    return f"{BRANCH_NAMESPACE}/{safe_slug}/{safe_id}"


def worktrees_root(repo: Path) -> Path:
    """Sibling of the repo, not inside it: a worktree inside its own
    repository shows up in `git status` and in every glob the run makes."""
    return repo.parent / f".{repo.name}-nightshift"


def install_hook(worktree_path: Path) -> Path:
    """Write the pre-push hook and point THIS worktree at it.

    `--worktree` is the whole point: without it, `core.hooksPath` is a
    repository-wide setting and this would redirect the operator's own hooks.
    """
    hooks = worktree_path / HOOKS_DIRNAME
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(PRE_PUSH_HOOK, encoding="utf-8")
    hook.chmod(0o755)

    # Repository-level, and required before `config --worktree` does anything.
    _git(worktree_path, "config", "extensions.worktreeConfig", "true")
    result = _git(worktree_path, "config", "--worktree", "core.hooksPath", str(hooks))
    if result.returncode != 0:
        # Leave nothing behind: an operator or a later `create` should never
        # find a `.nightshift-hooks` directory whose hook was never actually
        # wired up. `rmdir` rather than a recursive remove -- this directory
        # holds nothing but the file this function just wrote.
        hook.unlink(missing_ok=True)
        try:
            hooks.rmdir()
        except OSError:
            pass
        raise RuntimeError(
            "could not scope core.hooksPath to the worktree "
            f"({result.stderr.strip()}); refusing to continue, because the "
            "fallback would install a pre-push hook into the operator's own "
            "repository")
    return hook


def create(repo: Path, *, slug: str, story_id: str, base: str = "HEAD") -> Worktree:
    """A worktree on `nightshift/<slug>/<id>`, with the boundary installed."""
    branch = branch_for(slug, story_id)
    path = worktrees_root(repo) / branch.replace("/", "__")
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        raise FileExistsError(
            f"{path} already exists -- a previous shift did not dispose of it. "
            "Run `swarm doctor` to see orphaned worktrees; nothing here will "
            "reuse a tree it did not create.")

    existing = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    args = ["worktree", "add", str(path)]
    args += [branch] if existing.returncode == 0 else ["-b", branch, base]

    result = _git(repo, *args)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")

    try:
        install_hook(path)
    except Exception:
        # An unprotected worktree is worse than none: it sits inside this
        # module's own directory looking like a safe tree, but nothing
        # stops a push from it. Remove it rather than leave it behind for a
        # retry to trip over as a stale FileExistsError with no self-heal.
        _git(repo, "worktree", "remove", "--force", str(path))
        raise
    return Worktree(path=path, branch=branch, repo=repo)


def dispose(tree: Worktree, *, force: bool = False) -> str:
    """Remove the worktree. Never discards uncommitted work silently.

    A tree holding uncommitted changes is left in place and reported. The work
    an unattended run did is the only record of what it was trying to do, and
    `--force` here would delete exactly the evidence someone gets up to read.

    The dirty check excludes `HOOKS_DIRNAME`: that directory is this module's
    own bookkeeping (the pre-push hook), always untracked, and present in
    every worktree it creates. Counting it would make every worktree look
    dirty and `dispose` would never actually remove one.

    A `git status` that fails outright -- the path is gone, the index is
    locked, the pathspec syntax is unsupported, the repo is in a broken state
    -- is treated as dirty, not clean. This is the one path in the module
    where guessing wrong destroys the only record of the night, so an
    unreadable tree is left in place rather than force-removed.
    """
    status = _git(tree.path, "status", "--porcelain", "--",
                  ".", f":!{HOOKS_DIRNAME}")
    if status.returncode != 0:
        return (f"left in place: {tree.path} -- could not read its status "
                f"({status.stderr.strip()[:200]})")
    dirty = status.stdout.strip()
    if dirty and not force:
        return f"left in place: {tree.path} has uncommitted changes"
    # `--force` unconditionally: `git worktree remove`'s own dirty check does
    # not know to exclude HOOKS_DIRNAME, so it would refuse a tree this
    # module has already judged clean above. The real decision was made by
    # the check above; this only bypasses git re-litigating it against our
    # own bookkeeping directory.
    result = _git(tree.repo, "worktree", "remove", "--force", str(tree.path))
    if result.returncode != 0:
        return f"could not remove {tree.path}: {result.stderr.strip()}"
    return f"removed {tree.path}"


def orphans(repo: Path) -> list[tuple[Path, str]]:
    """Worktrees a shift never cleaned up, however they got left that way.

    Matches on two, independent signals: a worktree whose branch is still
    under `refs/heads/nightshift/`, OR a worktree whose directory sits under
    `worktrees_root(repo)` -- the directory only `create` ever populates.
    The second signal exists because the first is not reliable: an agent
    that runs `git checkout --detach`, or renames or deletes its branch,
    makes the tree invisible to a branch-only check while the directory
    (and whatever it pushed, or didn't) stays right where `create` left it.
    `create`'s own refusal to reuse an existing directory points an operator
    here, so silently under-reporting is worse than reporting too much.
    """
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise RuntimeError(
            f"could not list worktrees for {repo} ({result.stderr.strip()}); "
            "refusing to report an empty list, because `create` sends an "
            "operator here on the assumption that an empty result means "
            "there is nothing to clean up")

    root = worktrees_root(repo).resolve()
    out: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def flush(path: Path | None, ref: str | None) -> None:
        if path is None:
            return
        namespaced = ref is not None and ref.startswith(ALLOWED_REF_PREFIX)
        try:
            path.resolve().relative_to(root)
            under_root = True
        except ValueError:
            under_root = False
        if (namespaced or under_root) and path not in seen:
            seen.add(path)
            out.append((path, ref if ref is not None else "detached"))

    current: Path | None = None
    current_ref: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            flush(current, current_ref)
            current = Path(line[len("worktree "):])
            current_ref = None
        elif line.startswith("branch "):
            current_ref = line[len("branch "):].strip()
        elif line == "":
            flush(current, current_ref)
            current, current_ref = None, None
    flush(current, current_ref)
    return out
