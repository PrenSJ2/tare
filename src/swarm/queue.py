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
every night after. So parks are counted from the ledger, not stored as a flag
-- there is no separate state file that could drift from it, and a human can
always find the count by reading the ledger. That is not the same claim as
being able to CLEAR it by reading: the ledger is append-only, so resetting a
story's count means hand-editing (or truncating) that file, not flipping a
flag. What this design buys is a single visible source of truth for the
count, not a convenient way to reset it.

## What the counter counts, and what it deliberately does not

The paragraph above describes a judgement about the STORY: the gate refused
it, the dev agent reported it blocked, the verifier would not sign off. Those
are reasons a further night is not going to help, because nothing about the
story changed since it was last tried.

A `story-parked` entry can also record something that is not a judgement at
all: a leftover worktree directory from a prior crash, a hook that failed to
scope, a base sha or diff `git` could not read, a push that timed out. None
of those say anything about the story -- they say the machine could not get
far enough to try it. Counting them toward the same limit means a `story` a
gate would happily run is retired forever by a directory nobody deleted, and
deleting the directory does not un-retire it -- the append-only ledger still
has three parks on it. That is the failure this module now refuses to
reproduce: `park_counts` counts only the judgement kind. Infrastructure parks
still go to the ledger (so `recap` renders them and an operator can find and
fix the underlying cause) and still count toward `consecutive_failures` (so
three in a row still end the shift the same night) -- they simply do not
spend down the story's three tries at being judged.

An entry with no `kind` at all -- every entry written before this change --
is counted as a judgement. That is the conservative reading: those entries
already contributed to `park_counts` under the old code, and a story already
retired by three of them stays retired rather than being silently un-retired
the moment this module is upgraded. The alternative (treating absent as
infrastructure) would resurrect stories an operator may have already hand-
edited the ledger to deal with, on an assumption about history this module
has no way to check.
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

# The two things a `story-parked` entry's `kind` field can say. See the module
# docstring's "What the counter counts" section for why the distinction
# exists and why an absent field reads as JUDGEMENT.
PARK_KIND_JUDGEMENT = "judgement"
PARK_KIND_INFRASTRUCTURE = "infrastructure"

# How many times a story may be parked -- for a JUDGEMENT reason -- before the
# loop stops offering it. Infrastructure parks are visible in the ledger and
# still count toward a shift's `consecutive_failures` backstop, but they do
# not spend down this limit.
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
    """How many times each story has been parked for a JUDGEMENT reason.

    An infrastructure park (`kind == PARK_KIND_INFRASTRUCTURE`) is excluded --
    it is not evidence about the story, only about the machine that tried to
    run it, and counting it toward `DEFAULT_MAX_PARKS` is the defect this
    function exists to not have. An entry with no `kind` field (every entry
    written before that field existed) is counted as a judgement; see the
    module docstring for why that is the conservative reading.
    """
    counts: dict[str, int] = {}
    for entry in _ledger():
        if entry.get("event") != PARKED_EVENT or not entry.get("story_key"):
            continue
        if entry.get("kind", PARK_KIND_JUDGEMENT) != PARK_KIND_JUDGEMENT:
            continue
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
