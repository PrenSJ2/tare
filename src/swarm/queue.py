"""Which story runs next, and which have earned their way out of the queue.

`stories-schema.md` validity rule 3 is "No `status` field, ever" -- BMAD
deliberately keeps execution state out of the plan. So completion is derived
here, from the one record that already exists for this purpose: the nightshift
ledger. Nothing is written back into BMAD's tree, and there is no second
status file to drift against the first.

## Verified, not merely run

A story leaves the queue on a `story-verified` entry and on nothing else. A
dispatch that exited 0, wrote files and committed is not evidence that the
work was done -- it is evidence that a process ended. The distinction is the
reason this module exists rather than a `set()` of ids somewhere in the loop.

The consequence worth stating plainly: an unverified story is not a failure,
it is still open, and it comes back tomorrow. That is a cheap failure mode.
Marking one done wrongly is the expensive one, so everything here leans the
other way.

## Why parked stories are counted rather than remembered

A story parked for a bad reason -- a gate false positive, a transient blocked
response -- should be retried on another night. A story parked three times has
a problem no further night is going to solve, and holding the loop on it costs
every night after. So parks are counted from the ledger, not stored as a flag,
which also means clearing the count is something a human can do by reading the
ledger rather than by finding a hidden state file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import bmad
from .bmad import Story

# Ledger event names. `story-verified` is the ONLY one that removes a story
# from the queue; every other event is history.
VERIFIED_EVENT = "story-verified"
PARKED_EVENT = "story-parked"

# How many times a story may be parked before the loop stops offering it.
DEFAULT_MAX_PARKS = 3


@dataclass
class Pick:
    """The next runnable story, or the reason there isn't one."""
    story: Story | None
    reason: str
    skipped: list[tuple[Story, str]] = field(default_factory=list)


def _ledger() -> list[dict]:
    # Imported here rather than at module scope: `nightshift` will import
    # `queue` in Task 7, and a module-level import in both directions is a
    # cycle.
    from . import nightshift
    return nightshift.read_ledger()


def completed_keys() -> set[str]:
    """Story keys the ledger records as verified complete."""
    return {e.get("story_key") for e in _ledger()
            if e.get("event") == VERIFIED_EVENT and e.get("story_key")}


def park_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in _ledger():
        if entry.get("event") == PARKED_EVENT and entry.get("story_key"):
            counts[entry["story_key"]] = counts.get(entry["story_key"], 0) + 1
    return counts


def next_story(repo: Path, *, exclude: frozenset[str] = frozenset(),
               max_parks: int = DEFAULT_MAX_PARKS) -> Pick:
    """The first story, in plan order, that is runnable unattended.

    `exclude` holds keys already parked during THIS shift -- the ledger is
    written as the shift runs, but reading it back per pass to discover what
    this same loop just did would be a slower way of asking a question the
    caller already knows the answer to.
    """
    done = completed_keys()
    parks = park_counts()
    skipped: list[tuple[Story, str]] = []

    for story in bmad.stories_for(repo):
        if story.key in done:
            continue
        if story.key in exclude:
            continue
        if parks.get(story.key, 0) >= max_parks:
            skipped.append((story, f"parked {parks[story.key]} times already"))
            continue
        # `spec_checkpoint` is documented as caller-only and means a human
        # reviews the story spec between planning and implementation. There is
        # no human here, so honouring it means declining, not ignoring it.
        if story.spec_checkpoint:
            skipped.append((story, "spec_checkpoint is set and nobody is here to review it"))
            continue
        return Pick(story=story, reason="next in plan order", skipped=skipped)

    return Pick(story=None, reason="no story left to run", skipped=skipped)
