"""Classify capabilities by how often they get used.

`classify` writes `nodes.bucket` from a decayed-recency score over invocation
events. It exists so `shelve.py` (a later module) has a cheap, explainable
signal for "never touch this" vs "fair game" -- it is NOT the safety guard
itself. That guard is the `routes-to` traversal in a later module, seeded from
nodes with any usage at all; `classify` only has to be honest about recency,
not about what is safe to shelve.

`is_pinned` IS a safety predicate, and it lives here and only here. A previous
build grew a second, simplified copy of this check in another module, and the
two disagreed on a real capability -- pinned by one, shelveable by the other.
Nothing else in this codebase may re-derive it; everything that needs to know
whether something is pinned imports this function.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

# Names that are always kept loaded regardless of usage -- the tool's own
# dependencies (it would be absurd for harness to shelve its own browser
# driver) plus itself.
PINNED = frozenset({"agent-browser", "claude-api", "harness"})

# Prefixes that pin by *id component*, not by `name`. See is_pinned for why
# that distinction is load-bearing.
PINNED_PREFIXES = ("superpowers",)

# Score thresholds a node's decayed-invocation total is compared against.
ALWAYS_THRESHOLD = 4.0
SOMETIMES_THRESHOLD = 1.0

# Half-life-ish decay constant for score_node, in days. An invocation today
# contributes 1.0; one 30 days ago contributes ~0.37 (1/e).
DECAY_DAYS = 30.0


def is_pinned(name: str, node_id: str) -> bool:
    """Is this capability exempt from ever being shelved?

    Argument order is (name, node_id) -- deliberately, so a call site cannot
    silently transpose them and pass a type-checker.

    Two independent checks, both required:

    1. `name in PINNED` -- catches capabilities with a stable, known name.
    2. Every component of `node_id`, split on both ':' and '@', tested
       against PINNED_PREFIXES via startswith.

    Check 2 exists because plugin-provided skill nodes store only the leaf
    skill name in `name` (e.g. "brainstorming") -- the plugin/marketplace
    attribution lives in the id instead (e.g.
    "skill:brainstorming@superpowers"). Matching `name` alone means the
    prefix rule would never fire for any real superpowers-provided node, so
    the id is inspected component-by-component instead of as one string:
    a substring match against the whole id would also match an unrelated
    node whose *own* name happens to contain "superpowers" as a fragment.
    """
    if name in PINNED:
        return True
    for part in re.split(r"[:@]", node_id):
        if part.startswith(PINNED_PREFIXES):
            return True
    return False


def score_node(conn, node_id: str, now: datetime) -> float:
    """Decayed-recency usage score: sum(exp(-age_days / DECAY_DAYS)).

    Reads `events` rows of kind 'invocation' only -- per db.py, that table is
    a rebuilt cache re-derived from transcripts on every `mine` run, so this
    function must tolerate it being empty (score 0.0) rather than treat that
    as an error.
    """
    rows = conn.execute(
        "SELECT ts FROM events WHERE kind = 'invocation' AND node_id = ?",
        (node_id,),
    ).fetchall()
    total = 0.0
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["ts"])
        except ValueError:
            # A single corrupt timestamp must not zero out an otherwise-real
            # usage history for this node; it just contributes nothing.
            continue
        age_days = (now - ts).total_seconds() / 86400.0
        total += math.exp(-age_days / DECAY_DAYS)
    return total


def classify(conn, now: str | None = None) -> dict[str, int]:
    """Score every node and write nodes.bucket. Returns counts per bucket.

    `now` is a string (ISO 8601) rather than a datetime so callers -- and
    tests -- can pin the clock without monkeypatching datetime.now(). When
    omitted, the real current time is used. When given but unparseable, this
    raises naming the offending value rather than silently falling back to
    datetime.now(): a caller that passed a bad value wanted a specific clock,
    and substituting the real one would score against the wrong "now" without
    any sign that happened.
    """
    if now is None:
        current = datetime.now()
    else:
        try:
            current = datetime.fromisoformat(now)
        except ValueError:
            raise ValueError(f"classify: 'now' is not a valid ISO datetime: {now!r}") from None

    counts = {"always": 0, "sometimes": 0, "rarely": 0}
    for row in conn.execute("SELECT id, name FROM nodes").fetchall():
        node_id, name = row["id"], row["name"]
        if is_pinned(name, node_id):
            bucket = "always"
        else:
            score = score_node(conn, node_id, current)
            if score >= ALWAYS_THRESHOLD:
                bucket = "always"
            elif score >= SOMETIMES_THRESHOLD:
                bucket = "sometimes"
            else:
                bucket = "rarely"
        conn.execute("UPDATE nodes SET bucket = ? WHERE id = ?", (bucket, node_id))
        counts[bucket] += 1
    conn.commit()
    return counts
