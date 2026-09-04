"""Deciding whether a story is finished, as opposed to merely stopped.

Every default in this module leans toward NOT verified. An unverified story
is not a failure -- it stays open and comes back tomorrow, which costs one
night. A story wrongly marked done leaves the queue forever, which costs the
thing the queue was for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm import bmad, verify


def _story(tmp_path, **kw):
    spec = tmp_path / "spec-alpha"
    spec.mkdir(parents=True, exist_ok=True)
    defaults = dict(id="1", title="Add a limiter", description="D", spec_dir=spec)
    return bmad.Story(**{**defaults, **kw})


def test_a_clear_yes_verifies(tmp_path):
    v = verify.parse_verdict('{"verified": true, "reason": "all three criteria hold", "unmet": []}')
    assert v.verified is True
    assert "three criteria" in v.reason


def test_a_clear_no_does_not_verify(tmp_path):
    v = verify.parse_verdict('{"verified": false, "reason": "no Retry-After header", '
                             '"unmet": ["AC2"]}')
    assert v.verified is False
    assert v.unmet == ["AC2"]


def test_unparseable_output_does_not_verify(tmp_path):
    """The expensive error is marking done wrongly, so ambiguity is a no."""
    v = verify.parse_verdict("Looks good to me!")
    assert v.verified is False
    assert "no verdict" in v.reason


def test_empty_output_does_not_verify(tmp_path):
    assert verify.parse_verdict("").verified is False


def test_the_verifier_runs_read_only(tmp_path):
    """It renders an opinion. It must not be able to make the opinion true."""
    for forbidden in ("Write", "Edit", "Bash"):
        assert forbidden not in verify.VERIFY_TOOLS
    assert "Read" in verify.VERIFY_TOOLS


def test_criteria_come_from_the_story_spec_file_when_one_exists(tmp_path):
    story = _story(tmp_path)
    stories = story.spec_dir / "stories"
    stories.mkdir()
    (stories / "1-add-a-limiter.md").write_text("## Acceptance Criteria\n1. Returns 429\n")
    assert "Returns 429" in verify.acceptance_criteria(story)


def test_criteria_fall_back_to_the_plan_description(tmp_path):
    story = _story(tmp_path, description="Return 429 with Retry-After.")
    assert "Retry-After" in verify.acceptance_criteria(story)


def test_the_id_prefix_rule_is_why_the_filename_match_is_safe(tmp_path):
    """Ids are prefix-free by schema rule 2, so `1-*.md` cannot match story 1-2."""
    story = _story(tmp_path)
    stories = story.spec_dir / "stories"
    stories.mkdir()
    (stories / "1-mine.md").write_text("MINE\n")
    (stories / "2-theirs.md").write_text("THEIRS\n")
    assert "MINE" in verify.acceptance_criteria(story)
    assert "THEIRS" not in verify.acceptance_criteria(story)


def test_a_timeout_is_not_a_pass(tmp_path):
    import subprocess as sp

    def _timeout(argv):
        raise sp.TimeoutExpired(cmd=argv, timeout=1)

    v = verify.check(_story(tmp_path), worktree_path=tmp_path, diff="", runner=_timeout)
    assert v.verified is False
    assert "timed out" in v.reason


def test_a_crash_is_not_a_pass(tmp_path):
    def _boom(argv):
        raise OSError("claude not found")

    v = verify.check(_story(tmp_path), worktree_path=tmp_path, diff="", runner=_boom)
    assert v.verified is False
    assert "could not run" in v.reason


def test_a_huge_diff_is_truncated_and_says_so(tmp_path):
    argv = verify.build_verify_command(_story(tmp_path), worktree_path=tmp_path,
                                       diff="x" * 70000)
    prompt = argv[argv.index("-p") + 1]
    assert "diff truncated" in prompt
