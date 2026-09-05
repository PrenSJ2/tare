"""The boundary, tested by actually pushing at it.

The tool policy is widened, so this hook is what stands between an unattended
run and the operator's remotes. The precedent for how to test it is the `curl`
finding: an earlier claim that the tool policy held was false because the test
only ran a command that was already on the denylist, and proved nothing about
one that was on neither list.

So these tests push refs that are NOT obviously wrong -- `nightshift-evil/x`
looks like the namespace and is not in it -- and they push against a real bare
remote rather than asserting on the hook's text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarm import worktree as wt


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo_with_remote(tmp_path):
    """A real repository with one commit and a real bare remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    repo = tmp_path / "work"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    return repo


def test_it_creates_a_worktree_on_a_namespaced_branch(repo_with_remote):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    assert tree.branch == "nightshift/spec-alpha/1"
    assert tree.path.is_dir()
    assert (tree.path / "README.md").is_file()
    head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree.path).stdout.strip()
    assert head == "nightshift/spec-alpha/1"


def test_pushing_the_namespaced_branch_is_allowed(repo_with_remote):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    (tree.path / "new.txt").write_text("work\n")
    _git("add", "new.txt", cwd=tree.path)
    _git("commit", "-qm", "work", cwd=tree.path)

    pushed = _git("push", "origin", "HEAD:refs/heads/nightshift/spec-alpha/1", cwd=tree.path)
    assert pushed.returncode == 0, pushed.stderr


# --- the boundary -----------------------------------------------------------
#
# Each of these pushes a ref that is NOT on any denylist and NOT obviously
# wrong. That is the lesson from the `curl` finding: testing only the
# already-forbidden case proves nothing about the case nobody listed.

@pytest.mark.parametrize("refspec, why", [
    ("HEAD:refs/heads/main", "the default branch, reached via an explicit refspec"),
    ("HEAD:refs/heads/nightshift-evil/x", "looks like the namespace, is not in it"),
    ("HEAD:refs/heads/anightshift/x", "namespace as a suffix rather than a prefix"),
    ("HEAD:refs/heads/nightshift", "the namespace with no story under it"),
    ("HEAD:refs/tags/v1", "not a branch at all"),
])
def test_the_hook_refuses_any_ref_outside_the_namespace(repo_with_remote, refspec, why):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    (tree.path / "new.txt").write_text("work\n")
    _git("add", "new.txt", cwd=tree.path)
    _git("commit", "-qm", "work", cwd=tree.path)

    pushed = _git("push", "origin", refspec, cwd=tree.path)
    assert pushed.returncode != 0, f"push of {refspec} succeeded ({why})"
    assert "refusing to push" in pushed.stderr


def test_the_hook_refuses_an_unterminated_final_line(repo_with_remote):
    """`while read` alone returns non-zero on a final line with no trailing
    newline -- that would SKIP the line, not refuse it. Git always
    newline-terminates its own input, so this is not reachable through a
    real `git push` today; it is tested directly against the hook because a
    fail-closed claim should hold even for input git doesn't currently send.
    """
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    hook_path = tree.path / wt.HOOKS_DIRNAME / "pre-push"

    result = subprocess.run(
        ["sh", str(hook_path)],
        input="refs/heads/x deadbeef refs/heads/main cafebabe",  # no trailing newline
        capture_output=True, text=True,
    )
    assert result.returncode != 0, "an unterminated final line was skipped, not refused"
    assert "refusing to push" in result.stderr


def test_install_hook_raises_and_cleans_up_when_scoping_fails(repo_with_remote, monkeypatch):
    """The regression this module exists to prevent: if `--worktree` scoping
    ever silently fell back to a repo-wide hook, nothing else in the suite
    would catch it. `install_hook` must raise instead -- and not leave its
    bookkeeping directory behind for a later `create` to trip over.
    """
    tree_path = wt.worktrees_root(repo_with_remote) / "manual"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", "manual-branch", str(tree_path), cwd=repo_with_remote)

    real_git = wt._git

    def _fake_git(repo, *args):
        if args[:2] == ("config", "--worktree"):
            return subprocess.CompletedProcess(list(args), 1, "", "simulated: cannot scope")
        return real_git(repo, *args)

    monkeypatch.setattr(wt, "_git", _fake_git)

    with pytest.raises(RuntimeError, match="could not scope"):
        wt.install_hook(tree_path)

    assert not (tree_path / wt.HOOKS_DIRNAME).exists()
    assert not (repo_with_remote / ".git" / "hooks" / "pre-push").exists()


def test_the_hook_does_not_leak_into_the_operators_own_repository(repo_with_remote):
    """The trap: worktrees share .git/hooks with the main working tree.

    An unscoped hook would restrict the human's own pushes -- a tool meant to
    contain an unattended run instead breaking the person who installed it.
    """
    wt.create(repo_with_remote, slug="spec-alpha", story_id="1")

    assert not (repo_with_remote / ".git" / "hooks" / "pre-push").exists()

    (repo_with_remote / "other.txt").write_text("by hand\n")
    _git("add", "other.txt", cwd=repo_with_remote)
    _git("commit", "-qm", "by hand", cwd=repo_with_remote)
    pushed = _git("push", "origin", "HEAD:refs/heads/main", cwd=repo_with_remote)
    assert pushed.returncode == 0, f"the operator's own push was blocked: {pushed.stderr}"


def test_a_branch_name_cannot_escape_the_namespace(repo_with_remote):
    """Slug and id come off disk and out of YAML. Neither is trusted."""
    assert wt.branch_for("../../evil", "1") == "nightshift/------evil/1"
    assert "/" not in wt.branch_for("a", "../x").split("/", 2)[2]


def test_orphans_reports_a_worktree_a_shift_left_behind(repo_with_remote):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    found = wt.orphans(repo_with_remote)
    assert [ref for _, ref in found] == ["refs/heads/nightshift/spec-alpha/1"]
    assert found[0][0].resolve() == tree.path.resolve()


def test_orphans_fails_closed_when_it_cannot_list_worktrees(tmp_path):
    """An empty result must never be the answer to a failure.

    `create`'s own refusal to reuse an existing directory tells an operator
    to run `swarm doctor` (which calls this). If `git worktree list` itself
    fails and `orphans` swallowed that into `[]`, doctor would report
    "nothing to clean up" about a repository it could not even read.
    """
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(RuntimeError):
        wt.orphans(not_a_repo)


def test_orphans_sees_a_worktree_whose_branch_moved(repo_with_remote):
    """A branch-only check goes blind the moment the branch does; the
    directory `create` left behind does not move with it.
    """
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    _git("checkout", "--detach", "-q", cwd=tree.path)

    found = wt.orphans(repo_with_remote)

    assert [p.resolve() for p, _ in found] == [tree.path.resolve()]


def test_create_cleans_up_when_install_hook_fails(repo_with_remote, monkeypatch):
    """An unprotected worktree left behind is worse than none -- it sits
    inside this module's own directory looking safe, and a retry would hit
    a stale FileExistsError forever with no way to self-heal.
    """
    def _boom(worktree_path):
        raise RuntimeError("simulated hook failure")

    monkeypatch.setattr(wt, "install_hook", _boom)

    with pytest.raises(RuntimeError, match="simulated hook failure"):
        wt.create(repo_with_remote, slug="spec-alpha", story_id="1")

    leftover = wt.worktrees_root(repo_with_remote) / "nightshift__spec-alpha__1"
    assert not leftover.exists()

    # and the failure is not durable: a retry with a working install_hook
    # succeeds rather than tripping FileExistsError over the cleaned-up path.
    monkeypatch.undo()
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    assert tree.path.is_dir()


def test_dispose_refuses_to_discard_uncommitted_work(repo_with_remote):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    (tree.path / "unsaved.txt").write_text("the only record of what it tried\n")
    message = wt.dispose(tree)
    assert "left in place" in message
    assert tree.path.is_dir()


def test_dispose_removes_a_clean_worktree(repo_with_remote):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    assert "removed" in wt.dispose(tree)
    assert not tree.path.exists()


def test_dispose_fails_closed_when_status_cannot_be_read(repo_with_remote):
    """A `git status` that errors out is treated as dirty, not clean.

    This is the one path in the module where guessing wrong destroys the
    only record of the night, so an unreadable tree must be left in place,
    never force-removed.
    """
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    ghost = wt.Worktree(path=tree.path / "does-not-exist", branch=tree.branch, repo=tree.repo)

    message = wt.dispose(ghost)

    assert "left in place" in message
    assert "could not read its status" in message
    # the real worktree this ghost points near was never touched
    assert tree.path.is_dir()


def test_dispose_with_force_removes_a_genuinely_dirty_worktree(repo_with_remote):
    """The escape hatch still works: `force=True` removes real uncommitted work."""
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    (tree.path / "unsaved.txt").write_text("discard me\n")

    message = wt.dispose(tree, force=True)

    assert "removed" in message
    assert not tree.path.exists()


def test_creating_over_an_existing_tree_refuses_rather_than_reusing(repo_with_remote):
    wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    with pytest.raises(FileExistsError):
        wt.create(repo_with_remote, slug="spec-alpha", story_id="1")


# --- I2(b): orphans must see what git's own bookkeeping no longer does ------

def test_orphans_finds_a_directory_git_no_longer_tracks_as_a_worktree(repo_with_remote):
    """The gap this fixes: `git worktree remove` (or anything else that
    drops the directory from git's own porcelain listing while leaving it on
    disk) used to make `orphans() == []` -- neither the branch signal nor the
    "under root" signal ever fired, because both were only ever applied to
    paths `git worktree list --porcelain` yielded in the first place. `create`
    still refuses the same path with `FileExistsError` forever; before this
    fix, `doctor` had nothing to say about why.
    """
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    # Simulate git's bookkeeping losing track of it while the directory
    # itself survives -- not achievable by deleting `.git/worktrees/<id>`
    # portably, so this drops the whole `.git` metadata's reference instead,
    # which is the same end state porcelain listing would see: nothing.
    subprocess.run(["git", "worktree", "remove", "--force", str(tree.path)],
                   cwd=repo_with_remote, capture_output=True)
    tree.path.mkdir(parents=True)
    (tree.path / "leftover.txt").write_text("still here\n")

    found = wt.orphans(repo_with_remote)

    assert [p.resolve() for p, _ in found] == [tree.path.resolve()]
    assert found[0][1] == "untracked"
    with pytest.raises(FileExistsError):
        wt.create(repo_with_remote, slug="spec-alpha", story_id="1")


def test_orphans_does_not_double_report_a_worktree_git_still_tracks(repo_with_remote):
    """The filesystem pass must add to what the porcelain pass already found,
    not duplicate it."""
    wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    found = wt.orphans(repo_with_remote)
    assert len(found) == 1


# --- per-call timeout: cheap queries stay short, checkouts get longer ------
#
# `worktree add` is a full checkout and `worktree remove --force` deletes one
# including ignored files -- neither is a metadata read like `rev-parse` or
# `status`, and a Git-LFS smudge filter can turn `add` into a network fetch.
# One flat 15s timeout for every call risked SIGKILLing a slow-but-legitimate
# checkout mid-write, leaving exactly the leftover directory finding 1 is
# about. These tests assert the SPLIT exists, not that any particular number
# is "enough" -- see worktree.py's `_LONG_GIT_TIMEOUT` comment for that.

def _recording_run(monkeypatch):
    """Wrap `subprocess.run` inside `swarm.worktree` to capture the `timeout`
    kwarg of every call, while still actually running the command -- these
    tests must exercise the real `create`/`dispose` control flow, not a
    stub that never touches git."""
    calls: list[tuple[tuple, int | None]] = []
    real_run = subprocess.run

    def _wrapped(args, *a, **kw):
        calls.append((tuple(args), kw.get("timeout")))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(wt.subprocess, "run", _wrapped)
    return calls


def test_create_uses_the_long_timeout_for_worktree_add_only(repo_with_remote, monkeypatch):
    calls = _recording_run(monkeypatch)

    wt.create(repo_with_remote, slug="spec-alpha", story_id="1")

    by_args = {args: timeout for args, timeout in calls}
    add_call = next(args for args in by_args if "add" in args and "worktree" in args)
    rev_parse_call = next(args for args in by_args if "rev-parse" in args)

    assert by_args[add_call] == wt._LONG_GIT_TIMEOUT
    assert by_args[rev_parse_call] == wt._SHORT_GIT_TIMEOUT
    assert wt._LONG_GIT_TIMEOUT > wt._SHORT_GIT_TIMEOUT


def test_dispose_uses_the_long_timeout_for_worktree_remove_only(repo_with_remote, monkeypatch):
    tree = wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    calls = _recording_run(monkeypatch)

    wt.dispose(tree)

    by_args = {args: timeout for args, timeout in calls}
    remove_call = next(args for args in by_args if "remove" in args and "worktree" in args)
    status_call = next(args for args in by_args if "status" in args)

    assert by_args[remove_call] == wt._LONG_GIT_TIMEOUT
    assert by_args[status_call] == wt._SHORT_GIT_TIMEOUT
