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


def test_creating_over_an_existing_tree_refuses_rather_than_reusing(repo_with_remote):
    wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
    with pytest.raises(FileExistsError):
        wt.create(repo_with_remote, slug="spec-alpha", story_id="1")
