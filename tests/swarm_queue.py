"""What the loop should do next, and what it must not do twice.

`stories.yaml` carries no status by rule, so completion is derived here from
the ledger. Every test in this file is really one assertion in disguise: a
story leaves the queue when it is VERIFIED, and never merely because it ran.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from swarm import bmad, nightshift as ns, queue


def _install(repo: Path, stories_yaml: str, slug: str = "spec-alpha") -> Path:
    cfg = repo / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text("project_name: demo\n")
    d = repo / "_bmad-output" / "specs" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SPEC.md").write_text("# spec\n")
    (d / "stories.yaml").write_text(stories_yaml)
    return repo


THREE = (
    '- id: "1"\n  title: First\n  description: D\n'
    '- id: "2"\n  title: Second\n  description: D\n'
    '- id: "3"\n  title: Third\n  description: D\n'
)


def test_the_first_unstarted_story_is_next(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    pick = queue.next_story(tmp_path)
    assert pick.story.id == "1"


def test_a_verified_story_does_not_come_back(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    ns.record({"event": queue.VERIFIED_EVENT, "story_key": "spec-alpha/1"})
    assert queue.next_story(tmp_path).story.id == "2"


def test_a_story_that_merely_ran_does_come_back(swarm_home, tmp_path):
    """Exit code is not evidence. This is the whole point of the design."""
    _install(tmp_path, THREE)
    ns.record({"event": "continued", "story_key": "spec-alpha/1", "exit_code": 0})
    assert queue.next_story(tmp_path).story.id == "1"


def test_an_exhausted_queue_says_so(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    for sid in ("1", "2", "3"):
        ns.record({"event": queue.VERIFIED_EVENT, "story_key": f"spec-alpha/{sid}"})
    pick = queue.next_story(tmp_path)
    assert pick.story is None
    assert "no story left" in pick.reason


# --- the caller-only fields -------------------------------------------------

def test_spec_checkpoint_is_skipped_because_nobody_is_there_to_review(swarm_home, tmp_path):
    _install(tmp_path,
             '- id: "1"\n  title: First\n  description: D\n  spec_checkpoint: true\n'
             '- id: "2"\n  title: Second\n  description: D\n')
    pick = queue.next_story(tmp_path)
    assert pick.story.id == "2"
    assert pick.skipped[0][0].id == "1"
    assert "nobody is here" in pick.skipped[0][1]


def test_done_checkpoint_still_runs_it_is_the_caller_that_stops_after(swarm_home, tmp_path):
    """done_checkpoint pauses dispatch AFTER the story, so the story itself
    is perfectly runnable. Task 7 owns the stopping."""
    _install(tmp_path, '- id: "1"\n  title: First\n  description: D\n  done_checkpoint: true\n')
    pick = queue.next_story(tmp_path)
    assert pick.story.id == "1"
    assert pick.story.done_checkpoint is True


def test_a_story_parked_three_times_stops_being_offered(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    for _ in range(3):
        ns.record({"event": queue.PARKED_EVENT, "story_key": "spec-alpha/1"})
    pick = queue.next_story(tmp_path)
    assert pick.story.id == "2"
    assert any("parked 3 times" in why for _, why in pick.skipped)


def test_a_story_parked_twice_is_still_offered(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    for _ in range(2):
        ns.record({"event": queue.PARKED_EVENT, "story_key": "spec-alpha/1"})
    assert queue.next_story(tmp_path).story.id == "1"


def test_exclude_keeps_this_shift_from_re_offering_what_it_just_parked(swarm_home, tmp_path):
    _install(tmp_path, THREE)
    assert queue.next_story(tmp_path, exclude=frozenset({"spec-alpha/1"})).story.id == "2"


def test_stories_from_two_spec_folders_are_namespaced_by_slug(swarm_home, tmp_path):
    _install(tmp_path, '- id: "1"\n  title: A\n  description: D\n', slug="spec-alpha")
    _install(tmp_path, '- id: "1"\n  title: B\n  description: D\n', slug="spec-beta")
    ns.record({"event": queue.VERIFIED_EVENT, "story_key": "spec-alpha/1"})
    pick = queue.next_story(tmp_path)
    assert pick.story.key == "spec-beta/1"


def test_completion_survives_into_a_fresh_process(swarm_home, tmp_path):
    """A test that reads back in-process proves the object, not the file.

    The precedent is the four scanners that returned without `conn.commit()`:
    every scan wrote nothing to disk and the suite stayed green, because the
    tests reused one connection and uncommitted writes are visible on the
    connection that made them.
    """
    import subprocess
    import sys

    _install(tmp_path, THREE)
    ns.record({"event": queue.VERIFIED_EVENT, "story_key": "spec-alpha/1"})

    result = subprocess.run(
        [sys.executable, "-c",
         "import os,sys;"
         "from pathlib import Path;"
         "from swarm import queue;"
         "print(queue.next_story(Path(sys.argv[1])).story.id)",
         str(tmp_path)],
        capture_output=True, text=True,
        env={**os.environ, "SWARM_HOME": str(swarm_home)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"
